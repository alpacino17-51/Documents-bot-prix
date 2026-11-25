"""Idealo.fr : module de vérification / comparaison (pas un flux de deals).

- Recherche : `prechcat.html?q=...` -> la page embarque les résultats en JSON (base64) dans
  `window.csr.initialData['searchResult']` (id produit, titre, prix le plus bas, marchand, nb d'offres).
- Fiche produit : JSON-LD (AggregateOffer lowPrice/offerCount) + liste des offres (`data-mtrx-click`).
- Historique : API JSON du graphique de prix (`/price-chart/sites/{site}/products/{id}/history`),
  données `[{x: date, y: prix}]`. Si l'API ne répond pas, on se rabat sur l'historique local (state.db).
"""
from __future__ import annotations

import base64
import json
import re
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from http_client import FetchError
from models import Deal
from sources.base import BaseSource
from utils import find_keywords, normalize_text, parse_price, search_query_from_title, title_hash, title_similarity

DAY = 86400.0
_SEARCH_B64 = re.compile(r"initialData\[['\"]searchResult['\"]\]\s*=\s*\"([A-Za-z0-9+/=]+)\"")
_PRODUCT_ID = re.compile(r"/(?:prix|preisvergleich/OffersOfProduct|prixcompare/OffersOfProduct)/(\d+)")


# ------------------------------------------------------------------ parseurs

def idealo_url(href: str, base_url: str = "https://www.idealo.fr") -> str:
    """URL absolue d'une fiche Idealo ; certains résultats n'ont qu'un identifiant haché en guise de lien -> ''."""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    return ""


def parse_search_results(html: str, base_url: str = "https://www.idealo.fr") -> list[dict]:
    m = _SEARCH_B64.search(html)
    items: list[dict] = []
    if m:
        try:
            data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            data = {}
        for it in data.get("items", []):
            if it.get("type") not in (None, "PRODUCT"):
                continue
            offer = it.get("offerInfo") or {}
            shop = it.get("shopInfo") or {}
            price = it.get("rawPrice")
            if price is None and offer.get("formattedPrice"):
                price = parse_price(offer["formattedPrice"])[0]
            items.append({
                "product_id": str(it.get("id", "")),
                "title": it.get("title", ""),
                "price": float(price) if price is not None else None,
                "shop": shop.get("shopName", ""),
                "offer_count": offer.get("offerCount"),
                "used_only": bool(offer.get("usedOnly")),
                "url": idealo_url(it.get("href") or "", base_url),
            })
        return items
    # Secours HTML : tuiles de résultats avec lien produit
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    for a in soup.select("a[href*='/prix/'], a[href*='OffersOfProduct']"):
        pm = _PRODUCT_ID.search(a.get("href", ""))
        if not pm or pm.group(1) in seen:
            continue
        seen.add(pm.group(1))
        tile = a.find_parent(["div", "li", "article"]) or a
        text = tile.get_text(" ", strip=True)
        title_el = tile.select_one("[class*=title], [class*=Title]")
        href = a.get("href", "")
        items.append({
            "product_id": pm.group(1),
            "title": (title_el.get_text(" ", strip=True) if title_el else a.get_text(" ", strip=True)) or text[:120],
            "price": parse_price(text)[0],
            "shop": "",
            "offer_count": None,
            "used_only": False,
            "url": href if href.startswith("http") else base_url + href,
        })
    return items


