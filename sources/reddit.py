"""Reddit (optionnel) : endpoints publics `.json`, puis flux Atom `.rss`, puis HTML `old.reddit.com`."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from models import Deal
from sources.base import BaseSource
from utils import extract_asin, find_prices, html_to_text, url_host


def _prices(title: str) -> tuple[Optional[float], Optional[float], str]:
    prices = find_prices(title)
    if not prices:
        return None, None, "EUR"
    price, cur = prices[0]
    original = next((v for v, c in prices[1:] if c == cur and v > price), None)
    return price, original, cur


def _make(sub: str, pid: str, title: str, link: str, permalink: str, body: str, ts: Optional[float],
          flair: str = "") -> Deal:
    price, original, cur = _prices(title)
    external = link if link and "reddit.com" not in link and "redd.it" not in link else ""
    return Deal(
        source=f"reddit-{sub.lower()}",
        source_id=pid,
        title=title,
        url=permalink,
        site_country="",
        merchant=url_host(external) if external else "",
        merchant_url=external,
        link_host=url_host(external) if external else "",
        price=price,
        original_price=original,
        currency=cur,
        category=flair or "",
        description=body[:1000],
        published_at=ts,
        asin=extract_asin(external, body),
    )


def parse_listing_json(data: dict, sub: str) -> list[Deal]:
    deals = []
    for child in (data.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        if not d.get("id") or not d.get("title") or d.get("stickied"):
            continue
        deals.append(_make(sub, d["id"], d["title"], d.get("url_overridden_by_dest") or d.get("url") or "",
                           "https://www.reddit.com" + d.get("permalink", ""), d.get("selftext") or "",
                           d.get("created_utc"), d.get("link_flair_text") or ""))
    return deals


def parse_atom(xml: str, sub: str) -> list[Deal]:
    soup = BeautifulSoup(xml, "xml")
    deals = []
    for entry in soup.find_all("entry"):
        pid = (entry.find("id").get_text(strip=True) if entry.find("id") else "").replace("t3_", "")
        title = entry.find("title").get_text(strip=True) if entry.find("title") else ""
        link_el = entry.find("link")
        permalink = link_el.get("href", "") if link_el else ""
        content_html = entry.find("content").get_text() if entry.find("content") else ""
        body_soup = BeautifulSoup(content_html, "lxml")
        external = next((a.get("href") for a in body_soup.find_all("a")
                         if a.get_text(strip=True) == "[link]" and a.get("href")), "")
        ts = None
        if entry.find("updated"):
            try:
                ts = datetime.fromisoformat(entry.find("updated").get_text(strip=True)).timestamp()
            except ValueError:
                ts = None
        if pid and title:
            deals.append(_make(sub, pid, title, external, permalink, html_to_text(content_html), ts))
    return deals


def parse_old_html(html: str, sub: str) -> list[Deal]:
    soup = BeautifulSoup(html, "lxml")
    deals = []
    for thing in soup.select("div.thing[data-fullname]"):
        if "stickied" in thing.get("class", []):
            continue
        pid = thing.get("data-fullname", "").replace("t3_", "")
        a = thing.select_one("a.title")
        if not pid or not a:
            continue
        permalink = thing.get("data-permalink", "")
        ts_ms = thing.get("data-timestamp")
        flair = thing.select_one(".linkflairlabel")
        deals.append(_make(sub, pid, a.get_text(" ", strip=True), thing.get("data-url", ""),
                           "https://www.reddit.com" + permalink if permalink.startswith("/") else permalink, "",
                           int(ts_ms) / 1000 if ts_ms and re.fullmatch(r"\d+", ts_ms) else None,
                           flair.get_text(strip=True) if flair else ""))
    return deals


class RedditSource(BaseSource):
    name = "reddit"

    def fetch(self) -> list[Deal]:
        ua = {"User-Agent": self.cfg.get("user_agent", "windows:price-watch-personal:v1.0")}
        limit = int(self.cfg.get("limit", 40))
        order = self.cfg.get("fetch_order", ["json", "rss", "html"])
        deals: list[Deal] = []
        for sub in self.cfg.get("subreddits", []):
            handlers = {
                "json": lambda s=sub: parse_listing_json(
                    self.http.get_json(f"https://www.reddit.com/r/{s}/new.json?limit={limit}&raw_json=1", headers=ua), s),
                "rss": lambda s=sub: parse_atom(
                    self.http.get(f"https://www.reddit.com/r/{s}/new/.rss", headers=ua,
                                  accept="application/atom+xml, application/xml").text, s),
                "html": lambda s=sub: parse_old_html(
                    self.http.get(f"https://old.reddit.com/r/{s}/new/", headers=ua).text, s),
            }
            deals.extend(self.try_modes(order, handlers, f"reddit/r/{sub}"))
        return deals
