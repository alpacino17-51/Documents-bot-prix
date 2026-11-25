"""Recherche web publique légère via DuckDuckGo HTML (https://html.duckduckgo.com/html/).

- Résultats mis en cache (state.db, `web_search.cache_hours`).
- Budget de requêtes par passage + délai minimal entre deux requêtes (respect du service).
- Si DuckDuckGo renvoie sa page « anomalie » (anti-robot), la recherche est suspendue pour le passage
  (aucun contournement) ; le domaine passe aussi en pause automatique via http_client.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from bs4 import BeautifulSoup

from http_client import FetchError

log = logging.getLogger("web_search")

_ANOMALY = ("anomaly-modal", "bots use duckduckgo", "unfortunately, bots", "challenge-form")


def registrable_label(host: str) -> str:
    """'www.newbalance.fr' -> 'newbalance' ; 'shop.bbc.co.uk' -> 'bbc'."""
    parts = [p for p in (host or "").lower().split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "gouv"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for res in soup.select(".result"):
        classes = " ".join(res.get("class", []))
        if "result--ad" in classes:
            continue
        a = res.select_one("a.result__a")
        if a is None:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href if href.startswith("http") else "https:" + href).query)
                           .get("uddg", [""])[0])
        if not href.startswith("http"):
            continue
        snippet = res.select_one(".result__snippet")
        results.append({"url": href, "domain": domain_of(href), "title": a.get_text(" ", strip=True),
                        "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
    return results


class WebSearch:
    def __init__(self, cfg: dict, http, db=None):
        self.cfg = cfg or {}
        self.http = http
        self.db = db
        self.enabled = bool(self.cfg.get("enabled", True))
        self.endpoint = self.cfg.get("endpoint", "https://html.duckduckgo.com/html/")
        self.region = self.cfg.get("region", "fr-fr")
        self.max_queries = int(self.cfg.get("max_queries_per_run", 12))
        self.min_delay = float(self.cfg.get("min_delay_seconds", 4))
        self.cache_hours = float(self.cfg.get("cache_hours", 72))
        self._lock = threading.Lock()
        self._last = 0.0
        self.reset_run()

    def reset_run(self) -> None:
        self.queries = 0
        # pause persistée après une page anti-robot (ne pas réinsister au passage suivant)
        self.blocked = bool(self.db is not None and self.db.paused_for("web_search") > 0)

    @property
    def budget_exhausted(self) -> bool:
        return self.queries >= self.max_queries

    @property
    def available(self) -> bool:
        return self.enabled and not self.blocked and not self.budget_exhausted

    def _pause(self) -> None:
        self.blocked = True
        if self.db is not None:
            self.db.pause_source("web_search", float(self.cfg.get("pause_minutes_after_block", 60)))

    def search(self, query: str) -> Optional[list[dict]]:
        """Liste de résultats, [] si aucun résultat, None si la recherche n'a pas pu être faite."""
        key = f"{self.region}|{query.lower().strip()}"
        if self.db is not None:
            cached = self.db.kv_get("web", key, self.cache_hours)
            if cached is not None:
                return cached
        if not self.available:
            return None
        with self._lock:
            wait = self.min_delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.queries += 1
        url = f"{self.endpoint}?q={quote_plus(query)}&kl={self.region}"
        try:
            html = self.http.get(url, headers={"Referer": "https://html.duckduckgo.com/"}, timeout=15, retries=0).text
        except FetchError as exc:
            if exc.blocked:
                self._pause()
            log.info("Recherche web indisponible (%s)", exc)
            return None
        low = html[:50000].lower()
        if any(a in low for a in _ANOMALY) and "result__a" not in low:
            self._pause()
            log.warning("DuckDuckGo a renvoyé une page anti-robot : recherche web en pause %s min",
                        self.cfg.get("pause_minutes_after_block", 60))
            return None
        results = parse_results(html)
        if self.db is not None:
            self.db.kv_put("web", key, results)
        return results
