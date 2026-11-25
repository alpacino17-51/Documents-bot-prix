"""Galaxus (galaxus.fr) : offres du jour / promotions / liquidation.

Les pages Galaxus sont en Next.js : les produits servis côté serveur sont dans `__NEXT_DATA__`
(objets `Product` : nom, marque, `price.amountInclusive`, `insteadOfPrice` = ancien prix, `relativeUrl`).
Les listes complètes « Déstockage » sont chargées ensuite par l'application (non utilisées) ; on complète donc
avec les offres Galaxus présentes dans les « bons plans » publics d'Idealo.
"""
from __future__ import annotations

import json
from typing import Any, Iterator, Optional

from bs4 import BeautifulSoup

from models import Deal
from sources.catalog import CatalogSource
from sources.comparators_drops import idealo_shop_offers
from sources.merchant_common import make_deal, sort_by_discount

BASE = "https://www.galaxus.fr"


def _walk(o: Any) -> Iterator[dict]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _amount(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for key in ("amountInclusive", "amount", "value"):
            if isinstance(v.get(key), (int, float)):
                return float(v[key])
        if isinstance(v.get("price"), (dict, int, float)):
            return _amount(v["price"])
    return None


def parse_next_data(html: str, base_url: str = BASE, source: str = "galaxus") -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    script = soup.select_one("script#__NEXT_DATA__")
    if script is None or not script.string:
        return []
    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return []
    deals: dict[str, Deal] = {}
    for node in _walk(data):
        if node.get("__typename") != "Product" or "price" not in node:
            continue
        price = _amount(node.get("price"))
        original = _amount(node.get("insteadOfPrice"))
        rel = node.get("relativeUrl") or ""
        if not rel or price is None:
            continue
        brand = (node.get("brand") or {}).get("name", "") if isinstance(node.get("brand"), dict) else ""
        name = node.get("name", "")
        ext = (node.get("nameExtensions") or {}).get("properties") if isinstance(node.get("nameExtensions"), dict) else ""
        title = f"{brand} {name}".strip() + (f" ({ext})" if ext else "")
        pid = str(node.get("databaseId") or node.get("id") or rel)
        d = make_deal(source, "Galaxus", title, base_url + rel, price, original, source_id=pid,
                      link_host="galaxus.fr")
        if d:
            d.brand = brand
            deals.setdefault(pid, d)
    return list(deals.values())


class GalaxusSource(CatalogSource):
    engine = "galaxus"

    def fetch(self) -> list[Deal]:
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", [BASE + "/fr"]):
            html = self.get_text(url, f"{self.label} {url}")
            if html is None:
                if self.blocked:
                    break
                continue
            for d in parse_next_data(html, self.base_url or BASE, self.name):
                if d.original_price or self.cfg.get("include_non_discounted", False):
                    deals.setdefault(d.source_id, d)
        patterns = self.cfg.get("idealo_shop_patterns", ["galaxus"])
        for d in idealo_shop_offers(self, patterns, self.cfg.get("idealo_countries", ["fr"])):
            deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
