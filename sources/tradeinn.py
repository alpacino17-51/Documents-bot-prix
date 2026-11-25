"""Réseau Tradeinn (runnerinn, bikeinn, trekkinn, snowinn, waveinn, dressinn...) : un seul moteur mutualisé.

Les pages de tradeinn.com sont entièrement construites en JavaScript à partir d'un moteur de recherche privé
(protégé par une clé d'API) : il n'y a aucun produit dans le HTML et ce moteur n'est volontairement PAS utilisé.
Les boutiques Tradeinn sont en revanche référencées sur Idealo : ce moteur lit les « bons plans » publics
d'Idealo (fr/es/de/it, tri nouveauté + réduction) et ne garde que les offres des boutiques du réseau.
Les pages produit Tradeinn éventuellement listées dans `page_urls` sont lues en JSON-LD.
"""
from __future__ import annotations

from models import Deal
from sources.catalog import CatalogSource
from sources.comparators_drops import idealo_shop_offers
from sources.merchant_common import jsonld_products, make_deal, sort_by_discount
from utils import normalize_text

NETWORK = {
    "runnerinn": "Sport", "bikeinn": "Sport", "trekkinn": "Sport", "snowinn": "Sport", "waveinn": "Sport",
    "swiminn": "Sport", "smashinn": "Sport", "goalinn": "Sport", "diveinn": "Sport", "fitnessinn": "Sport",
    "motardinn": "Sport", "xtremeinn": "Sport", "outletinn": "Sport", "dressinn": "Mode", "kidinn": "Mode",
    "techinn": "High-tech", "tradeinn": "Sport",
}


def shop_category(shop: str) -> str:
    norm = normalize_text(shop)
    for name, cat in NETWORK.items():
        if name in norm:
            return cat
    return "Sport"


class TradeinnSource(CatalogSource):
    engine = "tradeinn"

    def fetch(self) -> list[Deal]:
        shops = self.cfg.get("shops", list(NETWORK))
        deals: dict[str, Deal] = {}
        for d in idealo_shop_offers(self, shops, self.cfg.get("idealo_countries", ["fr", "es", "de", "it"])):
            d.category = shop_category(d.merchant)
            d.site_country = "ES"                 # le réseau Tradeinn expédie depuis l'Espagne
            deals.setdefault(d.source_id, d)
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"{self.label} {url}")
            if not html:
                continue
            for p in jsonld_products(html, url):
                d = make_deal(self.name, "Tradeinn", p["title"], p["url"] or url, p["price"], p["original"],
                              source_id=p["sku"] or p["url"] or url, category="Sport", ean=p["gtin"], mpn=p["mpn"])
                if d:
                    d.brand = p["brand"]
                    d.site_country = "ES"
                    deals.setdefault(d.source_id, d)
        return sort_by_discount(list(deals.values()))
