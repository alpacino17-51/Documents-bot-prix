"""Marques indépendantes de confiance vendues uniquement sur leur propre boutique Shopify (`trusted_dtc_brands`).

Ces marques (ex. DivinByDivin, Davril Supply) n'ont ni revendeurs ni présence sur les comparateurs : la vérification
multi-marchands n'a pas de sens. Le verdict repose uniquement sur l'HISTORIQUE LOCAL construit par le bot :
1. collecte de tout le catalogue (`/products.json?limit=250`, paginé, + collections listées ; repli HTML si le JSON
   Shopify est désactivé) toutes les `min_interval_minutes` (30) ;
2. enregistrement du prix de CHAQUE produit (soldé ou non) dans `state.db` (clé `dtc:<domaine>:<id Shopify>`) ;
3. alerte :
   - ✅ « Baisse vérifiée (historique local) » : historique suffisant (≥ 2 relevés sur ≥ 3 jours, ou prix antérieur
     plus élevé observé) ET prix actuel ≥ `min_drop_pct` (35 %) sous la médiane observée ET ≤ au plus bas relevé ;
   - ℹ️ « Promo directe (historique en cours) » : historique encore insuffisant, mais `compare_at_price` affiche une
     baisse ≥ `intro_min_compare_at_pct` (50 %) ;
   - aucun signal si le prix n'a jamais bougé depuis le premier relevé (fausse promo permanente).
Filtre de tailles (alerte seulement, jamais l'historique) : `wanted_sizes` (ex. ["M", "L", "M/L"], [] = toutes),
correspondance EXACTE sur la valeur de taille de chaque variante `available == True` ; `allow_one_size` laisse passer
les accessoires en taille unique. Les tailles retenues sont affichées dans l'alerte.
"""
from __future__ import annotations

import re
import statistics
import time
from typing import Optional
from urllib.parse import urlsplit

from http_client import FetchError
from models import Deal
from sources.base import BaseSource
from sources.merchant_common import make_deal, sort_by_discount
from sources.sneakers_generic import parse_cards

DAY = 86400.0
ONE_SIZE_VALUES = {"default title", "one size", "onesize", "one-size", "os", "tu", "t.u.", "taille unique",
                   "unique", "u", "size unique", "einheitsgrösse", "einheitsgrosse"}
SIZE_OPTION_NAMES = ("size", "taille", "taglia", "talla", "grösse", "grosse", "größe", "pointure", "waist")
_LETTER_SIZE = r"(?:X{0,3}S|M|X{0,3}L|\d?XL|\d?XS)"
# taille de vêtement standard (S, M, XL, 2XL, M/L, S-M...) ou chiffrée (32, 34/32, W32, 42.5, EU 42)
_CLOTHING_RE = re.compile(rf"^{_LETTER_SIZE}(?:\s?[/-]\s?{_LETTER_SIZE})?$", re.I)
_NUMERIC_RE = re.compile(r"^(?:W|EU\s?|FR\s?|US\s?|UK\s?)?\d{1,2}(?:[.,]5)?(?:\s?[/x]\s?(?:L)?\d{2})?$", re.I)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_size(value: str) -> str:
    """« m / l » -> « M/L », « eu 42 » -> « EU 42 » : comparaison exacte, insensible à la casse et aux espaces."""
    value = re.sub(r"\s*/\s*", "/", (value or "").strip())
    return re.sub(r"\s+", " ", value).upper()


def is_size_value(value: str) -> bool:
    v = normalize_size(value)
    return bool(v) and (bool(_CLOTHING_RE.match(v)) or bool(_NUMERIC_RE.match(v)))


def is_one_size_value(value: str) -> bool:
    return (value or "").strip().lower() in ONE_SIZE_VALUES


def variant_options(v: dict) -> list[str]:
    """Valeurs d'options d'une variante : option1/2/3, sinon le titre découpé sur « / » ENTOURÉ d'espaces
    (« Noir / M/L » -> ["Noir", "M/L"] : la taille double M/L n'est pas cassée)."""
    values = [(v.get(k) or "").strip() for k in ("option1", "option2", "option3")]
    values = [x for x in values if x]
    if not values:
        values = [x.strip() for x in (v.get("title") or "").split(" / ") if x.strip()]
    return values


def size_option_index(product: dict) -> Optional[int]:
    """Position de l'option « Taille » d'après les noms d'options Shopify (Size, Taille, SIZE...)."""
    for i, opt in enumerate(product.get("options") or []):
        name = (opt.get("name") if isinstance(opt, dict) else str(opt)) or ""
        if any(n in name.strip().lower() for n in SIZE_OPTION_NAMES):
            return i
    return None


