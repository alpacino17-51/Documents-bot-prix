"""Registre des sources. Pour ajouter une source : créer une classe héritant de BaseSource
et l'ajouter à FEED_SOURCES avec la clé de sa section dans config.json (sources.<clé>)."""
from sources.base import BaseSource
from sources.boulanger import BoulangerSource
from sources.camel import CamelSource
from sources.dealabs import DealabsSource
from sources.decathlon import DecathlonSource
from sources.fnac import FnacSource
from sources.idealo import IdealoSource
from sources.idealo_drops import IdealoDropsSource
from sources.pepper_eu import PepperEUSource
from sources.reddit import RedditSource
from sources.zalando import ZalandoSource

FEED_SOURCES: dict[str, type[BaseSource]] = {
    "dealabs": DealabsSource,
    "pepper_eu": PepperEUSource,
    "camelcamelcamel": CamelSource,
    "reddit": RedditSource,
    "idealo_drops": IdealoDropsSource,
    "decathlon": DecathlonSource,
    "boulanger": BoulangerSource,
    "fnac": FnacSource,
    "zalando": ZalandoSource,
}

#: sources présentes avant l'ajout du « premier passage silencieux par source »
LEGACY_SOURCES = ("dealabs", "pepper_eu", "camelcamelcamel", "reddit")

__all__ = ["BaseSource", "FEED_SOURCES", "LEGACY_SOURCES", "BoulangerSource", "CamelSource", "DealabsSource",
           "DecathlonSource", "FnacSource", "IdealoDropsSource", "IdealoSource", "PepperEUSource", "RedditSource",
           "ZalandoSource"]
