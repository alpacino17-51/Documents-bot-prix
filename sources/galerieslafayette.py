"""Galeries Lafayette (galerieslafayette.com) : bons plans / promotions / déstockage.

Le site est une application Angular : toutes ses pages renvoient la même coquille HTML et les produits sont
chargés ensuite depuis une API interne (non utilisée). Sources publiques exploitées, dans l'ordre :
1. les deals Galeries Lafayette publiés sur Dealabs (recherche triée par date, filtrée sur le marchand) ;
2. les pages du site listées dans `page_urls`, si elles contiennent un jour des produits (JSON-LD / JSON embarqué) ;
3. les offres Galeries Lafayette présentes dans les bons plans publics d'Idealo.
Le site incluant une marketplace tierce, ces deals passent par la vérification complète (marque, comparateurs).
"""
from __future__ import annotations

from models import Deal
from sources.base import BaseSource
from sources.comparators_drops import idealo_shop_offers
from sources.merchant_common import sort_by_discount
from sources.pepper_common import parse_listing_json
from sources.retailer_generic import parse_embedded, parse_jsonld_discounted
from utils import normalize_text

DEALABS = {"name": "dealabs", "country": "FR", "base_url": "https://www.dealabs.com", "deal_path": "bons-plans",
           "visit_path": "visit/homenew", "currency": "EUR"}


class GaleriesLafayetteSource(BaseSource):
    name = "galerieslafayette"
    label = "Galeries Lafayette"

    def fetch(self) -> list[Deal]:
        deals: dict[str, Deal] = {}
        wanted = normalize_text(self.cfg.get("merchant_filter", "galeries lafayette"))
        for url in self.cfg.get("dealabs_search_urls", []):
            html = self.get_text(url, "Galeries Lafayette via Dealabs")
            if not html:
                continue
            for d in parse_listing_json(html, DEALABS, self.name):
                if wanted in normalize_text(d.merchant):
                    d.description = f"{d.description} — deal Galeries Lafayette relayé par Dealabs".strip(" —")
                    deals.setdefault(f"dl-{d.source_id}", d)

        base = self.cfg.get("base_url", "https://www.galerieslafayette.com")
        for url in self.cfg.get("page_urls", []):
            html = self.get_text(url, f"Galeries Lafayette {url}")
            if not html:
                if self.blocked:
                    break
                continue
            found = parse_jsonld_discounted(html, base, self.name) or parse_embedded(html, base, self.name)
            if not found:
                self.log.debug("Galeries Lafayette %s : aucun produit dans le HTML (application JavaScript)", url)
            for d in found:
                d.merchant, d.link_host = "Galeries Lafayette", "galerieslafayette.com"
                deals.setdefault(d.url, d)

        patterns = self.cfg.get("idealo_shop_patterns", ["galeries lafayette", "galerieslafayette"])
        if patterns:
            for d in idealo_shop_offers(self, patterns, self.cfg.get("idealo_countries", ["fr"])):
                deals.setdefault(f"id-{d.source_id}", d)
        return sort_by_discount(list(deals.values()))