def variant_size(v: dict, size_index: Optional[int] = None) -> str:
    """Taille normalisée d'une variante ('' si aucune taille de vêtement / pantalon / chaussure reconnue)."""
    options = variant_options(v)
    if size_index is not None and size_index < len(options) and is_size_value(options[size_index]):
        return normalize_size(options[size_index])
    for value in options:                     # pas d'option nommée : première valeur qui ressemble à une taille
        if is_size_value(value):
            return normalize_size(value)
    return ""


def is_one_size_product(product: dict) -> bool:
    """Taille unique : une seule variante « Default Title / One Size / OS / TU / Taille unique / Unique / U »,
    ou aucune variante ne porte de taille reconnaissable (ex. uniquement des couleurs)."""
    variants = product.get("variants") or []
    if not variants:
        return False
    if len(variants) == 1 and any(is_one_size_value(x) for x in variant_options(variants[0]) or ["Default Title"]):
        return True
    idx = size_option_index(product)
    if all(any(is_one_size_value(x) for x in variant_options(v)) for v in variants):
        return True
    return not any(variant_size(v, idx) for v in variants)


def select_variants(product: dict, wanted: list[str], allow_one_size: bool) -> tuple[list[dict], list[str]]:
    """Variantes EN STOCK (available == True) retenues pour l'alerte, et libellés de tailles à afficher.
    Liste vide = produit ignoré pour l'alerte (aucune taille voulue en stock, ou taille unique refusée)."""
    available = [v for v in product.get("variants") or [] if v.get("available") is True]
    if not available:
        return [], []
    if is_one_size_product(product):
        return (available, ["Taille unique"]) if allow_one_size else ([], [])
    idx = size_option_index(product)
    wanted_norm = {normalize_size(w) for w in wanted if str(w).strip()}
    matched = [v for v in available if variant_size(v, idx) and (not wanted_norm or variant_size(v, idx) in wanted_norm)]
    labels = sorted({variant_size(v, idx) for v in matched}, key=_size_order)
    return matched, labels


def analyse_history(previous: list[tuple[float, float]], current: float, compare_at: Optional[float],
                    cfg: dict, now: Optional[float] = None) -> Optional[dict]:
    """Décide l'alerte pour un produit à partir des relevés ANTÉRIEURS au passage courant."""
    now = now or time.time()
    # tolérance d'un demi-point : les boutiques arrondissent leurs prix (64,98 € au lieu de 129,95 € = « -50 % »)
    min_drop = float(cfg.get("min_drop_pct", 35)) / 100 - 0.005
    intro_min = float(cfg.get("intro_min_compare_at_pct", 50)) / 100 - 0.005
    min_points = int(cfg.get("min_history_points", 2))
    min_days = float(cfg.get("min_history_days", 3))
    prices = [p for _, p in previous]
    if prices and all(abs(p - current) < 0.01 for p in prices):
        return None               # prix identique depuis le premier relevé : pas une baisse
    span_days = (now - previous[0][0]) / DAY if previous else 0
    higher_seen = any(p > current + 0.01 for p in prices)
    ready = (len(prices) >= min_points and span_days >= min_days) or higher_seen
    if ready:
        reference = statistics.median(prices)
        lowest = min(prices)
        drop = 1 - current / reference if reference else 0
        if drop >= min_drop and current <= lowest + 0.01:
            return {"kind": "verified", "reference": round(reference, 2), "lowest": round(lowest, 2),
                    "max": round(max(prices), 2), "points": len(prices), "days": round(span_days, 1),
                    "drop_pct": round(drop * 100, 1)}
        return None
    if compare_at and compare_at > current and 1 - current / compare_at >= intro_min:
        return {"kind": "intro", "compare_at": compare_at, "points": len(prices), "days": round(span_days, 1),
                "drop_pct": round((1 - current / compare_at) * 100, 1)}
    return None


