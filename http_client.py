"""Client HTTP basé sur curl_cffi (empreinte TLS/HTTP2 d'un Chrome standard).

- Proxy et en-têtes supplémentaires lus UNIQUEMENT via variables d'environnement (voir settings.Secrets).
- Délai poli aléatoire entre deux requêtes vers un même domaine.
- Nouvelles tentatives sur erreurs réseau, 429 et 5xx (pas sur 403/404 : inutile d'insister).
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: Optional[int] = None, antibot: str = ""):
        super().__init__(f"{message} [{status or '-'}] {url}")
        self.url = url
        self.status = status
        self.antibot = antibot

    @property
    def blocked(self) -> bool:
        return self.status in (403, 429) or bool(self.antibot)


_ANTIBOT_SIGNATURES = (
    ("DataDome", ("captcha-delivery.com", "datadome")),
    ("Akamai", ("errors.edgesuite.net", "akamai bot manager", "_abck")),
    ("Cloudflare", ("cf-chl-", "challenge-platform", "just a moment...", "cf-browser-verification")),
    ("PerimeterX", ("px-captcha", "_pxhd", "perimeterx")),
)


def detect_antibot(text: str) -> str:
    """Nom de la protection anti-robot si la page reçue est une page d'attente / captcha, sinon ''."""
    if not text or len(text) > 60_000:   # une vraie page catalogue est bien plus grosse
        return ""
    head = text.lstrip()[:300].lower()
    if head.startswith(("{", "[", "<?xml", "<rss", "<feed")):   # JSON / RSS : jamais une page captcha
        return ""
    low = text.lower()
    for name, needles in _ANTIBOT_SIGNATURES:
        if any(n in low for n in needles):
            return name
    if "access denied" in low and "reference #" in low:
        return "Akamai"
    return ""


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
        self._session = session                 # session imposée (tests) : partagée par tous les threads
        self._local = threading.local()         # sinon une session curl par thread (curl n'est pas thread-safe)
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "errors": 0, "blocked": 0}
        # pause automatique par domaine (persistée dans state.db) après des 403/429 consécutifs
        self.cooldown_store = None
        self.cooldown_after = int(self.cfg.get("domain_cooldown_after_failures", 2))
        self.cooldown_minutes = float(self.cfg.get("domain_cooldown_minutes", 60))

    def reset_run(self) -> None:
        """À appeler au début de chaque passage : remet à zéro les compteurs et les hôtes en panne."""
        with self._lock:
            self._down_hosts.clear()
            self.stats = {"requests": 0, "errors": 0, "blocked": 0}

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    # -- session paresseuse (permet d'injecter un faux client dans les tests)
    @property
    def session(self):
        if self._session is not None:
            return self._session
        sess = getattr(self._local, "session", None)
        if sess is None:
            from curl_cffi import requests as cffi_requests

            kwargs: dict[str, Any] = {"impersonate": self.impersonate}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            sess = self._local.session = cffi_requests.Session(**kwargs)
        return sess

    def _polite_wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        with self._lock:
            last = self._last_hit.get(host)
            now = time.monotonic()
            wait = 0.0
            if last is not None:
                wait = max(0.0, random.uniform(self.min_delay, self.max_delay) - (now - last))
            self._last_hit[host] = now + wait       # réserve le créneau avant de dormir (threads concurrents)
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _domain(host: str) -> str:
        return host[4:] if host.startswith("www.") else host

    def _check_cooldown(self, url: str, host: str) -> None:
        if self.cooldown_store is None:
            return
        left = self.cooldown_store.domain_cooldown_left(self._domain(host))
        if left > 0:
            raise FetchError(url, f"domaine en pause automatique ({left:.0f} min restantes)", antibot="cooldown")

    def _record_block(self, host: str) -> None:
        if self.cooldown_store is None:
            return
        if self.cooldown_store.domain_failure(self._domain(host), self.cooldown_after, self.cooldown_minutes):
            log.warning("%s : %d blocages consécutifs -> domaine mis en pause %.0f min", self._domain(host),
                        self.cooldown_after, self.cooldown_minutes)

    def _record_ok(self, host: str) -> None:
        if self.cooldown_store is not None:
            self.cooldown_store.domain_success(self._domain(host))

    def resolve_redirect(self, url: str, timeout: float = 10.0) -> str:
        """Suit les redirections (ex. lien Pepper /visit/...) et renvoie l'URL finale, même si la page finale
        répond 403 (le marchand a pu bloquer la page, mais son adresse est connue). '' en cas d'échec réseau."""
        host = urlsplit(url).hostname or ""
        if host in self._down_hosts:
            return ""
        self._polite_wait(url)
        self._count("requests")
        try:
            resp = self.session.request("GET", url, headers=self._headers(None, None),
                                        timeout=(min(5.0, timeout), timeout), allow_redirects=True)
        except Exception as exc:
            log.info("Résolution du lien %s impossible (%s)", url, type(exc).__name__)
            return ""
        final = str(resp.url)
        if (urlsplit(final).hostname or "") == host:
            # redirection en JavaScript / meta refresh restée sur la page intermédiaire
            m = re.search(r'(?:http-equiv=["\']?refresh["\']?[^>]*url=|window\.location(?:\.href)?\s*=\s*["\'])'
                          r'(https?://[^"\'>\s]+)', resp.text[:30000], re.I)
            return m.group(1) if m else ""
        return final

    def _headers(self, accept: Optional[str], extra: Optional[dict]) -> dict[str, str]:
        headers = {"Accept-Language": self.cfg.get("accept_language", "fr-FR,fr;q=0.9,en;q=0.7")}
        if accept:
            headers["Accept"] = accept
        headers.update(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, url: str, *, accept: Optional[str] = None, headers: Optional[dict] = None,
                json_body: Any = None, data: Any = None, polite: bool = True, timeout: Optional[float] = None,
                retries: Optional[int] = None) -> FetchResult:
        """`timeout` / `retries` : valeurs propres à cet appel (ex. vérifications secondaires rapides)."""
        read_timeout = float(timeout) if timeout else self.timeout
        max_retries = self.max_retries if retries is None else int(retries)
        host = urlsplit(url).hostname or ""
        if host in self._down_hosts:
            raise FetchError(url, "hôte ignoré pour ce passage (erreurs réseau répétées)")
        self._check_cooldown(url, host)
        attempt = 0
        while True:
            attempt += 1
            if polite:
                self._polite_wait(url)
            self._count("requests")
            try:
                resp = self.session.request(method, url, headers=self._headers(accept, headers), json=json_body,
                                            data=data, timeout=(min(self.connect_timeout, read_timeout), read_timeout),
                                            allow_redirects=True)
            except Exception as exc:  # erreurs réseau curl (DNS, timeout, reset TLS...)
                self._count("errors")
                if attempt <= max_retries:
                    backoff = 2 ** attempt + random.random()
                    log.warning("Erreur réseau (%s) sur %s, nouvel essai dans %.0fs", type(exc).__name__, url, backoff)
                    time.sleep(backoff)
                    continue
                with self._lock:
                    self._down_hosts.add(host)   # on n'insiste pas sur ce site jusqu'au prochain passage
                raise FetchError(url, f"erreur réseau ({type(exc).__name__}), site ignoré jusqu'au prochain passage") \
                    from exc

            status = int(resp.status_code)
            if status in (429, 500, 502, 503, 504) and attempt <= max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                backoff = float(retry_after) if retry_after.isdigit() else 3 * attempt + random.random() * 2
                log.warning("HTTP %s sur %s, nouvel essai dans %.0fs", status, url, backoff)
                time.sleep(min(backoff, 60))
                continue
            text = resp.text
            antibot = detect_antibot(text) if (status == 403 or status < 300) else ""
            if status in (403, 429) or antibot:
                self._count("blocked")
                self._record_block(host)
                what = (f"page anti-robot {antibot}" if antibot else "trop de requêtes (429)" if status == 429
                        else "accès refusé (protection anti-robot ou IP de datacenter)")
                raise FetchError(url, what, status, antibot=antibot)
            if status >= 400:
                self._count("errors")
                raise FetchError(url, f"HTTP {status}", status)
            self._record_ok(host)
            return FetchResult(url=str(resp.url), status=status, text=text,
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
