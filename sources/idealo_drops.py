"""Idealo « Bons plans » : produits dont le prix actuel est nettement sous leur prix habituel, tous marchands
confondus (Boulanger, Fnac, Darty, Cdiscount, Rakuten, Amazon...).

La page publique https://www.idealo.fr/bons-plans embarque ses résultats (JSON base64) et expose les liens de
son API publique de résultats, filtrable : `discountFrom`/`discountTo` (%), `priceFrom`/`priceTo` (€),
`categoryId`. Chaque produit porte `bargainInfo.saving` = % d'économie par rapport au prix habituel Idealo.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from http_client import FetchError
from models import Deal
from sources.base import BaseSource
from sources.idealo import _SEARCH_B64, idealo_url
from sources.merchant_common import make_deal, sort_by_discount

BASE = "https://www.idealo.fr"


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        return payload.get("items") or []
    return []


def items_from_html(html: str) -> list[dict]:
    m = _SEARCH_B64.search(html)
    if not m:
        return []
    try:
        return _items(json.loads(base64.b64decode(m.group(1)).decode("utf-8")))
    except (ValueError, json.JSONDecodeError):
        return []


def item_to_deal(it: dict, base_url: str = BASE, source: str = "idealo-drops",
                 country: str = "FR") -> Optional[Deal]:
    if it.get("type") not in (None, "PRODUCT"):
        return None
    offer = it.get("offerInfo") or {}
    if offer.get("usedOnly"):
        return None
    shop = (it.get("shopInfo") or {}).get("shopName", "")
    saving = (it.get("bargainInfo") or {}).get("saving")
    price = it.get("rawPrice")
    href = idealo_url(it.get("href") or "", base_url) or offer.get("leadoutHref") or ""
    usual = round(price / (1 - saving / 100), 2) if price and saving and 0 < saving < 100 else None
    deal = make_deal(
        source, shop or "Idealo", it.get("title", ""), href,
        float(price) if price is not None else None, usual, float(saving) if saving else None,
        source_id=str(it.get("id", "")), category_hint=it.get("subheading", ""),
        link_host=shop.lower() if re.fullmatch(r"[a-z0-9-]+(\.[a-z]{2,})+", shop.lower()) else "",
        merchant_ref={"market_median": usual} if usual else {},     # prix habituel calculé par Idealo
        description=f"{it.get('subheading', '')} — économie Idealo {saving} % vs prix habituel"
        + (" (baisse récente)" if (it.get("bargainInfo") or {}).get("isShortTerm") else ""),
    )
    if deal:
        deal.merchant_url = offer.get("leadoutHref") or deal.url     # lien direct vers l'offre du marchand
        deal.site_country = country
    return deal


class IdealoDropsSource(BaseSource):
    name = "idealo_drops"
    label = "Idealo bons plans"

    def fetch(self) -> list[Deal]:
        deals: dict[str, Deal] = {}
        api_ok = False
        for url in self.cfg.get("api_urls", []):
            try:
                items = _items(self.http.get_json(url, headers={"Referer": BASE + "/bons-plans"}))
                self.pages_ok += 1
                api_ok = True
            except FetchError as exc:
                self.note_error(exc, "Idealo bons plans API")
                if exc.blocked:
                    break
                continue
            self._add(deals, items)
        if not api_ok and not self.blocked:      # repli : page HTML publique (résultats en JSON base64)
            html = self.get_text(self.cfg.get("page_url", BASE + "/bons-plans"), "Idealo bons plans page")
            self._add(deals, items_from_html(html) if html else [])
        return sort_by_discount(list(deals.values()))

    @staticmethod
    def _add(deals: dict[str, Deal], items: list[dict]) -> None:
        for it in items:
            d = item_to_deal(it)
            if d:
                deals.setdefault(d.source_id, d)