class DTCShopifySource(BaseSource):
    name = "trusted_dtc_brands"
    label = "Marques indépendantes"

    def _get_products(self, base: str, path: str, max_pages: int) -> Optional[list[dict]]:
        """Tous les produits d'un endpoint Shopify paginé ; None si l'endpoint n'est pas disponible."""
        products: list[dict] = []
        for page in range(1, max_pages + 1):
            url = f"{base}{path}products.json?limit=250&page={page}"
            try:
                payload = self.http.get_json(url)
                self.pages_ok += 1
            except FetchError as exc:
                if page == 1:
                    self.note_error(exc, f"{base}{path}products.json")
                    return None
                break
            batch = (payload or {}).get("products", []) if isinstance(payload, dict) else []
            products.extend(batch)
            if len(batch) < 250:
                break
        return products

    def _catalog(self, brand: dict) -> tuple[list[dict], bool]:
        base = brand["base_url"].rstrip("/")
        max_pages = int(self.cfg.get("max_pages", 10))
        catalog: dict[int, dict] = {}
        json_ok = False
        paths = ["/"] + [f"/collections/{c.strip('/').split('/')[-1]}/" for c in brand.get("collections", [])]
        for path in paths:
            got = self._get_products(base, path, max_pages)
            if got is None:
                if self.blocked:
                    break
                continue
            json_ok = True
            for p in got:
                catalog.setdefault(p.get("id"), p)
        return list(catalog.values()), json_ok

    def _html_fallback(self, brand: dict) -> list[Deal]:
        """`/products.json` désactivé : lecture des vignettes HTML des collections (sans tailles ni stock)."""
        base = brand["base_url"].rstrip("/")
        deals: list[Deal] = []
        for c in brand.get("collections", ["/collections/all"]) or ["/collections/all"]:
            url = base + (c if c.startswith("/") else "/" + c)
            html = self.get_text(url, f"{brand.get('name')} {url}")
            if html:
                deals.extend(parse_cards(html, base, f"dtc-{brand['id']}", "/products/", "Mode"))
        return deals

    def fetch(self) -> list[Deal]:
        now = time.time()
        out: list[Deal] = []
        for brand in self.cfg.get("brands", []):
            if brand.get("enabled", True) is False:
                continue
            base = brand["base_url"].rstrip("/")
            domain = urlsplit(base).hostname.removeprefix("www.")
            name = brand.get("name") or domain
            source = f"dtc-{brand['id']}"
            products, json_ok = self._catalog(brand)
            if not json_ok:
                for d in self._html_fallback(brand):        # pas de stock / tailles : analyse sur le prix seul
                    key = f"dtc:{domain}:{d.url}"
                    decision = self._decide(key, d.price, d.original_price, now)
                    if decision and self.db is not None:
                        d.source_id = f"{d.url}@{d.price:.2f}"     # un nouveau prix plus bas = nouvelle alerte
                        out.append(self._finish(d, name, domain, decision, []))
                    if self.db is not None and d.price:
                        self.db.add_price_observation(key, d.price, source, ts=now)
                continue
            wanted = list(brand.get("wanted_sizes", self.cfg.get("wanted_sizes", [])))
            allow_one_size = bool(brand.get("allow_one_size", self.cfg.get("allow_one_size", True)))
            for p in products:
                pid = p.get("id")
                variants = p.get("variants") or []
                all_prices = [x for x in (_f(v.get("price")) for v in variants) if x]
                if not pid or not all_prices:
                    continue
                key = f"dtc:{domain}:{pid}"
                # le filtre de tailles ne s'applique qu'à l'alerte : l'historique est calculé sur le catalogue entier
                matched, sizes = select_variants(p, wanted, allow_one_size)
                decision = None
                prices = [x for x in (_f(v.get("price")) for v in matched) if x]
                if prices:
                    current = min(prices)
                    compare_at = max((_f(v.get("compare_at_price")) or 0) for v in matched) or None
                    decision = self._decide(key, current, compare_at, now)
                # historique complet : TOUS les produits, soldés ou non, en stock ou non, toutes tailles
                if self.db is not None:
                    self.db.add_price_observation(key, min(all_prices), source, ts=now)
                if not decision:
                    continue
                original = decision.get("reference") if decision["kind"] == "verified" else decision.get("compare_at")
                d = make_deal(source, name, p.get("title", ""), f"{base}/products/{p.get('handle', '')}", current,
                              original, source_id=f"{pid}@{current:.2f}", category="Mode",
                              link_host=domain, category_hint=p.get("product_type", ""))
                if d:
                    out.append(self._finish(d, name, domain, decision, sizes, p.get("vendor") or name))
        return sort_by_discount(out)

    def _decide(self, key: str, current: Optional[float], compare_at: Optional[float], now: float) -> Optional[dict]:
        if current is None:
            return None
        previous = self.db.price_history(key, now=now) if self.db is not None else []
        previous = [(ts, p) for ts, p in previous if ts < now - 1]        # relevés antérieurs à ce passage
        return analyse_history(previous, current, compare_at, self.cfg, now)

    @staticmethod
    def _finish(d: Deal, name: str, domain: str, decision: dict, sizes: list[str], brand: str = "") -> Deal:
        d.merchant = name
        d.brand = brand or name
        d.trusted_dtc = True
        d.dtc = decision
        d.sizes = sizes
        d.link_host = domain
        d.merchant_url = d.url
        d.description = ("Baisse mesurée par l'historique local" if decision["kind"] == "verified"
                         else "Promo affichée par la marque ; historique local en construction")
        if decision["kind"] == "verified":
            d.merchant_ref = {"local_median": decision["reference"]}
        return d


_SIZE_RANK = {s: i for i, s in enumerate(["XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL"])}


def _size_order(size: str):
    up = size.upper()
    if up in _SIZE_RANK:
        return (0, _SIZE_RANK[up], up)
    try:
        return (1, float(size.replace(",", ".")), up)
    except ValueError:
        return (2, 0, up)
