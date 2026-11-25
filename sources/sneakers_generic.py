"""Moteur configurable pour les boutiques sneakers / streetwear (une entrée `sites_catalog` par boutique).

`engine` choisit la méthode de lecture, de la plus structurée à la moins structurée :
- `shopify`   : JSON public Shopify `/collections/<collection>/products.json` (variantes : `price`,
                `compare_at_price`, `available`, `sku`) — asphaltgold, overkillshop, sneakersnstuff, size.co.uk ;
- `hypernova` : données JSON embarquées par les pages PrestaShop/Hypernova (`data-hypernova-key=ProductListing`)
                — footshop ;
- `jsonld`    : JSON-LD schema.org (Product / ItemList) des pages promo ;
- `cards`     : vignettes HTML (lien produit + prix affichés) — kickz et autres sites sans données structurées.
Tous les deals sont tagués « Mode » (pas d'Idealo ; vérification par marque / site officiel / présence web).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.catalog import CatalogSource
from sources.merchant_common import absolute, jsonld_products, make_deal, sort_by_discount
from utils import find_prices, normalize_text

_MODEL_CODE = re.compile(r"^[A-Z0-9]{2,}[-_ ]?[A-Z0-9]*\d[A-Z0-9-]*$", re.I)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _title(brand: str, name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    if brand and not normalize_text(name).startswith(normalize_text(brand)):
        return f"{brand} {name}"
    return name


# ------------------------------------------------------------------ Shopify

def parse_shopify(payload: dict, base_url: str, source: str, product_path: str = "/products/",
                  category: str = "Mode", currency: str = "EUR", include_full_price: bool = False) -> list[Deal]:
    deals: list[Deal] = []
    for p in (payload or {}).get("products", []):
        variants = [v for v in p.get("variants", []) if v.get("available", True)]
        best = None
        for v in variants:
            price, cmp = _f(v.get("price")), _f(v.get("compare_at_price"))
            if price is None:
                continue
            disc = (1 - price / cmp) if cmp and cmp > price else 0
            if best is None or disc > best[0]:
                best = (disc, price, cmp if disc else None, v)
        if best is None or (not best[0] and not include_full_price):
            continue
        _, price, original, variant = best
        brand = p.get("vendor") or ""
        sku = (variant.get("sku") or "").strip()
        d = make_deal(source, source, _title(brand, p.get("title", "")),
                      base_url.rstrip("/") + product_path + p.get("handle", ""), price, original,
                      source_id=str(p.get("id")), category=category,
                      mpn=sku.split(" ")[0] if sku and _MODEL_CODE.match(sku.split(" ")[0]) else "",
                      category_hint=p.get("product_type", ""))
        if d:
            d.brand, d.currency = brand, currency
            deals.append(d)
    return deals


# ------------------------------------------------------------------ Hypernova (Footshop)

def parse_hypernova(html: str, base_url: str, source: str, path_prefix: str = "/fr/", category: str = "Mode",
                    include_full_price: bool = False) -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    script = soup.select_one("script[data-hypernova-key=ProductListing]")
    if script is None:
        return []
    raw = (script.string or script.get_text() or "").strip()
    raw = raw.removeprefix("<!--").removesuffix("-->")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    products = ((data.get("data") or {}).get("state") or {}).get("products") or {}
    items = products.get("items", []) if isinstance(products, dict) else products
    deals: list[Deal] = []
    for p in items:
        if p.get("sold_out") or p.get("in_stock") is False:
            continue
        pr = p.get("price") or {}
        price, original = _f(pr.get("value")), _f(pr.get("value_without_reduction"))
        sale = p.get("sale") or pr.get("sale") or {}
        if not include_full_price and not (sale.get("active") or (original and price and original > price)):
            continue
        brand = (p.get("manufacturer") or {}).get("name", "")
        variants = (p.get("attributes") or {}).get("variants") or []
        ean = next((str(v.get("ean")) for v in variants if v.get("ean")), "")
        d = make_deal(source, source, _title(brand, p.get("title_without_manufacturer") or p.get("title") or p.get("name", "")),
                      base_url.rstrip("/") + path_prefix + (p.get("url") or "").lstrip("/"), price, original,
                      _f(sale.get("percents")), source_id=str(p.get("id")), category=category,
                      mpn=p.get("code") or "", ean=ean,
                      category_hint=(p.get("main_category") or {}).get("name", ""))
        if d:
            d.brand = brand
            deals.append(d)
    return deals


# ------------------------------------------------------------------ vignettes HTML

def parse_cards(html: str, base_url: str, source: str, link_contains: str = "/p/", category: str = "Mode",
                max_depth: int = 8) -> list[Deal]:
    """Pour chaque lien produit, remonte jusqu'au bloc qui contient ses prix (prix actuel = le plus bas,
    prix barré = le plus haut), sans données structurées."""
    soup = BeautifulSoup(html, "lxml")
    deals: dict[str, Deal] = {}
    for a in soup.select(f"a[href*='{link_contains}']"):
        url = absolute(a.get("href", "").split("?")[0].split("#")[0], base_url)
        if url in deals:
            continue
        card, prices = a, []
        for _ in range(max_depth):
            card = card.parent
            if card is None or card.name in ("body", "html"):
                break
            prices = [v for v, _ in find_prices(card.get_text(" ", strip=True))]
            if prices:
                break
        if not prices or card is None or len(card.select(f"a[href*='{link_contains}']")) > 3:
            continue        # bloc trop large (plusieurs produits) : on n'invente pas d'association
        texts = [t.get_text(" ", strip=True) for t in card.select(f"a[href*='{link_contains}']")]
        img = card.find("img", alt=True)
        title = max(texts + ([img["alt"]] if img else []), key=len, default="")
        price = min(prices)
        original = max(prices) if max(prices) > price else None
        pct = re.search(r"-\s?(\d{1,2})\s?%", card.get_text(" ", strip=True))
        d = make_deal(source, source, title, url, price, original, float(pct.group(1)) if pct else None,
                      source_id=url, category=category)
        if d:
            deals[url] = d
    return list(deals.values())


class SneakersGenericSource(CatalogSource):
    engine = "sneakers"

    def _merchant(self) -> str:
        return self.cfg.get("merchant") or self.label

    def fetch(self) -> list[Deal]:
        engine = self.cfg.get("engine", "shopify")
        base = self.base_url
        category = self.cfg.get("category", "Mode")
        full = bool(self.cfg.get("include_full_price", False))
        deals: dict[str, Deal] = {}

        if engine == "shopify":
            collection = self.cfg.get("collection", "sale")
            limit = int(self.cfg.get("limit", 250))
            for page in range(1, int(self.cfg.get("pages", 1)) + 1):
                url = f"{base}/collections/{collection}/products.json?limit={limit}&page={page}"
                try:
                    payload = self.http.get_json(url)
                    self.pages_ok += 1
                except FetchError as exc:
                    self.note_error(exc, f"{self.label} {url}")
                    break
                found = parse_shopify(payload, base, self.name, self.cfg.get("product_path", "/products/"),
                                      category, self.currency, full)
                for d in found:
                    deals.setdefault(d.source_id, d)
                if len((payload or {}).get("products", [])) < limit:
                    break
        else:
            for url in self.cfg.get("page_urls", []):
                html = self.get_text(url, f"{self.label} {url}")
                if html is None:
                    if self.blocked:
                        break
                    continue
                if engine == "hypernova":
                    found = parse_hypernova(html, base, self.name, self.cfg.get("path_prefix", "/fr/"), category, full)
                elif engine == "jsonld":
                    found = []
                    for p in jsonld_products(html, base):
                        d = make_deal(self.name, self.name, _title(p["brand"], p["title"]), p["url"], p["price"],
                                      p["original"], source_id=p["sku"] or p["url"], category=category,
                                      ean=p["gtin"], mpn=p["mpn"])
                        if d and (d.original_price or full):
                            d.brand = p["brand"]
                            found.append(d)
                else:
                    found = parse_cards(html, base, self.name, self.cfg.get("link_contains", "/p/"), category)
                for d in found:
                    deals.setdefault(d.source_id, d)

        out = []
        for d in deals.values():
            d.merchant = self._merchant()
            d.site_country = self.country
            d.currency = self.currency
            d.link_host = (base.split("//")[-1].split("/")[0]).removeprefix("www.")
            out.append(d)
        return sort_by_discount(out)
