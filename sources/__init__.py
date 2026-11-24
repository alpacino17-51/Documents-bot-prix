"""Registre des sources. Pour ajouter une source : créer une classe héritant de BaseSource
et l'ajouter à FEED_SOURCES avec la clé de sa section dans config.json (sources.<clé>)."""
from sources.base import BaseSource
from sources.camel import CamelSource
from sources.dealabs import DealabsSource
from sources.idealo import IdealoSource
from sources.pepper_eu import PepperEUSource
from sources.reddit import RedditSource

FEED_SOURCES: dict[str, type[BaseSource]] = {
    "dealabs": DealabsSource,
    "pepper_eu": PepperEUSource,
    "camelcamelcamel": CamelSource,
    "reddit": RedditSource,
}

__all__ = ["BaseSource", "FEED_SOURCES", "CamelSource", "DealabsSource", "IdealoSource", "PepperEUSource",
           "RedditSource"]
