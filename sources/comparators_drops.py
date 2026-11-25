"""Baisses de prix des comparateurs européens : idealo.fr / .de / .es / .it et geizhals.eu.

Idealo : la page publique « bons plans » de chaque pays expose une API de résultats (liens fournis dans la page),
filtrable et triable : `discountFrom` / `priceTo` / `sort=DISCOUNT_DESC|CREATION_DATE_DESC` / `pageIndex`.
Chaque offre donne marchand, prix, % d'économie vs prix habituel Idealo et lien de sortie vers le marchand.
Pour les sites hors France, seules les boutiques connues pour livrer la France sont gardées
(`ships_to_france_shops`, liste modifiable).
"""
from __future__ import annotations

import re
import threading
import time

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.catalog import CatalogSource
from sources.idealo_drops import item_to_deal
from sources.merchant_common import make_deal, percent, sort_by_discount
from utils import find_prices, normalize_text

IDEALO_LOCALES = {"fr": ("https://www.idealo.fr", "fr_FR", "FR"), "de": ("https://www.idealo.de", "de_DE", "DE"),
                  "es": ("https://www.idealo.es", "es_ES", "ES"), "it": ("https://www.idealo.it", "it_IT", "IT"),
                  "at": ("https://www.idealo.at", "de_AT", "AT"), "uk": ("https://www.idealo.co.uk", "en_GB", "UK")}

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def idealo_deals_url(country: str, discount_from: int = 50, price_to: int = 200, sort: str = "",
                     page_index: int = 0, items: int = 60, category_id: str = "") -> str:
    base, locale, _ = IDEALO_LOCALES[country]
    url = (f"{base}/csr/api/v2/modules/dealsResult?locale={locale}&itemsPerPage={items}"
           f"&discountFrom={discount_from}&discountTo=100&priceTo={price_to}&itemStates=BARGAIN")
    if sort:
        url += f"&sort={sort}"
    if page_index:
        url += f"&pageIndex={page_index}"
    if category_id:
        url += f"&categoryId={category_id}"
    return url


def fetch_idealo_items(http, url: str, ttl: float = 240) -> list[dict]:
    """Résultats d'une URL de l'API (mutualisés quelques minutes entre sources : Tradeinn, Galaxus... relisent
    les mêmes pages sans nouvelle requête)."""
    with _cache_lock:
        hit = _cache.get(url)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    referer = url.split("/csr/")[0] + "/"
    data = http.get_json(url, headers={"Referer": referer})
    items = data.get("items", []) if isinstance(data, dict) else []
    with _cache_lock:
        _cache[url] = (time.monotonic(), items)
    return items


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def shop_matches(shop: str, patterns: list[str]) -> bool:
    norm = normalize_text(shop).replace(" ", "")
    return any(normalize_text(p).replace(" ", "") in norm for p in patterns if p)


class IdealoDealsSource(CatalogSource):
    """Moteur `idealo_deals` : un pays Idealo, plusieurs tris, filtre de livraison vers la France."""
    engine = "idealo_deals"

    def urls(self) -> list[str]:
        country = self.cfg.get("idealo_country", "de")
        urls = []
        for sort in self.cfg.get("sorts", ["CREATION_DATE_DESC", "DISCOUNT_DESC"]):
            for page in range(int(self.cfg.get("pages", 1))):
                urls.append(idealo_deals_url(country, int(self.cfg.get("discount_from", 50)),
                                             int(self.cfg.get("price_to", 200)), sort, page))
        return urls

    def fetch(self) -> list[Deal]:
        country = self.cfg.get("idealo_country", "de")
        base, _, site_country = IDEALO_LOCALES[country]
        allowed = self.cfg.get("ships_to_france_shops")
        deals: dict[str, Deal] = {}
        dropped = 0
        for url in self.urls():
            try:
                items = fetch_idealo_items(self.http, url)
                self.pages_ok += 1
            except FetchError as exc:
                self.note_error(exc, f"{self.label} API")
                if exc.blocked:
                    break
                continue
            for it in items:
                shop = (it.get("shopInfo") or {}).get("shopName", "")
                if allowed is not None and not shop_matches(shop, allowed):
                    dropped += 1
                    continue
                d = item_to_deal(it, base, source=self.name, country=site_country)
                if d and d.source_id not in deals:
                    deals[d.source_id] = d
        if dropped:
            self.log.debug("%s : %d offre(s) ignorée(s) (boutique ne livrant pas la France)", self.name, dropped)
        return sort_by_discount(list(deals.values()))


def parse_geizhals_deals(html: str, base_url: str = "https://geizhals.eu") -> list[Deal]:
    """Page « Deals » de geizhals : vignettes produit avec prix actuel, ancien prix / % de baisse."""
    soup = BeautifulSoup(html, "lxml")
    deals: dict[str, Deal] = {}
    for it in soup.select(".listview__item, .productlist__item, article[data-id], .deal, .deals__item"):
        a = it.select_one("a.listview__name-link, a.productlist__link, a[href*='-a']")
        if a is None:
            continue
        href = a.get("href", "")
        url = href if href.startswith("http") else base_url + "/" + href.lstrip("/")
        m = re.search(r"-a(\d+)\.html", href)
        text = it.get_text(" ", strip=True)
        prices = [v for v, _ in find_prices(text)]
        if not prices:
            continue
        price = min(prices)
        original = max(prices) if max(prices) > price else None
        d = make_deal("geizhals", "geizhals.eu (plusieurs marchands)", a.get_text(" ", strip=True), url, price,
                      original, percent(text), source_id=m.group(1) if m else url)
        if d:
            d.site_country = "AT"
            deals[d.source_id] = d
    return sort_by_discount(list(deals.values()))


class GeizhalsDealsSource(CatalogSource):
    """Moteur `geizhals_deals` (geizhals.eu renvoie souvent 403 depuis la France : pause automatique)."""
    engine = "geizhals_deals"

    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        for url in self.cfg.get("page_urls", ["https://geizhals.eu/?deals"]):
            html = self.get_text(url, f"{self.label} {url}")
            if html is None:
                if self.blocked:
                    break
                continue
            for d in parse_geizhals_deals(html):
                d.source = self.name
                deals.append(d)
        return deals


def idealo_shop_offers(source: CatalogSource, shop_patterns: list[str], countries: list[str]) -> list[Deal]:
    """Offres « bons plans » Idealo (plusieurs pays) d'une enseigne précise (ex. boutiques Tradeinn, Galaxus)."""
    deals: dict[str, Deal] = {}
    for country in countries:
        if country not in IDEALO_LOCALES:
            continue
        base, _, site_country = IDEALO_LOCALES[country]
        for sort in ("CREATION_DATE_DESC", "DISCOUNT_DESC"):
            url = idealo_deals_url(country, int(source.cfg.get("discount_from", 40)),
                                   int(source.cfg.get("price_to", 200)), sort)
            try:
                items = fetch_idealo_items(source.http, url)
                source.pages_ok += 1
            except FetchError as exc:
                source.note_error(exc, f"{source.label} via idealo.{country}")
                break
            for it in items:
                shop = (it.get("shopInfo") or {}).get("shopName", "")
                if not shop_matches(shop, shop_patterns):
                    continue
                d = item_to_deal(it, base, source=source.name, country=site_country)
                if d and d.source_id not in deals:
                    d.description = f"{d.description} — vu sur idealo.{country}"
                    deals[d.source_id] = d
    return list(deals.values())
