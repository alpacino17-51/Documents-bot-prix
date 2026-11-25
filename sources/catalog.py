"""Sources pilotées par la section `sites_catalog` de config.json.

Chaque entrée de `sites_catalog.sites` décrit UN site et le MOTEUR qui sait le lire :
    {"id": "asphaltgold", "engine": "shopify", "enabled": true, "min_interval_minutes": 15,
     "base_url": "https://www.asphaltgold.com", ...}
Le moteur est une classe héritant de `CatalogSource` (elle-même `BaseSource`) : ajouter un site déjà couvert par un
moteur existant = ajouter quelques lignes de configuration, sans nouveau fichier Python.
"""
from __future__ import annotations

import logging
from typing import Optional

from sources.base import BaseSource


class CatalogSource(BaseSource):
    """Base des moteurs du catalogue : le nom de la source est l'`id` de l'entrée de configuration."""
    engine = "base"

    def __init__(self, cfg: dict, http, general_cfg: Optional[dict] = None):
        merged = dict(cfg or {})
        super().__init__(merged, http, general_cfg)
        self.name = merged.get("id") or self.engine
        self.label = merged.get("label") or self.name
        self.log = logging.getLogger(f"source.{self.name}")

    @property
    def base_url(self) -> str:
        return (self.cfg.get("base_url") or "").rstrip("/")

    @property
    def country(self) -> str:
        return self.cfg.get("country", "FR")

    @property
    def currency(self) -> str:
        return self.cfg.get("currency", "EUR")


def catalog_engines() -> dict[str, type[CatalogSource]]:
    # imports tardifs : les moteurs importent eux-mêmes `CatalogSource`
    from sources.comparators_drops import GeizhalsDealsSource, IdealoDealsSource
    from sources.galaxus import GalaxusSource
    from sources.retailer_generic import RetailerSource
    from sources.sneakers_generic import SneakersGenericSource
    from sources.tradeinn import TradeinnSource

    return {
        "retailer": RetailerSource,
        "idealo_deals": IdealoDealsSource,
        "geizhals_deals": GeizhalsDealsSource,
        "tradeinn": TradeinnSource,
        "galaxus": GalaxusSource,
        "shopify": SneakersGenericSource,
        "hypernova": SneakersGenericSource,
        "cards": SneakersGenericSource,
        "jsonld": SneakersGenericSource,
    }


def build_catalog_sources(cfg: dict, http, general: dict, reserved: set[str]) -> list[CatalogSource]:
    """Instancie une source par site du catalogue (les sites désactivés sont créés mais ignorés au passage)."""
    log = logging.getLogger("catalog")
    catalog = cfg.get("sites_catalog") or {}
    defaults = catalog.get("defaults", {})
    engines = catalog_engines()
    sources: list[CatalogSource] = []
    seen: set[str] = set()
    for site in catalog.get("sites", []):
        sid, engine = site.get("id"), site.get("engine")
        if not sid and not engine:
            continue            # entrée de commentaire (ex. {"_lot3": "..."})
        if not sid or sid in reserved or sid in seen:
            log.warning("sites_catalog : id manquant ou en double (%s) — entrée ignorée", sid)
            continue
        cls = engines.get(engine)
        if cls is None:
            log.warning("sites_catalog : moteur inconnu « %s » pour %s — entrée ignorée", engine, sid)
            continue
        seen.add(sid)
        sources.append(cls({**defaults, **site}, http, general))
    return sources
