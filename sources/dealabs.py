"""Dealabs : flux « nouveaux » et « hot », groupe « Erreur de prix », topics de suivi des erreurs de prix."""
from __future__ import annotations

from models import Deal
from sources.base import BaseSource
from sources.pepper_common import parse_listing_html, parse_listing_json, parse_rss, parse_topic_comments


class DealabsSource(BaseSource):
    name = "dealabs"

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
        page_url, rss_url = feed.get("page_url"), feed.get("rss_url")
        handlers = {}
        if page_url:
            handlers["json"] = lambda: parse_listing_json(self.http.get(page_url).text, site, "dealabs", ctx)
            handlers["html"] = lambda: parse_listing_html(self.http.get(page_url).text, site, "dealabs", ctx)
        if rss_url:
            handlers["rss"] = lambda: parse_rss(self.http.get(rss_url, accept="application/rss+xml, application/xml, text/xml").text,
                                                site, "dealabs", ctx)
        order = self.cfg.get("fetch_order", ["json", "rss", "html"])
        return self.try_modes(order, handlers, f"Dealabs/{feed.get('name')}")

    def fetch_topics(self) -> list[Deal]:
        site = self._site()
        deals: list[Deal] = []
        for topic in self.cfg.get("topics", []):
            url = topic.get("url")
            if not url:
                continue
            html = self.http.get(url).text
            found = parse_topic_comments(html, topic, site, "dealabs-topic")
            if not found:
                self.log.debug("Topic %s : aucun commentaire présent dans le HTML (chargés en JavaScript)",
                               topic.get("name"))
            deals.extend(found)
        return deals

    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        seen: set[str] = set()
        for feed in self.cfg.get("feeds", []):
            for d in self.fetch_feed(feed):
                if d.source_id in seen:
                    # même deal dans plusieurs flux : on garde le contexte « erreur de prix » s'il existe
                    if d.price_error_context:
                        for existing in deals:
                            if existing.source_id == d.source_id:
                                existing.price_error_context = True
                    continue
                seen.add(d.source_id)
                deals.append(d)

        db = getattr(self, "db", None)
        interval = float(self.cfg.get("topics_min_interval_minutes", 15))
        if self.cfg.get("topics_enabled", True) and (db is None or db.source_due("dealabs-topics", interval)):
            try:
                deals.extend(self.fetch_topics())
            finally:
                if db is not None:
                    db.mark_source_run("dealabs-topics")
        return deals