def parse_product_page(html: str, url: str = "") -> dict:
    soup = BeautifulSoup(html, "lxml")
    info: dict[str, Any] = {"product_id": "", "title": "", "lowest_price": None, "offer_count": None,
                            "offers": [], "gtin": "", "mpn": "", "url": url}
    chart = soup.select_one("[data-price-chart]")
    if chart:
        try:
            info["product_id"] = str(json.loads(chart["data-price-chart"]).get("productId", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    if not info["product_id"]:
        m = _PRODUCT_ID.search(url)
        info["product_id"] = m.group(1) if m else ""
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            info["title"] = info["title"] or node.get("name", "")
            info["gtin"] = str(node.get("gtin13") or node.get("gtin") or node.get("gtin14") or "")
            info["mpn"] = str(node.get("mpn") or "")
            offers = node.get("offers") or {}
            if isinstance(offers, dict):
                info["lowest_price"] = offers.get("lowPrice") or offers.get("price")
                info["offer_count"] = offers.get("offerCount")
    for li in soup.select("li.productOffers-listItem[data-mtrx-click]"):
        try:
            data = json.loads(li["data-mtrx-click"])
        except (json.JSONDecodeError, KeyError):
            continue
        products = data.get("products") or [{}]
        price = products[0].get("price")
        if price is not None:
            info["offers"].append({"shop": data.get("shop_name", ""), "price": float(price)})
    info["offers"].sort(key=lambda o: o["price"])
    if info["lowest_price"] is None and info["offers"]:
        info["lowest_price"] = info["offers"][0]["price"]
    if info["lowest_price"] is not None:
        info["lowest_price"] = float(info["lowest_price"])
    return info


def _ts(x: Any) -> Optional[float]:
    if isinstance(x, (int, float)):
        return x / 1000 if x > 1e11 else float(x)
    if isinstance(x, str):
        try:
            dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def parse_history(payload: Any, now: Optional[float] = None) -> Optional[dict]:
    """Convertit la réponse du graphique Idealo en statistiques 90 j / 30 j."""
    if not isinstance(payload, dict):
        return None
    now = now or time.time()
    points = []
    for p in payload.get("data") or []:
        ts, y = _ts(p.get("x")), p.get("y")
        if ts is not None and isinstance(y, (int, float)):
            points.append((ts, float(y)))
    p90 = [y for ts, y in points if ts >= now - 90 * DAY]
    p30 = [y for ts, y in points if ts >= now - 30 * DAY]
    if not p90:
        return None
    return {
        "median_90": round(statistics.median(p90), 2),
        "low_30": round(min(p30), 2) if p30 else None,
        "max_90": round(max(p90), 2),
        "points": len(p90),
    }


# ------------------------------------------------------------------ module

class IdealoSource(BaseSource):
    name = "idealo"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = None
        self.filters_cfg: dict = {}
        self._history_broken = False
        self.last_lookup_failed = False

    def fetch(self) -> list[Deal]:
        return []  # module de vérification uniquement

    @property
    def base_url(self) -> str:
        return self.cfg.get("base_url", "https://www.idealo.fr").rstrip("/")

    def search(self, query: str) -> tuple[list[dict], Optional[dict]]:
        """Retourne (candidats, fiche_produit_si_redirection_directe)."""
        tpl = self.cfg.get("search_url_template", self.base_url + "/prechcat.html?q={query}")
        res = self.http.get(tpl.format(query=quote_plus(query)))
        if _PRODUCT_ID.search(res.url) and "initialData['searchResult']" not in res.text:
            return [], parse_product_page(res.text, res.url)
        return parse_search_results(res.text, self.base_url), None

    def product(self, url: str) -> dict:
        res = self.http.get(url)
        return parse_product_page(res.text, res.url)

    def history(self, product_id: str) -> Optional[dict]:
        if not self.cfg.get("history_api_enabled", True) or self._history_broken or not product_id:
            return None
        tpl = self.cfg.get("history_url_template",
                           self.base_url + "/price-chart/sites/{site_id}/products/{product_id}/history?period={period}")
        url = tpl.format(site_id=self.cfg.get("site_id", 4), product_id=product_id,
                         period=self.cfg.get("history_period", "P3M"))
        try:
            return parse_history(self.http.get_json(url, headers={"Referer": self.base_url + "/"}))
        except FetchError as exc:
            if exc.status in (401, 403, 404):
                self._history_broken = True   # inutile de réessayer pendant ce passage
            self.log.info("Historique Idealo indisponible (%s) : repli sur l'historique local", exc)
            return None

    # -------------------------------------------------------------- correspondance
    def _acceptable(self, deal: Deal, cand: dict, by_code: bool) -> tuple[bool, float]:
        if cand.get("used_only"):
            return False, 0.0
        sim = title_similarity(deal.title, cand.get("title", ""))
        acc_kw = self.filters_cfg.get("accessory_keywords", [])
        cand_acc = set(find_keywords(cand.get("title", ""), acc_kw))
        deal_acc = set(find_keywords(deal.title, acc_kw))
        if cand_acc - deal_acc:
            return False, sim          # le candidat est un accessoire, pas le produit
        price = cand.get("price")
        if price is not None and deal.price_eur and price < deal.price_eur * 0.5:
            return False, sim          # bien moins cher que le deal : sûrement un autre produit/accessoire
        threshold = 0.3 if by_code else float(self.cfg.get("min_title_similarity", 0.55))
        return sim >= threshold, sim

    def lookup(self, deal: Deal) -> Optional[dict]:
        """Cherche le produit sur Idealo (EAN/GTIN -> MPN -> titre nettoyé). Résultat mis en cache 12 h."""
        self.last_lookup_failed = False
        cache_key = self.cfg.get("cache_prefix", "idealo") + ":" + (deal.ean or deal.mpn or title_hash(deal.title))
        db = self.db
        if db is not None:
            cached = db.cache_get(cache_key, float(self.cfg.get("cache_hours", 12)))
            if cached is not None:
                return cached or None

        result = None
        queries = [(q, by) for q, by in ((deal.ean, "EAN"), (deal.mpn, "MPN"),
                                         (search_query_from_title(deal.title), "titre")) if q]
        for query, by in queries:
            try:
                candidates, direct = self.search(query)
            except FetchError as exc:
                self.log.info("Recherche Idealo impossible (%s)", exc)
                self.last_lookup_failed = True
                return None     # pas de mise en cache : on réessaiera au prochain passage
            by_code = by in ("EAN", "MPN")
            if direct and direct.get("product_id"):
                ok, sim = self._acceptable(deal, {"title": direct["title"], "price": direct["lowest_price"]}, by_code)
                if ok:
                    result = self._build(direct, sim, by)
                    break
            scored = []
            for cand in candidates[: int(self.cfg.get("max_candidates", 8))]:
                ok, sim = self._acceptable(deal, cand, by_code)
                if ok:
                    scored.append((sim, cand))
            if scored:
                sim, best = max(scored, key=lambda s: s[0])
                result = self._build({"product_id": best["product_id"], "title": best["title"],
                                      "lowest_price": best["price"], "offer_count": best.get("offer_count"),
                                      "url": best["url"], "offers": [], "shop": best.get("shop", "")}, sim, by)
                break

        if result:
            result = self._complete(deal, result)
        if db is not None:
            db.cache_put(cache_key, result or {})
            if result and result.get("lowest_price"):
                db.add_price_observation(f"idealo:{result['product_id']}", result["lowest_price"], "idealo")
        return result

    def _build(self, info: dict, sim: float, by: str) -> dict:
        lowest_shop = info.get("shop", "")
        if not lowest_shop and info.get("offers"):
            lowest_shop = info["offers"][0]["shop"]
        return {
            "product_id": info.get("product_id", ""),
            "title": info.get("title", ""),
            "url": info.get("url", ""),
            "lowest_price": info.get("lowest_price"),
            "lowest_shop": lowest_shop,
            "offer_count": info.get("offer_count"),
            "offers": info.get("offers", []),
            "similarity": sim,
            "matched_by": by,
            "history": None,
        }

    def _complete(self, deal: Deal, result: dict) -> dict:
        # si le moins cher est le marchand du deal, on cherche le moins cher *ailleurs*
        merchant = normalize_text(deal.merchant).split(".")[0]
        if merchant and merchant in normalize_text(result.get("lowest_shop", "")):
            offers = result.get("offers")
            if not offers and result.get("url", "").startswith(self.base_url + "/"):
                try:
                    offers = self.product(result["url"]).get("offers", [])
                except FetchError:
                    offers = []
            others = [o for o in offers if merchant not in normalize_text(o["shop"])]
            if others:
                result["lowest_price"] = others[0]["price"]
                result["lowest_shop"] = others[0]["shop"]
            else:
                # aucune autre offre connue : le « plus bas » est l'offre du deal elle-même (offres lues),
                # ou bien la liste des offres n'a pas pu être lue -> le prix des concurrents est inconnu
                result["lowest_is_same_merchant"] = True
                if not offers:
                    result["offers_unknown"] = True
            result["offers"] = offers
        result["history"] = self.history(result.get("product_id", ""))
        result["offers"] = result.get("offers", [])[:5]
        return result
