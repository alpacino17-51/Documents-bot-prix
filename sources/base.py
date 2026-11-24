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

    def __init__(self, cfg: dict, http: HttpClient, general_cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.http = http
        self.general = general_cfg or {}
        self.log = logging.getLogger(f"source.{self.name}")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def min_interval_minutes(self) -> float:
        return float(self.cfg.get("min_interval_minutes", 0) or 0)

    @abstractmethod
    def fetch(self) -> list[Deal]:
        """Récupère les deals. Ne doit lever que des exceptions inattendues (les erreurs réseau sont gérées)."""

    def safe_fetch(self) -> list[Deal]:
        try:
            deals = self.fetch()
            self.log.info("%d deal(s) récupéré(s)", len(deals))
            return deals
        except FetchError as exc:
            self.log.warning("Source indisponible : %s", exc)
        except Exception:  # un bug de parsing ne doit jamais arrêter le bot
            self.log.exception("Erreur inattendue dans la source %s", self.name)
        return []

    def try_modes(self, order: list[str], handlers: dict[str, Callable[[], list[Deal]]], label: str) -> list[Deal]:
        """Essaie successivement json -> rss -> html (selon `order`) et garde le premier résultat non vide."""
        for mode in order:
            handler = handlers.get(mode)
            if handler is None:
                continue
            try:
                deals = handler()
            except FetchError as exc:
                self.log.warning("%s [%s] : %s", label, mode, exc)
                continue
            except Exception:
                self.log.exception("%s [%s] : erreur de parsing", label, mode)
                continue
            if deals:
                self.log.debug("%s : %d deal(s) via %s", label, len(deals), mode)
                return deals
            self.log.debug("%s : rien via %s", label, mode)
        return []
