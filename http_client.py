"""Client HTTP basé sur curl_cffi (empreinte TLS/HTTP2 d'un Chrome standard).

- Proxy et en-têtes supplémentaires lus UNIQUEMENT via variables d'environnement (voir settings.Secrets).
- Délai poli aléatoire entre deux requêtes vers un même domaine.
- Nouvelles tentatives sur erreurs réseau, 429 et 5xx (pas sur 403/404 : inutile d'insister).
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: Optional[int] = None):
        super().__init__(f"{message} [{status or '-'}] {url}")
        self.url = url
        self.status = status


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient:
    def __init__(self, http_cfg: dict, secrets=None, session=None):
        self.cfg = http_cfg or {}
        self.timeout = float(self.cfg.get("timeout_seconds", 25))
        self.max_retries = int(self.cfg.get("max_retries", 2))
        self.min_delay = float(self.cfg.get("min_delay_seconds", 2.0))
        self.max_delay = float(self.cfg.get("max_delay_seconds", 4.5))
        self.impersonate = self.cfg.get("impersonate", "chrome")
        self.proxy = getattr(secrets, "proxy_url", "") if secrets else ""
        self.extra_headers = dict(getattr(secrets, "extra_headers", {}) or {}) if secrets else {}
        self.connect_timeout = min(10.0, self.timeout)
        self._last_hit: dict[str, float] = {}
        self._down_hosts: set[str] = set()
        self._session = session
        self.stats = {"requests": 0, "errors": 0, "blocked": 0}

    def reset_run(self) -> None:
        """À appeler au début de chaque passage : remet à zéro les compteurs et les hôtes en panne."""
        self._down_hosts.clear()
        self.stats = {"requests": 0, "errors": 0, "blocked": 0}

    # -- session paresseuse (permet d'injecter un faux client dans les tests)
    @property
    def session(self):
        if self._session is None:
            from curl_cffi import requests as cffi_requests

            kwargs: dict[str, Any] = {"impersonate": self.impersonate}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = cffi_requests.Session(**kwargs)
        return self._session

    def _polite_wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        last = self._last_hit.get(host)
        if last is not None:
            wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _headers(self, accept: Optional[str], extra: Optional[dict]) -> dict[str, str]:
        headers = {"Accept-Language": self.cfg.get("accept_language", "fr-FR,fr;q=0.9,en;q=0.7")}
        if accept:
            headers["Accept"] = accept
        headers.update(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, url: str, *, accept: Optional[str] = None, headers: Optional[dict] = None,
                json_body: Any = None, data: Any = None, polite: bool = True) -> FetchResult:
        host = urlsplit(url).hostname or ""
        if host in self._down_hosts:
            raise FetchError(url, "hôte ignoré pour ce passage (erreurs réseau répétées)")
        attempt = 0
        while True:
            attempt += 1
            if polite:
                self._polite_wait(url)
            self.stats["requests"] += 1
            try:
                resp = self.session.request(method, url, headers=self._headers(accept, headers), json=json_body,
                                            data=data, timeout=(self.connect_timeout, self.timeout),
                                            allow_redirects=True)
            except Exception as exc:  # erreurs réseau curl (DNS, timeout, reset TLS...)
                self.stats["errors"] += 1
                if attempt <= self.max_retries:
                    backoff = 2 ** attempt + random.random()
                    log.warning("Erreur réseau (%s) sur %s, nouvel essai dans %.0fs", type(exc).__name__, url, backoff)
                    time.sleep(backoff)
                    continue
                self._down_hosts.add(host)   # on n'insiste pas sur ce site jusqu'au prochain passage
                raise FetchError(url, f"erreur réseau ({type(exc).__name__}), site ignoré jusqu'au prochain passage") \
                    from exc

            status = int(resp.status_code)
            if status in (429, 500, 502, 503, 504) and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                backoff = float(retry_after) if retry_after.isdigit() else 3 * attempt + random.random() * 2
                log.warning("HTTP %s sur %s, nouvel essai dans %.0fs", status, url, backoff)
                time.sleep(min(backoff, 60))
                continue
            if status == 403:
                self.stats["blocked"] += 1
                raise FetchError(url, "accès refusé (403) : protection anti-robot ou IP de datacenter", status)
            if status >= 400:
                self.stats["errors"] += 1
                raise FetchError(url, f"HTTP {status}", status)
            return FetchResult(url=str(resp.url), status=status, text=resp.text,
                               headers={k: v for k, v in resp.headers.items()})

    def get(self, url: str, **kwargs) -> FetchResult:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs) -> Any:
        kwargs.setdefault("accept", "application/json, text/plain, */*")
        res = self.get(url, **kwargs)
        try:
            return res.json()
        except json.JSONDecodeError as exc:
            raise FetchError(url, "réponse non JSON", res.status) from exc

    def post(self, url: str, **kwargs) -> FetchResult:
        return self.request("POST", url, **kwargs)
