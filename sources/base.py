"""Interface commune à toutes les sources."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

from http_client import FetchError, HttpClient
from models import Deal


class BaseSource(ABC):
    #: clé de la section dans config.json -> sources.<name>
    name: str = "base"
    #: libellé lisible dans les logs
    label: str = ""

    def __init__(self, cfg: dict, http: HttpClient, general_cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.http = http
        self.general = general_cfg or {}
        self.log = logging.getLogger(f"source.{self.name}")
        self.db = None
        self.blocked: str = ""          # renseigné si une page a été bloquée (403 / anti-robot) pendant le passage
        self.pages_ok = 0
        self.pages_failed = 0
        self.last_mode = ""

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def min_interval_minutes(self) -> float:
        return float(self.cfg.get("min_interval_minutes", 0) or 0)

    @abstractmethod
    def fetch(self) -> list[Deal]:
        """Récupère les deals. Ne doit lever que des exceptions inattendues (les erreurs réseau sont gérées)."""

    def reset(self) -> None:
        self.blocked, self.pages_ok, self.pages_failed = "", 0, 0

    def note_error(self, exc: FetchError, label: str) -> None:
        self.pages_failed += 1
        if exc.blocked:
            self.blocked = exc.antibot or "403"
            self.log.warning("%s : bloqué (%s) — les autres sources continuent", label,
                             f"page anti-robot {exc.antibot}" if exc.antibot else "HTTP 403")
        else:
            self.log.warning("%s : %s", label, exc)

    def safe_fetch(self) -> list[Deal]:
        self.reset()
        try:
            deals = self.fetch()
        except FetchError as exc:
            self.note_error(exc, self.label or self.name)
            deals = []
        except Exception:  # un bug de parsing ne doit jamais arrêter le bot
            self.log.exception("Erreur inattendue dans la source %s", self.name)
            deals = []
        for d in deals:
            d.origin = self.name
        self.log.info("%d deal(s) récupéré(s)%s", len(deals),
                      f" — {self.pages_failed} page(s) en échec" if self.pages_failed else "")
        return deals

    def get_text(self, url: str, label: str = "", **kwargs) -> Optional[str]:
        """GET tolérant : renvoie None (et journalise proprement) en cas d'échec."""
        try:
            text = self.http.get(url, **kwargs).text
            self.pages_ok += 1
            return text
        except FetchError as exc:
            self.note_error(exc, label or url)
            return None

    def try_modes(self, order: list[str], handlers: dict[str, Callable[[], list[Deal]]], label: str) -> list[Deal]:
        """Essaie successivement json -> rss -> html (selon `order`) et garde le premier résultat non vide."""
        self.last_mode = ""
        for mode in order:
            handler = handlers.get(mode)
            if handler is None:
                continue
            try:
                deals = handler()
                self.pages_ok += 1
            except FetchError as exc:
                self.note_error(exc, f"{label} [{mode}]")
                continue
            except Exception:
                self.log.exception("%s [%s] : erreur de parsing", label, mode)
                continue
            if deals:
                self.log.debug("%s : %d deal(s) via %s", label, len(deals), mode)
                self.last_mode = mode
                return deals
            self.log.debug("%s : rien via %s", label, mode)
        return []
