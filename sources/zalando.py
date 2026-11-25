"""Zalando : pages promo homme / femme / sport triées par réduction décroissante (`?order=sale`).

Chaque produit est un `<article>` : lien `.html`, titre `<h3>` (marque | nom), prix actuel puis prix de
référence libellés, par ex. « Prix habituel : 129,95 € », « Dernier prix le plus bas : 29,99 € »
(= plus bas des 30 derniers jours, obligation Omnibus) ou « Prix médian : 29,95 € ».
Ces références sont conservées dans `deal.merchant_ref` et servent à détecter un prix barré gonflé.
Tous les deals sont tagués catégorie « Mode » : pas de comparaison Idealo.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from sources.merchant_common import make_deal, sort_by_discount
from utils import normalize_text, parse_price

BASE = "https://www.zalando.fr"
_LABELS = (
    ("usual", ("prix habituel", "prix initial", "prix conseille")),
    ("low_30", ("dernier prix le plus bas", "prix le plus bas")),
    ("median", ("prix median",)),
)
_SKU = re.compile(r"-([a-z0-9]{9}-[a-z0-9]{3})\.html", re.I)


def _label_of(text: str) -> Optional[str]:
    norm = normalize_text(text)
    for key, words in _LABELS:
        if any(w in norm for w in words):
            return key
    return None


def parse_article(art, base_url: str = BASE) -> Optional[Deal]:
    link = art.find("a", href=re.compile(r"\.html"))
    h3 = art.find("h3")
    if link is None or h3 is None:
        return None
    url = link["href"] if link["href"].startswith("http") else base_url + link["href"]
    parts = [s for s in h3.stripped_strings]
    title = " ".join(parts) if parts else h3.get_text(" ", strip=True)

    price: Optional[float] = None
    refs: dict[str, float] = {}
    pending: Optional[str] = None
    after_title = False
    title_tokens = set(parts)
    for s in art.stripped_strings:
        if not after_title:
            if s in title_tokens and s == parts[-1]:
                after_title = True
            continue
        label = _label_of(s)
        if label:
            pending = label
            continue
        value, cur = parse_price(s)
        if value is None or "%" in s or cur is None and "€" not in s:
            continue
        if pending:
            refs.setdefault(pending, value)
            pending = None
        elif price is None:
            price = value
    if price is None:
        return None
    original = refs.get("usual") or refs.get("low_30") or refs.get("median")
    m = _SKU.search(url)
    deal = make_deal("zalando", "Zalando", title, url.split("?")[0], price, original,
                     source_id=(m.group(1).upper() if m else url), category="Mode",
                     link_host="zalando.fr", merchant_ref=refs)
    if deal and len(parts) > 1:
        deal.brand = parts[0]          # <h3> : « Marque | Nom du produit »
    return deal


def parse_listing(html: str, base_url: str = BASE) -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    deals: dict[str, Deal] = {}
    for art in soup.select("article"):
        d = parse_article(art, base_url)
        if d and d.source_id not in deals:
            deals[d.source_id] = d
    return sort_by_discount(list(deals.values()))


class ZalandoSource(BaseSource):
    name = "zalando"
    label = "Zalando"

    def fetch(self) -> list[Deal]:
        base = self.cfg.get("base_url", BASE)
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"Zalando {url}")
            if html is None:
                if self.blocked:
                    break
                continue
            for d in parse_listing(html, base):
                deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
