"""Étape C : comparaison multi-comparateurs + présence web multi-marchands (cache 12 h).

- idealo.fr (puis idealo.de en secours) hors mode : nombre d'offres et prix le plus bas ailleurs ;
- geizhals.eu pour le High-Tech / Gaming / Informatique quand Idealo ne trouve rien ;
- recherche web (DuckDuckGo) : marchands indépendants distincts proposant ce produit (indispensable pour la
  mode / les sneakers, peu présentes sur Idealo).
`complete` = tous les contrôles prévus ont réellement pu être faits (sinon la règle d'exclusivité ne s'applique pas).
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from brand_checker import independent_resellers
from http_client import FetchError
from models import Deal
from utils import normalize_text, parse_price, search_query_from_title, title_hash, title_similarity
from web_search import WebSearch, registrable_label

log = logging.getLogger("comparators")


def parse_geizhals(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    for it in soup.select(".listview__item, .productlist__item, article[data-id]"):
        a = it.select_one("a.listview__name-link, a.productlist__link, a[href*='-a']")
        if a is None:
            continue
        text = it.get_text(" ", strip=True)
        price_el = it.select_one(".price, .gh_price, .listview__price")
        offers = re.search(r"(\d+)\s+(?:Angebote|Angebot|offers?|offres?)", text, re.I)
        href = a.get("href", "")
        items.append({
            "title": a.get_text(" ", strip=True),
            "price": parse_price(price_el.get_text(" ", strip=True) if price_el else text)[0],
            "offer_count": int(offers.group(1)) if offers else None,
            "url": href if href.startswith("http") else "https://geizhals.eu/" + href.lstrip("/"),
        })
    return items


def same_shop(merchant: str, shop: str) -> bool:
    m = normalize_text(merchant).split(".")[0].split(" ")[0]
    return bool(m) and len(m) >= 3 and m in normalize_text(shop)


def idealo_other_merchants(res: dict, merchant: str) -> int:
    """Nombre de marchands autres que celui du deal, d'après un résultat Idealo."""
    n = int(res.get("offer_count") or 0)
    lowest_is_source = bool(res.get("lowest_is_same_merchant")) or same_shop(merchant, res.get("lowest_shop", ""))
    if n <= 0:
        return 0 if lowest_is_source or res.get("lowest_price") is None else 1
    return n - 1 if lowest_is_source else max(1, n - 1)


class MultiComparator:
    def __init__(self, cfg: dict, http, db=None, idealo_fr=None, idealo_de=None, web: Optional[WebSearch] = None,
                 brands_cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.http = http
        self.db = db
        self.idealo_fr = idealo_fr
        self.idealo_de = idealo_de
        self.web = web
        self.brands_cfg = brands_cfg or {}
        self._geizhals_blocked = False

    def reset_run(self) -> None:
        self._geizhals_blocked = False

    def _geizhals(self, deal: Deal) -> Optional[dict]:
        gcfg = self.cfg.get("geizhals", {})
        if self.http is None:
            self._geizhals_blocked = True
        if not gcfg.get("enabled", True) or self._geizhals_blocked:
            return None
        url = gcfg.get("search_url_template", "https://geizhals.eu/?fs={query}").format(
            query=quote_plus(search_query_from_title(deal.title)))
        try:
            items = parse_geizhals(self.http.get(url, timeout=12, retries=0).text)
        except FetchError as exc:
            if exc.blocked:
                self._geizhals_blocked = True
            log.info("Geizhals indisponible (%s)", exc)
            return None
        best = None
        for it in items[:8]:
            sim = title_similarity(deal.title, it["title"])
            if sim >= float(gcfg.get("min_title_similarity", 0.55)) and (best is None or sim > best[0]):
                best = (sim, it)
        return best[1] if best else {}

    def _web_presence(self, deal: Deal, brand: str, source_host: str) -> Optional[tuple[list[str], int]]:
        """(marchands indépendants trouvés, nombre de résultats pertinents au total, site source compris)."""
        if self.web is None:
            return None
        query = search_query_from_title(deal.title, max_tokens=8)
        if brand and normalize_text(brand) not in normalize_text(query):
            query = f"{brand} {query}"
        results = self.web.search(f"{query} prix")
        if results is None:
            return None
        relevant = [r for r in results
                    if title_similarity(deal.title, r["title"]) >= float(self.cfg.get("web_min_title_similarity", 0.35))]
        # le site officiel compte comme une présence indépendante : on ne retire pas les domaines « marque »
        return independent_resellers(relevant, "", source_host, self.brands_cfg), len(relevant)

    def compare(self, deal: Deal, *, fashion: bool, brand: str = "", source_host: str = "") -> dict:
        key = f"{title_hash(deal.title)}|{registrable_label(source_host)}|{int(fashion)}"
        if self.db is not None:
            cached = self.db.kv_get("compare", key, float(self.cfg.get("cache_hours", 12)))
            if cached is not None:
                return cached
        out = {"others": 0, "lowest": None, "lowest_shop": "", "checked": [], "complete": True, "idealo": None,
               "web_domains": []}

        def take_lowest(price: Optional[float], shop: str) -> None:
            if price is not None and (out["lowest"] is None or price < out["lowest"]):
                out["lowest"], out["lowest_shop"] = price, shop

        if not fashion:
            for name, client in (("idealo.fr", self.idealo_fr), ("idealo.de", self.idealo_de)):
                if client is None or not client.enabled:
                    continue
                res = client.lookup(deal)
                out["checked"].append(name)
                if res is None:
                    if getattr(client, "last_lookup_failed", False):
                        out["complete"] = False
                    continue
                out["idealo"] = out["idealo"] or res
                out["others"] = max(out["others"], idealo_other_merchants(res, deal.merchant))
                if not res.get("lowest_is_same_merchant"):
                    take_lowest(res.get("lowest_price"), f"{res.get('lowest_shop', '')} ({name})")
                break                       # idealo.de seulement en secours
            if out["others"] == 0 and self.cfg.get("geizhals", {}).get("enabled", True):
                g = self._geizhals(deal)
                if g is None:
                    if not self._geizhals_blocked:   # erreur passagère : le contrôle n'a pas été fait
                        out["complete"] = False      # (bloqué durablement = comparateur indisponible, ignoré)
                else:
                    out["checked"].append("geizhals.eu")
                    if g:
                        n = g.get("offer_count") or 0
                        out["others"] = max(out["others"], max(0, n - 1) if n else 1)
                        take_lowest(g.get("price"), "geizhals.eu")

        if out["others"] == 0:
            presence = self._web_presence(deal, brand, source_host)
            if presence is None:
                out["complete"] = False
            else:
                domains, relevant = presence
                out["checked"].append("recherche web")
                out["web_domains"] = domains[:8]
                out["others"] = max(out["others"], len(domains))
                if relevant == 0:
                    # la recherche n'a retrouvé le produit nulle part (pas même sur le site source) :
                    # c'est une recherche infructueuse, pas une preuve d'exclusivité
                    out["complete"] = False

        if self.db is not None and out["complete"]:
            self.db.kv_put("compare", key, out)
        return out
