"""Dealabs : flux « nouveaux » et « hot », groupe « Erreur de prix », pages marchands, topics de suivi."""
from __future__ import annotations

from models import Deal
from sources.base import BaseSource
from sources.pepper_common import parse_listing_html, parse_listing_json, parse_rss, parse_topic_comments
from utils import normalize_text


def page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    return f"{url}{'&' if '?' in url else '?'}page={page}"


class DealabsSource(BaseSource):
    name = "dealabs"
    label = "Dealabs"

    def _site(self) -> dict:
        return {
            "name": "dealabs",
            "country": self.cfg.get("country", "FR"),
            "base_url": self.cfg.get("base_url", "https://www.dealabs.com"),
            "deal_path": self.cfg.get("deal_path", "bons-plans"),
            "visit_path": self.cfg.get("visit_path", "visit/homenew"),
            "currency": "EUR",
        }

    def fetch_feed(self, feed: dict) -> list[Deal]:
        site = self._site()
        ctx = bool(feed.get("price_error_context", False))
        page, rss_url = feed.get("page_url"), feed.get("rss_url")
        handlers = {}
        if page:
            handlers["json"] = lambda: parse_listing_json(self.http.get(page).text, site, "dealabs", ctx)
            handlers["html"] = lambda: parse_listing_html(self.http.get(page).text, site, "dealabs", ctx)
        if rss_url:
            handlers["rss"] = lambda: parse_rss(self.http.get(rss_url, accept="application/rss+xml, application/xml, text/xml").text,
                                                site, "dealabs", ctx)
        order = self.cfg.get("fetch_order", ["json", "rss", "html"])
        deals = self.try_modes(order, handlers, f"Dealabs/{feed.get('name')}")

        # pages suivantes (JSON embarqué uniquement) pour ne rien manquer entre deux passages
        pages = int(feed.get("pages", self.cfg.get("pages_per_feed", 2))) if feed.get("paginate") else 1
        for n in range(2, pages + 1):
            if not page or not deals or self.last_mode != "json":
                break
            html = self.get_text(page_url(page, n), f"Dealabs/{feed.get('name')} page {n}")
            more = parse_listing_json(html, site, "dealabs", ctx) if html else []
            if not more:
                break
            deals.extend(more)
        return deals

    def fetch_merchant_pages(self) -> list[Deal]:
        """Deals récents d'enseignes clés (recherche Dealabs filtrée sur le nom du marchand)."""
        site = self._site()
        deals: list[Deal] = []
        for mp in self.cfg.get("merchant_pages", []):
            url = mp.get("url")
            if not url or mp.get("enabled", True) is False:
                continue
            html = self.get_text(url, f"Dealabs/marchand {mp.get('name')}")
            if not html:
                continue
            wanted = normalize_text(mp.get("merchant_filter") or mp.get("name", ""))
            found = [d for d in parse_listing_json(html, site, "dealabs") if not wanted or wanted in normalize_text(d.merchant)]
            self.log.debug("Dealabs/marchand %s : %d deal(s)", mp.get("name"), len(found))
            deals.extend(found)
        return deals

    def fetch_topics(self) -> list[Deal]:
        site = self._site()
        deals: list[Deal] = []
        for topic in self.cfg.get("topics", []):
            url = topic.get("url")
            if not url:
                continue
            html = self.get_text(url, f"Dealabs/topic {topic.get('name')}")
            if not html:
                continue
            found = parse_topic_comments(html, topic, site, "dealabs-topic")
            if not found:
                self.log.debug("Topic %s : aucun commentaire présent dans le HTML (chargés en JavaScript)",
                               topic.get("name"))
            deals.extend(found)
        return deals

    def _due(self, key: str, minutes: float) -> bool:
        return self.db is None or self.db.source_due(key, minutes)

    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        index: dict[str, Deal] = {}

        def add(items: list[Deal]) -> None:
            for d in items:
                existing = index.get(d.source_id)
                if existing is not None:
                    # même deal dans plusieurs flux : on garde le contexte « erreur de prix » s'il existe
                    existing.price_error_context = existing.price_error_context or d.price_error_context
                    continue
                index[d.source_id] = d
                deals.append(d)

        for feed in self.cfg.get("feeds", []):
            add(self.fetch_feed(feed))

        mp_interval = float(self.cfg.get("merchant_pages_min_interval_minutes", 10))
        if self.cfg.get("merchant_pages") and self._due("dealabs-merchants", mp_interval):
            add(self.fetch_merchant_pages())
            if self.db is not None:
                self.db.mark_source_run("dealabs-merchants")

        interval = float(self.cfg.get("topics_min_interval_minutes", 15))
        if self.cfg.get("topics_enabled", True) and self._due("dealabs-topics", interval):
            add(self.fetch_topics())
            if self.db is not None:
                self.db.mark_source_run("dealabs-topics")
        return deals
