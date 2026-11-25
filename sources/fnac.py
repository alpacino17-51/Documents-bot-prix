"""Fnac : pages « Bons plans » / « Ventes flash » (High-tech, Gaming, Informatique).

fnac.com est protégé par DataDome : depuis une IP résidentielle « froide », les pages renvoient souvent un
403 avec une page captcha. Le bot le détecte, le signale proprement et met la source en pause (voir
`general.blocked_pause_minutes`) ; il ne tente jamais de contourner la protection.

Quand la page est servie, on lit dans l'ordre : JSON-LD (Product / ItemList), puis les vignettes HTML
(`.Article-item` : titre `.Article-title`, prix `.userPrice` / `.f-priceBox-price`, ancien prix `.oldPrice`).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from sources.merchant_common import absolute, card_text_price, jsonld_products, make_deal, percent, sort_by_discount

BASE = "https://www.fnac.com"
CARD_SELECTORS = ".Article-item, .Article-itemGroup, article.f-productCard, li.clearfix.Article-item"
_ID = re.compile(r"/a(\d+)/")


def fnac_price(el):
    """Prix Fnac, y compris le format « 79€<sup>99</sup> » (= 79,99 €)."""
    if el is None:
        return None
    compact = el.get_text("", strip=True).replace(" ", "").replace(" ", "")
    m = re.search(r"(\d[\d.]*)€(\d{2})(?!\d)", compact)
    if m:
        return float(m.group(1).replace(".", "") + "." + m.group(2))
    return card_text_price(el)


def _pid(url: str) -> str:
    m = _ID.search(url)
    return m.group(1) if m else url


def parse_listing(html: str, base_url: str = BASE) -> list[Deal]:
    deals: dict[str, Deal] = {}
    for p in jsonld_products(html, base_url):
        d = make_deal("fnac", "Fnac", p["title"], p["url"], p["price"], p["original"], source_id=_pid(p["url"]),
                      category=p["category"], ean=p["gtin"], mpn=p["mpn"], link_host="fnac.com")
        if d:
            deals[d.source_id] = d

    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(CARD_SELECTORS):
        a = card.select_one("a.Article-title, .Article-title a, .f-productCard-title a, a[href*='/a']")
        if a is None or not a.get("href"):
            continue
        url = absolute(a["href"].split("#")[0], base_url)
        pid = _pid(url)
        if pid in deals:
            continue
        title = a.get("title") or a.get_text(" ", strip=True)
        price = fnac_price(card.select_one(".userPrice, .f-priceBox-price--reco, .f-priceBox-price, .price"))
        original = fnac_price(card.select_one(".oldPrice, .f-priceBox-price--old, del, s"))
        disc_el = card.select_one(".f-priceBox-discount, .discount, .Article-discount, .f-badge--discount")
        pct = percent(disc_el.get_text(" ", strip=True)) if disc_el else None
        seller_el = card.select_one(".Article-seller, .f-productCard-seller")
        seller = seller_el.get_text(" ", strip=True) if seller_el else ""
        d = make_deal("fnac", "Fnac" + (f" ({seller})" if seller and "fnac" not in seller.lower() else ""),
                      title, url, price, original, pct, source_id=pid, link_host="fnac.com")
        if d:
            deals[pid] = d
    return sort_by_discount(list(deals.values()))


class FnacSource(BaseSource):
    name = "fnac"
    label = "Fnac"

    def fetch(self) -> list[Deal]:
        base = self.cfg.get("base_url", BASE)
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"Fnac {url}")
            if html is None:
                if self.blocked:
                    break      # inutile d'insister sur les autres pages : même protection
                continue
            for d in parse_listing(html, base):
                deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
