"""Étape B : prix public sur le site officiel de la marque (cache 24 h par produit).

Recherche `site:<domaine officiel> <modèle>`, ouvre la 1re page produit pertinente du domaine et lit le prix :
JSON-LD `Offer.price` -> balises meta (`product:price:amount`, `og:price:amount`, itemprop=price) -> prix affiché.
La page n'est retenue que si son nom de produit ressemble au titre du deal (évite un accessoire / autre modèle).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.merchant_common import jsonld_products
from utils import normalize_text, parse_price, search_query_from_title, title_hash, title_similarity
from web_search import WebSearch, domain_of

log = logging.getLogger("official_price")


def extract_official_price(html: str, url: str = "") -> tuple[Optional[float], str]:
    """(prix, nom du produit) lus sur une page produit."""
    products = jsonld_products(html, url or "https://x")
    if products:
        return products[0]["price"], products[0]["title"]
    soup = BeautifulSoup(html, "lxml")
    name_el = soup.select_one("meta[property='og:title']") or soup.select_one("h1")
    name = (name_el.get("content") if name_el and name_el.name == "meta" else name_el.get_text(" ", strip=True)
            if name_el else "") or ""
    for sel in ("meta[property='product:price:amount']", "meta[property='og:price:amount']",
                "meta[itemprop='price']", "[itemprop='price'][content]"):
        el = soup.select_one(sel)
        if el and el.get("content"):
            value = parse_price(el["content"])[0]
            if value:
                return value, name
    el = soup.select_one("[itemprop='price'], .product-price, .price, [data-price]")
    if el is not None:
        value = parse_price(el.get("data-price") or el.get_text(" ", strip=True))[0]
        if value:
            return value, name
    return None, name


class OfficialPriceFinder:
    def __init__(self, cfg: dict, http, web: Optional[WebSearch], db=None):
        self.cfg = cfg or {}
        self.http = http
        self.web = web
        self.db = db
        self.min_similarity = float(self.cfg.get("min_title_similarity", 0.5))
        self.cache_hours = float(self.cfg.get("cache_hours", 24))

    def find(self, deal: Deal, domain: str, brand: str = "") -> Optional[dict]:
        """{'price', 'url', 'title', 'domain'} ou None (introuvable / recherche impossible)."""
        if not domain or not self.cfg.get("enabled", True) or self.http is None:
            return None
        key = f"{domain}|{deal.mpn or title_hash(deal.title)}"
        if self.db is not None:
            cached = self.db.kv_get("official", key, self.cache_hours)
            if cached is not None:
                return cached or None
        if self.web is None:
            return None
        model = search_query_from_title(deal.title)
        brand_norm = normalize_text(brand)
        model = " ".join(w for w in model.split() if normalize_text(w) not in brand_norm.split()) or model
        results = self.web.search(f"site:{domain} {deal.mpn or model}")
        if results is None:
            return None                       # pas de cache : on réessaiera
        found = None
        for r in [r for r in results if r["domain"].endswith(domain)][:2]:
            try:
                res = self.http.get(r["url"], timeout=float(self.cfg.get("timeout_seconds", 10)), retries=0)
            except FetchError as exc:
                log.info("Page officielle %s inaccessible (%s)", r["url"], exc)
                continue
            price, name = extract_official_price(res.text, res.url)
            sim = title_similarity(deal.title, name or r["title"])
            if price and sim >= self.min_similarity:
                found = {"price": price, "url": res.url, "title": name or r["title"], "domain": domain_of(res.url),
                         "similarity": sim}
                break
        if self.db is not None:
            self.db.kv_put("official", key, found or {})
        return found
