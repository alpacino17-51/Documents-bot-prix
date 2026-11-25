"""Decathlon : pages « Deals » / « Bons plans » (et toute page liste configurée).

Les pages liste sont rendues côté serveur (Next.js) : chaque produit est une `.product-card` avec
prix actuel (`.vp-price-amount`), prix initial barré (`.vp-price-barred-amount`) et remise (`.vp-price-discount`).
Le JSON-LD (Product / ItemList) est lu en priorité quand il contient des prix.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from sources.merchant_common import absolute, card_text_price, jsonld_products, make_deal, percent, sort_by_discount

BASE = "https://www.decathlon.fr"
_ID = re.compile(r"/p/[^/]+/(\d+)/(?:[a-z0-9]*m(\d+))?")


def _ids(url: str) -> tuple[str, str]:
    m = _ID.search(url)
    if not m:
        return url, ""
    return m.group(1), (m.group(2) or "")


def parse_listing(html: str, base_url: str = BASE) -> list[Deal]:
    deals: dict[str, Deal] = {}
    for p in jsonld_products(html, base_url):
        pid, model = _ids(p["url"])
        d = make_deal("decathlon", "Decathlon", p["title"], p["url"], p["price"], p["original"], source_id=pid,
                      category="Sport", ean=p["gtin"], mpn=p["mpn"] or model, link_host="decathlon.fr")
        if d:
            d.brand = p.get("brand", "")
            deals[d.source_id] = d

    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(".product-card"):
        link = card.select_one("h2 a[href*='/p/']") or card.select_one("a[href*='/p/']")
        if link is None:
            continue
        url = absolute(link.get("href", "").split("?")[0], base_url)
        pid, model = _ids(url)
        if pid in deals:
            continue
        title_el = card.select_one("h2") or link
        title = title_el.get_text(" ", strip=True)
        price_el = card.select_one(".vp-price-amount--sale") or card.select_one(
            ".vp-price-amount:not(.vp-price-barred-amount)")
        barred_el = card.select_one(".vp-price-barred-amount")
        disc_el = card.select_one(".vp-price-discount")
        price = card_text_price(price_el)
        original = card_text_price(barred_el)
        pct = percent(disc_el.get_text(" ", strip=True)) if disc_el else None
        promo = card.find(string=re.compile(r"Promotion du", re.I))
        d = make_deal("decathlon", "Decathlon", title, url, price, original, pct, source_id=pid, category="Sport",
                      mpn=model, link_host="decathlon.fr", description=str(promo).strip() if promo else "")
        if d:
            deals[pid] = d
    return sort_by_discount(list(deals.values()))


class DecathlonSource(BaseSource):
    name = "decathlon"
    label = "Decathlon"

    def fetch(self) -> list[Deal]:
        base = self.cfg.get("base_url", BASE)
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"Decathlon {url}")
            if html is None:
                if self.blocked:
                    break      # même protection sur toutes les pages : on n'insiste pas
                continue
            for d in parse_listing(html, base):
                deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
