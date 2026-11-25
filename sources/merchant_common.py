"""Outils communs aux sources « enseignes » (Decathlon, Boulanger, Fnac, Zalando...).

Ordre de préférence : JSON-LD (schema.org Product / ItemList) et JSON embarqués -> HTML des fiches produit.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

from bs4 import BeautifulSoup

from models import Deal
from utils import extract_ean, parse_price


def iter_jsonld(soup_or_html) -> Iterator[dict]:
    soup = soup_or_html if isinstance(soup_or_html, BeautifulSoup) else BeautifulSoup(soup_or_html, "lxml")
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def _offer_prices(offers: Any) -> tuple[Optional[float], Optional[float]]:
    """(prix, prix barré) depuis un bloc schema.org `offers`."""
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, None
    price = offers.get("price") or offers.get("lowPrice")
    price = parse_price(str(price))[0] if price is not None else None
    original = None
    spec = offers.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []:
        if "strikethrough" in str(s.get("priceType", "")).lower() or "listprice" in str(s.get("priceType", "")).lower():
            original = parse_price(str(s.get("price")))[0]
    if original is None and offers.get("highPrice") and price is not None:
        hp = parse_price(str(offers["highPrice"]))[0]
        original = hp if hp and hp > price and offers.get("@type") != "AggregateOffer" else None
    return price, original


def jsonld_products(html: str, base_url: str) -> list[dict]:
    """Produits décrits en JSON-LD (Product, ou ItemList contenant des Product avec prix)."""
    products = []
    for node in iter_jsonld(html):
        candidates = []
        if node.get("@type") == "Product":
            candidates.append(node)
        elif node.get("@type") == "ItemList":
            for el in node.get("itemListElement") or []:
                item = el.get("item") if isinstance(el, dict) else None
                if isinstance(item, dict) and item.get("@type") == "Product":
                    candidates.append(item)
        for p in candidates:
            price, original = _offer_prices(p.get("offers"))
            if price is None:
                continue
            offers = p.get("offers")
            url = p.get("url") or (offers.get("url") if isinstance(offers, dict) else "") or ""
            brand = p.get("brand")
            products.append({
                "title": p.get("name", ""),
                "url": absolute(url or "", base_url),
                "price": price,
                "original": original,
                "sku": str(p.get("sku") or p.get("productID") or ""),
                "gtin": str(p.get("gtin13") or p.get("gtin") or p.get("gtin14") or ""),
                "mpn": str(p.get("mpn") or ""),
                "brand": brand.get("name", "") if isinstance(brand, dict) else str(brand or ""),
                "category": str(p.get("category") or ""),
            })
    return products


def absolute(url: str, base_url: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    return base_url.rstrip("/") + "/" + url.lstrip("/")


def percent(text: str) -> Optional[float]:
    m = re.search(r"-?\s?(\d{1,2}(?:[.,]\d+)?)\s?%", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def make_deal(source: str, merchant: str, title: str, url: str, price: Optional[float],
              original: Optional[float] = None, pct: Optional[float] = None, *, source_id: str = "",
              category: str = "", category_hint: str = "", ean: str = "", mpn: str = "", link_host: str = "",
              merchant_ref: Optional[dict] = None, description: str = "") -> Optional[Deal]:
    if not title or not url or price is None or price <= 0:
        return None
    if original is not None and original <= price:
        original = None
    if original is None and pct and 0 < pct < 100:
        original = round(price / (1 - pct / 100), 2)
    return Deal(
        source=source,
        source_id=source_id or url,
        title=re.sub(r"\s+", " ", title).strip(),
        url=url,
        site_country="FR",
        merchant=merchant,
        merchant_url=url,
        link_host=link_host,
        price=round(price, 2),
        original_price=original,
        currency="EUR",
        category=category,
        category_hint=category_hint,
        description=description,
        ean=ean if ean and extract_ean(f"EAN {ean}") else "",
        mpn=mpn,
        displayed_discount_override=pct if pct and pct > 0 else None,
        merchant_ref=merchant_ref or {},
    )


def sort_by_discount(deals: list[Deal]) -> list[Deal]:
    return sorted(deals, key=lambda d: d.displayed_discount_pct or 0, reverse=True)


def card_text_price(el) -> Optional[float]:
    return parse_price(el.get_text(" ", strip=True))[0] if el is not None else None
