"""Moteur catalogue partagé pour les enseignes directes (high-tech, grande distribution, mode, sport, jouets...).

Une entrée `sites_catalog` avec `"engine": "retailer"` suffit : le moteur essaie, dans l'ordre de `strategies`
(défaut : jsonld -> embedded -> shopify -> cards), et garde la première méthode qui trouve des produits REMISÉS :
- `jsonld`   : schema.org Product / ItemList (prix + prix barré `StrikethroughPrice`) ;
- `embedded` : JSON embarqué (`__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`, scripts `application/json`...) :
               tout objet ayant un nom, un prix et un ancien prix (oldPrice, listPrice, wasPrice, crossedPrice...) ;
- `shopify`  : boutiques Shopify (`/collections/<x>` -> `/collections/<x>/products.json`) ;
- `cards`    : vignettes HTML (lien produit + prix affichés), en dernier recours.
Les pages protégées (403 / DataDome / Akamai / Cloudflare) sont signalées proprement et la source mise en pause.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.catalog import CatalogSource
from sources.merchant_common import absolute, jsonld_products, make_deal, sort_by_discount
from sources.sneakers_generic import parse_cards, parse_shopify
from utils import parse_price

NAME_KEYS = ("name", "title", "productName", "product_name", "label", "designation", "libelle")
PRICE_KEYS = ("salePrice", "sale_price", "finalPrice", "final_price", "currentPrice", "current_price", "priceNow",
              "promoPrice", "promotionalPrice", "specialPrice", "special_price", "sellingPrice", "offerPrice",
              "discountedPrice", "price", "prix", "amount", "value")
OLD_KEYS = ("oldPrice", "old_price", "originalPrice", "original_price", "listPrice", "list_price", "regularPrice",
            "regular_price", "strikePrice", "strikeThroughPrice", "strikethroughPrice", "crossedPrice",
            "crossed_price", "wasPrice", "was_price", "insteadOfPrice", "priceBeforeDiscount", "basePrice",
            "compareAtPrice", "compare_at_price", "referencePrice", "rrp", "msrp", "uvp", "initialPrice",
            "prixBarre", "prix_barre", "previousPrice", "fullPrice", "standardPrice")
URL_KEYS = ("url", "productUrl", "product_url", "link", "href", "canonicalUrl", "uri", "path", "slug", "seoUrl")
BRAND_KEYS = ("brand", "brandName", "brand_name", "manufacturer", "marque", "vendor")
STATE_SCRIPT = re.compile(r"(?:window\.)?(__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__"
                          r"|__STATE__|__DATA__|initialState|__INITIAL_DATA__)\s*=\s*", re.I)


def _price_of(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        value = parse_price(v)[0]
        return value if value and value > 0 else None
    if isinstance(v, dict):
        for k in ("amountInclusive", "amount", "value", "price", "gross", "raw", "centAmount", "formatted",
                  "formattedValue", "displayValue"):
            if k in v:
                p = _price_of(v[k])
                if p is not None:
                    return p / 100 if k == "centAmount" else p
    return None


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _walk(o: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 40:
        return
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v, depth + 1)
    elif isinstance(o, list):
        for v in o[:500]:
            yield from _walk(v, depth + 1)


def embedded_json_blobs(html: str) -> Iterator[Any]:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = (script.string or script.get_text() or "").strip()
        if len(text) < 50:
            continue
        stype = (script.get("type") or "").lower()
        if "ld+json" in stype:
            continue
        candidates = []
        if "json" in stype or script.get("id") == "__NEXT_DATA__":
            candidates.append(text.removeprefix("<!--").removesuffix("-->"))
        else:
            m = STATE_SCRIPT.search(text)
            if m:
                body = text[m.end():].strip().rstrip(";")
                candidates.append(body)
        for c in candidates:
            try:
                yield json.loads(c)
            except (json.JSONDecodeError, ValueError):
                start = c.find("{")
                end = c.rfind("}")
                if 0 <= start < end:
                    try:
                        yield json.loads(c[start:end + 1])
                    except (json.JSONDecodeError, ValueError):
                        continue


def parse_embedded(html: str, base_url: str, source: str, category: str = "") -> list[Deal]:
    """Produits remisés trouvés dans le JSON embarqué (heuristique : nom + prix + ancien prix + lien)."""
    deals: dict[str, Deal] = {}
    for blob in embedded_json_blobs(html):
        for d in _walk(blob):
            name = _first(d, NAME_KEYS)
            if not isinstance(name, str) or len(name) < 4:
                continue
            old = _price_of(_first(d, OLD_KEYS))
            price = None
            for k in PRICE_KEYS:
                if k in d:
                    price = _price_of(d[k])
                    if price is not None:
                        break
            if price is None and isinstance(d.get("prices"), dict):
                price = _price_of(_first(d["prices"], PRICE_KEYS))
                old = old or _price_of(_first(d["prices"], OLD_KEYS))
            if price is None or old is None or old <= price:
                continue
            url = _first(d, URL_KEYS)
            if not isinstance(url, str) or not url:
                continue
            url = absolute(url if ("/" in url or url.startswith("http")) else "/" + url, base_url)
            brand = _first(d, BRAND_KEYS)
            if isinstance(brand, dict):
                brand = brand.get("name") or brand.get("label") or ""
            brand = brand if isinstance(brand, str) else ""
            title = name if not brand or brand.lower() in name.lower() else f"{brand} {name}"
            deal = make_deal(source, source, title, url.split("#")[0], price, old, source_id=url, category=category)
            if deal:
                deal.brand = brand
                deals.setdefault(deal.url, deal)
    return list(deals.values())


def parse_jsonld_discounted(html: str, base_url: str, source: str, category: str = "") -> list[Deal]:
    deals = []
    for p in jsonld_products(html, base_url):
        if not p["original"]:
            continue
        d = make_deal(source, source, p["title"], p["url"] or base_url, p["price"], p["original"],
                      source_id=p["sku"] or p["url"], category=category or p["category"], ean=p["gtin"], mpn=p["mpn"])
        if d:
            d.brand = p["brand"]
            deals.append(d)
    return deals


def shopify_collection_url(page_url: str) -> Optional[str]:
    parts = urlsplit(page_url)
    m = re.search(r"(/(?:[a-z]{2}(?:-[a-z]{2})?/)?collections/[^/?#]+)", parts.path)
    if not m:
        return None
    return f"{parts.scheme}://{parts.netloc}{m.group(1)}/products.json?limit=250"


class RetailerSource(CatalogSource):
    engine = "retailer"

    def _parse(self, strategy: str, html: str, url: str) -> list[Deal]:
        base = self.base_url or f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        category = self.cfg.get("category", "")
        if strategy == "jsonld":
            return parse_jsonld_discounted(html, base, self.name, category)
        if strategy == "embedded":
            return parse_embedded(html, base, self.name, category)
        if strategy == "cards":
            deals = parse_cards(html, base, self.name, self.cfg.get("link_contains", "/"), category,
                                link_regex=self.cfg.get("link_regex"))
            return [d for d in deals if d.original_price]          # seulement les produits remisés
        if strategy == "shopify":
            if "cdn.shopify.com" not in html and "Shopify.theme" not in html:
                return []
            api = shopify_collection_url(url)
            if not api:
                return []
            try:
                payload = self.http.get_json(api)
            except FetchError as exc:
                self.note_error(exc, f"{self.label} {api}")
                return []
            return parse_shopify(payload, base, self.name, "/products/", category or "", self.currency)
        return []

    def fetch(self) -> list[Deal]:
        strategies = self.cfg.get("strategies", ["jsonld", "embedded", "shopify", "cards"])
        deals: dict[str, Deal] = {}
        used: set[str] = set()
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"{self.label} {url}")
            if html is None:
                if self.blocked:
                    break          # même protection anti-robot sur toutes les pages du site : on n'insiste pas
                continue
            for strategy in strategies:
                try:
                    found = self._parse(strategy, html, url)
                except Exception:                  # une page atypique ne doit pas casser la source
                    self.log.exception("%s : erreur d'analyse (%s)", self.label, strategy)
                    found = []
                if found:
                    used.add(strategy)
                    for d in found:
                        deals.setdefault(d.url, d)
                    break
        if used:
            self.log.debug("%s : méthode(s) %s", self.label, ", ".join(sorted(used)))
        host = (self.base_url.split("//")[-1].split("/")[0]).removeprefix("www.") if self.base_url else ""
        out = []
        for d in deals.values():
            d.merchant = self.cfg.get("merchant") or self.label
            d.site_country = self.country
            d.currency = self.currency
            d.link_host = d.link_host or host
            out.append(d)
        return sort_by_discount(out)
