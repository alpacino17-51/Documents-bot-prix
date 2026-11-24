"""CamelCamelCamel : flux « Top Price Drops » (fr/de/it/es) + statistiques d'historique produit Amazon."""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.base import BaseSource
from utils import find_prices, normalize_text, parse_price

AMAZON_TLD = {"fr": "fr", "de": "de", "it": "it", "es": "es", "uk": "co.uk", "com": "com"}

_DROP_TITLE = re.compile(
    r"^(?P<title>.*?)\s+-\s+(?:down|baisse de|gesunken um|bajada del?|ribasso del?|-)\s*(?P<pct>[\d.,]+)\s*%"
    r"\s*\((?P<delta>[^)]*)\)\s*(?:to|à|a|auf|al)\s+(?P<price>.+?)\s+(?:from|au lieu de|de|von|desde|da)\s+(?P<orig>.+?)\s*$",
    re.I,
)
_ASIN_IN_URL = re.compile(r"/product/([A-Z0-9]{10})", re.I)

_LABELS = {
    "current": ("current", "actuel", "aktuell", "actual", "attuale"),
    "highest": ("highest", "plus haut", "hochst", "mas alto", "piu alto"),
    "lowest": ("lowest", "plus bas", "niedrigst", "mas bajo", "piu basso"),
    "average": ("average", "moyen", "durchschnitt", "promedio", "media"),
}


def amazon_url(asin: str, locale: str) -> str:
    return f"https://www.amazon.{AMAZON_TLD.get(locale, locale)}/dp/{asin}"


def parse_drop_title(raw: str) -> Optional[dict]:
    m = _DROP_TITLE.match(raw.strip())
    if not m:
        return None
    price, cur = parse_price(m.group("price"), "EUR")
    orig, _ = parse_price(m.group("orig"), cur or "EUR")
    try:
        pct = float(m.group("pct").replace(",", "."))
    except ValueError:
        pct = None
    return {"title": m.group("title").strip(), "price": price, "original": orig, "pct": pct, "currency": cur or "EUR"}


def parse_feed(xml: str, locale: str) -> list[Deal]:
    soup = BeautifulSoup(xml, "xml")
    deals = []
    for item in soup.find_all("item"):
        raw_title = item.find("title").get_text(strip=True) if item.find("title") else ""
        link = item.find("link").get_text(strip=True) if item.find("link") else ""
        m = _ASIN_IN_URL.search(link)
        info = parse_drop_title(raw_title)
        if not m or not info:
            continue
        asin = m.group(1).upper()
        deals.append(_make_deal(asin, locale, info, link))
    return deals


def parse_top_drops_html(html: str, locale: str) -> list[Deal]:
    """Secours : page HTML top_drops (une ligne par produit, prix actuel puis ancien prix)."""
    soup = BeautifulSoup(html, "lxml")
    deals: dict[str, Deal] = {}
    for a in soup.select("a[href*='/product/']"):
        m = _ASIN_IN_URL.search(a.get("href", ""))
        if not m or m.group(1).upper() in deals:
            continue
        row = a.find_parent(["tr", "li"]) or a.find_parent("div")
        if row is None:
            continue
        text = row.get_text(" ", strip=True)
        title = a.get("title") or a.get_text(" ", strip=True)
        if not title:
            continue
        info = parse_drop_title(text)
        if not info:
            prices = find_prices(text)
            if not prices:
                continue
            price = prices[-2][0] if len(prices) >= 3 else prices[0][0]
            orig = max(v for v, _ in prices)
            pct_m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
            info = {"title": title, "price": price, "original": orig if orig > price else None,
                    "pct": float(pct_m.group(1).replace(",", ".")) if pct_m else None, "currency": prices[0][1]}
        info["title"] = title
        href = a.get("href", "")
        asin = m.group(1).upper()
        deals[asin] = _make_deal(asin, locale, info,
                                 href if href.startswith("http") else f"https://{locale}.camelcamelcamel.com{href}")
    return list(deals.values())


def _make_deal(asin: str, locale: str, info: dict, camel_link: str) -> Deal:
    return Deal(
        source=f"camelcamelcamel-{locale}",
        source_id=asin,
        title=info["title"],
        url=amazon_url(asin, locale),
        site_country=locale.upper(),
        merchant=f"Amazon.{AMAZON_TLD.get(locale, locale)}",
        merchant_url=amazon_url(asin, locale),
        link_host=f"amazon.{AMAZON_TLD.get(locale, locale)}",
        price=info.get("price"),
        original_price=info.get("original") if info.get("original") and info.get("price")
        and info["original"] > info["price"] else None,
        currency=info.get("currency") or "EUR",
        asin=asin,
        description=f"Historique : {camel_link.split('?')[0]}",
        displayed_discount_override=info.get("pct"),
    )


def parse_product_stats(html: str) -> dict:
    """Statistiques de la 1re table de prix (Amazon) d'une page produit camelcamelcamel."""
    soup = BeautifulSoup(html, "lxml")
    stats: dict[str, Optional[float]] = {}
    tables = soup.select("table.product_pane") or soup.select("table")
    for table in tables:
        for tr in table.select("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = normalize_text(cells[0].get_text(" ", strip=True))
            classes = " ".join(tr.get("class", []))
            price = None
            for cell in cells[1:]:
                value, _ = parse_price(cell.get_text(" ", strip=True))
                if value is not None and find_prices(cell.get_text(" ", strip=True)):
                    price = value
                    break
            if price is None:
                continue
            for key, words in _LABELS.items():
                if key not in stats and (any(w in label for w in words) or key in classes):
                    stats[key] = price
                    break
        if stats:
            break       # 1re table = prix Amazon (les suivantes = tiers neuf / occasion)
    return stats


class CamelSource(BaseSource):
    name = "camelcamelcamel"

    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        order = self.cfg.get("fetch_order", ["rss", "html"])
        for locale in self.cfg.get("locales", ["fr"]):
            feed_url = self.cfg.get("feed_url_template", "https://{locale}.camelcamelcamel.com/top_drops/feed").format(locale=locale)
            page_url = self.cfg.get("page_url_template", "https://{locale}.camelcamelcamel.com/top_drops").format(locale=locale)
            handlers = {
                "rss": lambda u=feed_url, l=locale: parse_feed(
                    self.http.get(u, accept="application/rss+xml, application/xml, text/xml").text, l),
                "html": lambda u=page_url, l=locale: parse_top_drops_html(self.http.get(u).text, l),
            }
            deals.extend(self.try_modes(order, handlers, f"camel/{locale}"))
        return deals

    def history(self, asin: str, locale: str = "fr") -> Optional[dict]:
        """Retourne {current, lowest, highest, average} (prix Amazon) ou None si indisponible."""
        if not self.cfg.get("history_lookup_enabled", True) or not asin:
            return None
        url = self.cfg.get("product_url_template", "https://{locale}.camelcamelcamel.com/product/{asin}").format(
            locale=locale, asin=asin)
        try:
            stats = parse_product_stats(self.http.get(url).text)
        except FetchError as exc:
            self.log.info("Historique camel indisponible pour %s : %s", asin, exc)
            return None
        if not stats:
            return None
        stats["url"] = url
        return stats
