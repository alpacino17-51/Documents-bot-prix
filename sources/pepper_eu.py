"""Réseau Pepper européen (mydealz.de, hotukdeals.com, chollometro.com, pepper.it).

Ne conserve que les offres livrables en France : liens Amazon EU (amazon.de/.it/.es/.fr/...), marchand
« Amazon » sur un site de l'UE, ou mention explicite de livraison en France.
"""
from __future__ import annotations

from models import Deal
from sources.base import BaseSource
from sources.pepper_common import parse_listing_html, parse_listing_json, parse_rss
from utils import find_keywords, url_host

COUNTRY_AMAZON_HOST = {"DE": "amazon.de", "ES": "amazon.es", "IT": "amazon.it", "FR": "amazon.fr",
                       "UK": "amazon.co.uk"}


def _host_match(host: str, domains: list[str]) -> bool:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in domains)


def ships_to_france(deal: Deal, fcfg: dict) -> bool:
    # un lien « /visit/ » pointe vers le site Pepper lui-même, pas vers le marchand
    host = deal.link_host or ("" if "/visit/" in deal.merchant_url else url_host(deal.merchant_url))
    allowed = fcfg.get("allowed_link_hosts", [])
    excluded = fcfg.get("excluded_link_hosts", [])
    if host and _host_match(host, excluded):
        return False
    if host and _host_match(host, allowed):
        return True
    is_amazon_merchant = any(p in (deal.merchant or "").lower() for p in fcfg.get("amazon_merchant_patterns", ["amazon"]))
    if is_amazon_merchant and not host:
        inferred = COUNTRY_AMAZON_HOST.get(deal.site_country.upper(), "")
        if inferred and _host_match(inferred, allowed):
            deal.link_host = inferred
            return True
    return bool(find_keywords(deal.text_blob(), fcfg.get("ship_to_france_keywords", [])))


class PepperEUSource(BaseSource):
    name = "pepper_eu"

    def fetch_site(self, site: dict) -> list[Deal]:
        source_name = site.get("name", "pepper")
        order = self.cfg.get("fetch_order", ["json", "rss", "html"])
        deals: dict[str, Deal] = {}
        page_urls = site.get("page_urls", [])
        rss_urls = site.get("rss_urls", [])
        for i in range(max(len(page_urls), len(rss_urls))):
            page = page_urls[i] if i < len(page_urls) else None
            rss = rss_urls[i] if i < len(rss_urls) else None
            handlers = {}
            if page:
                handlers["json"] = lambda p=page: parse_listing_json(self.http.get(p).text, site, source_name)
                handlers["html"] = lambda p=page: parse_listing_html(self.http.get(p).text, site, source_name)
            if rss:
                handlers["rss"] = lambda r=rss: parse_rss(
                    self.http.get(r, accept="application/rss+xml, application/xml, text/xml").text, site, source_name)
            for d in self.try_modes(order, handlers, f"{source_name}/{page or rss}"):
                deals.setdefault(d.source_id, d)
        return list(deals.values())

    def fetch(self) -> list[Deal]:
        fcfg = self.cfg.get("france_delivery", {})
        kept: list[Deal] = []
        for site in self.cfg.get("sites", []):
            if not site.get("enabled", True):
                continue
            site_deals = self.fetch_site(site)
            ok = [d for d in site_deals if ships_to_france(d, fcfg)]
            self.log.info("%s : %d deal(s), %d livrable(s) en France", site.get("name"), len(site_deals), len(ok))
            kept.extend(ok)
        return kept
