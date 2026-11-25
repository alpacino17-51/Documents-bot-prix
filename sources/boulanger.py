"""Boulanger : pages liste (catégories High-tech / Informatique / Gaming / Petit électroménager).

Les pages « Bons plans » / « Vente flash » de Boulanger chargent leurs produits en JavaScript ; en revanche
les pages liste de catégories embarquent :
- `tc_vars["list_products"] = [...]` : id, prix TTC, % de remise, vendeur, état, marque, catégories, EAN ;
- un JSON-LD `ItemList` : nom et URL de chaque produit.
On combine les deux, puis repli sur le JSON-LD Product et enfin sur le HTML.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from sources.merchant_common import absolute, iter_jsonld, jsonld_products, make_deal, sort_by_discount
from utils import find_prices

BASE = "https://www.boulanger.com"
_TC = re.compile(r'tc_vars\["list_products"\]\s*=\s*(\[.*?\]);', re.S)
_REF = re.compile(r"/ref/(\d+)")


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _names_from_itemlist(html: str) -> dict[str, tuple[str, str]]:
    names: dict[str, tuple[str, str]] = {}
    for node in iter_jsonld(html):
        if node.get("@type") != "ItemList":
            continue
        for el in node.get("itemListElement") or []:
            if not isinstance(el, dict):
                continue
            item = el.get("item")
            url = el.get("url") or (item.get("url") if isinstance(item, dict) else item if isinstance(item, str) else "")
            name = el.get("name") or (item.get("name") if isinstance(item, dict) else "") or ""
            m = _REF.search(url or "")
            if m:
                names[m.group(1)] = (name, url)
    return names


def parse_listing(html: str, base_url: str = BASE, include_refurbished: bool = False) -> list[Deal]:
    deals: dict[str, Deal] = {}
    names = _names_from_itemlist(html)
    m = _TC.search(html)
    if m:
        try:
            products = json.loads(m.group(1))
        except json.JSONDecodeError:
            products = []
        for p in products:
            pid = str(p.get("list_product_id") or "")
            condition = (p.get("list_product_condition") or "neuf").lower()
            if not pid or (condition != "neuf" and not include_refurbished):
                continue
            name, url = names.get(pid, ("", f"{base_url}/ref/{pid}"))
            if not name:
                name = (p.get("list_product_name") or "").replace("_", " ").strip()
            price = _f(p.get("list_product_unitprice_ati"))
            pct = _f(p.get("list_product_discount"))
            seller = (p.get("list_product_seller") or "boulanger").replace("_", " ")
            merchant = "Boulanger" if seller == "boulanger" else f"Boulanger (vendeur : {seller})"
            cats = [c for c in (p.get("list_product_category1"), p.get("list_product_category2")) if c]
            category = " / ".join(c.replace("_", " ") for c in cats)
            d = make_deal("boulanger", merchant, name, absolute(url, base_url), price, None, pct, source_id=pid,
                          category=category, ean=str(p.get("list_product_ean") or ""), link_host="boulanger.com",
                          description=f"État : {condition}" if condition != "neuf" else "")
            if d:
                d.brand = (p.get("list_product_brand") or "").strip()
                deals[pid] = d

    if not deals:      # repli : JSON-LD Product
        for p in jsonld_products(html, base_url):
            mm = _REF.search(p["url"])
            pid = mm.group(1) if mm else p["url"]
            d = make_deal("boulanger", "Boulanger", p["title"], p["url"], p["price"], p["original"], source_id=pid,
                          category=p["category"], ean=p["gtin"], mpn=p["mpn"], link_host="boulanger.com")
            if d:
                deals[pid] = d

    if not deals:      # dernier recours : HTML des vignettes produit
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href*='/ref/']"):
            mm = _REF.search(a.get("href", ""))
            if not mm or mm.group(1) in deals:
                continue
            tile = a.find_parent(["article", "li"]) or a.find_parent("div")
            if tile is None:
                continue
            prices = [v for v, _ in find_prices(tile.get_text(" ", strip=True))]
            title = a.get("title") or a.get_text(" ", strip=True)
            if not prices or not title:
                continue
            price = min(prices)
            original = max(prices) if max(prices) > price else None
            d = make_deal("boulanger", "Boulanger", title, absolute(a["href"].split("#")[0], base_url), price, original,
                          source_id=mm.group(1), link_host="boulanger.com")
            if d:
                deals[mm.group(1)] = d
    return sort_by_discount(list(deals.values()))


class BoulangerSource(BaseSource):
    name = "boulanger"
    label = "Boulanger"

    def fetch(self) -> list[Deal]:
        base = self.cfg.get("base_url", BASE)
        refurb = bool(self.cfg.get("include_refurbished", False))
        deals: dict[str, Deal] = {}
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"Boulanger {url}")
            if html is None:
                if self.blocked:
                    break      # même protection sur toutes les pages : on n'insiste pas
                continue
            for d in parse_listing(html, base, refurb):
                deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
