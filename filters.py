"""Filtres (prix, catégorie, mode) et critères de déclenchement."""
from __future__ import annotations

import re
from typing import Optional

from models import Deal
from utils import find_keywords, normalize_text, remove_patterns, url_host


def is_uk_shop(deal: Deal, general_cfg: dict) -> bool:
    host = (deal.link_host or url_host(deal.merchant_url) or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in general_cfg.get("uk_hosts", []))


def price_ok(deal: Deal, general_cfg: dict) -> tuple[bool, str]:
    max_price = float(general_cfg.get("max_price_eur", 200))
    if is_uk_shop(deal, general_cfg):
        # au-delà de 150 € une commande venant du Royaume-Uni subit droits de douane + TVA à l'import
        max_price = min(max_price, float(general_cfg.get("uk_max_price_eur", 150)))
    if deal.price_eur is None:
        if general_cfg.get("allow_unknown_price", False):
            return True, "prix inconnu (autorisé)"
        return False, "prix inconnu"
    if deal.price_eur > max_price:
        return False, f"prix {deal.price_eur:.2f} € > {max_price:.0f} €"
    return True, ""


def resale_brands_found(deal: Deal, filters_cfg: dict) -> list[str]:
    return find_keywords(deal.title, filters_cfg.get("resale_brands", []))


def category_ok(deal: Deal, filters_cfg: dict) -> tuple[bool, str]:
    """Catégories incluses (High-tech/informatique, Gaming, Mode & sport) ou marque à fort potentiel de revente."""
    excluded = find_keywords(deal.title, filters_cfg.get("exclude_title_keywords", []))
    if excluded:
        return False, f"mot exclu « {excluded[0]} »"
    if deal.category and find_keywords(deal.category, filters_cfg.get("category_include_keywords", [])):
        return True, f"catégorie « {deal.category} »"
    if not deal.category or deal.price_error_context:
        hits = find_keywords(f"{deal.title} | {deal.category_hint}", filters_cfg.get("title_category_keywords", []))
        if hits:
            return True, f"mot-clé catégorie « {hits[0]} »"
    brands = resale_brands_found(deal, filters_cfg)
    if brands:
        return True, f"marque revente « {brands[0]} »"
    return False, f"hors catégories ({deal.category or 'inconnue'})"


def is_fashion(deal: Deal, filters_cfg: dict) -> bool:
    """Vêtements / chaussures / mode : pas de comparaison Idealo (tailles et coloris trop variables)."""
    if deal.category and find_keywords(deal.category, filters_cfg.get("fashion_category_keywords", [])):
        return True
    if find_keywords(deal.title, filters_cfg.get("fashion_keywords", [])):
        return True
    norm = normalize_text(deal.title)
    for pat in filters_cfg.get("fashion_size_patterns", []):
        try:
            if re.search(pat, norm):
                return True
        except re.error:
            continue
    return False


def keyword_trigger(deal: Deal, triggers_cfg: dict) -> list[str]:
    text = remove_patterns(deal.text_blob(), triggers_cfg.get("negation_patterns", []))
    return find_keywords(text, triggers_cfg.get("keywords", []))


def evaluate_triggers(deal: Deal, triggers_cfg: dict, velocity: Optional[float],
                      resale_brands: Optional[list[str]] = None) -> list[str]:
    """Retourne la liste lisible des critères remplis (vide = pas d'alerte)."""
    found: list[str] = []
    kws = keyword_trigger(deal, triggers_cfg)
    if kws:
        found.append("Mot-clé : " + ", ".join(f"« {k} »" for k in kws[:3]))
    elif deal.price_error_context and triggers_cfg.get("price_error_context_counts_as_keyword", True):
        found.append("Publié dans un groupe/topic « Erreur de prix »")

    disc = deal.displayed_discount_pct
    min_disc = float(triggers_cfg.get("min_displayed_discount_pct", 70))
    resale_min = float(triggers_cfg.get("resale_brand_min_discount_pct", min_disc))
    if disc is not None and disc >= min_disc:
        found.append(f"Réduction affichée {disc:.0f} % (≥ {min_disc:.0f} %)")
    elif disc is not None and disc >= resale_min and resale_brands:
        brands = find_keywords(deal.title, resale_brands)
        if brands:
            found.append(f"Réduction affichée {disc:.0f} % (≥ {resale_min:.0f} %, marque revente « {brands[0]} »)")

    vcfg = triggers_cfg.get("velocity", {})
    if vcfg.get("enabled", True) and velocity is not None and deal.temperature is not None:
        thr = float(vcfg.get("threshold_deg_per_min", 5.0))
        if velocity >= thr and deal.temperature >= float(vcfg.get("min_temperature", 0)):
            found.append(f"Vélocité {velocity:.1f} °/min (≥ {thr:g})")
    return found
