"""Courir (courir.com) : pages Outlet / Promotions / Soldes.

Les pages liste (Salesforce Commerce Cloud) sont rendues côté serveur : chaque `.product-tile` porte un attribut
`data-gtm` (JSON) avec marque, nom, référence fournisseur, prix, remise et indicateur outlet. Pagination
`?start=<n>&sz=<taille>`.

Particularité de l'Outlet : beaucoup d'articles ont un prix cassé SANS prix barré. Pour les marques de
`filters.resale_brands`, ces articles sont marqués `allow_no_strike` : ils peuvent déclencher une vérification
multi-marchands (comparaison au prix du marché) même sans réduction affichée.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from sources.merchant_common import absolute, make_deal, sort_by_discount
from sources.sneakers_generic import _STRIKE_SELECTOR
from utils import find_prices

BASE = "https://www.courir.com"


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def page_url(url: str, start: int, size: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}start={start}&sz={size}"


def parse_listing(html: str, base_url: str = BASE, allow_no_strike: bool = True) -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    deals: dict[str, Deal] = {}
    for tile in soup.select(".product-tile[data-gtm]"):
        try:
            gtm = json.loads(tile["data-gtm"])
        except (json.JSONDecodeError, KeyError):
            continue
        price = _f(gtm.get("price"))
        link = tile.find("a", href=re.compile(r"/p/"))
        if price is None or link is None:
            continue
        url = absolute(link["href"].split("?")[0], base_url)
        original = None
        for el in tile.select(_STRIKE_SELECTOR):          # prix barré affiché dans la vignette
            values = [v for v, _ in find_prices(el.get_text(" ", strip=True)) if v > price]
            if values:
                original = max(values)
                break
        discount = _f(gtm.get("discount")) or 0
        if original is None and discount > 0 and str(gtm.get("isDiscount")) == "1":
            original = round(price + discount, 2)           # remise exprimée en euros dans data-gtm
        brand = (gtm.get("brand") or "").strip()
        name = (gtm.get("name") or link.get_text(" ", strip=True) or "").strip()
        title = f"{brand.title()} {name.title()}".strip()
        sku = str(gtm.get("sku") or tile.get("data-itemid") or url)
        is_outlet = str(gtm.get("isOutlet")) == "1"
        d = make_deal("courir", "Courir", title, url, price, original, source_id=sku, category="Mode",
                      mpn=str(gtm.get("supplierRef") or gtm.get("supplier_id") or ""), link_host="courir.com",
                      category_hint=str(gtm.get("category") or "").replace("-", " ").lower(),
                      description="Outlet" if is_outlet else "")
        if d is None:
            continue
        d.brand = brand.title()
        if d.original_price is None and is_outlet and allow_no_strike:
            d.allow_no_strike = True          # prix cassé sans prix barré : comparaison au marché si marque revente
            d.force_market_compare = True
        deals.setdefault(sku, d)
    return sort_by_discount(list(deals.values()))


class CourirSource(BaseSource):
    name = "courir"
    label = "Courir"

    def fetch(self) -> list[Deal]:
        size = int(self.cfg.get("page_size", 40))
        pages = int(self.cfg.get("pages", 3))
        max_price = float(self.general.get("max_price_eur", 200))
        allow = bool(self.cfg.get("outlet_allow_no_strike_for_resale_brands", True))
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", []):
            for n in range(pages):
                html = self.get_text(page_url(url, n * size, size), f"Courir {url} p{n + 1}")
                if html is None:
                    break
                found = parse_listing(html, self.cfg.get("base_url", BASE), allow)
                if not found:
                    break
                for d in found:
                    if d.price is not None and d.price <= max_price:     # filtre ≤ 200 € dès la collecte
                        deals.setdefault(d.source_id, d)
            if self.blocked:
                break
        return sort_by_discount(list(deals.values()))
