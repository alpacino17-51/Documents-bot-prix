"""Vérification des prix : historique (CamelCamelCamel / Idealo / local) + comparaison Idealo -> verdict."""
from __future__ import annotations

import logging
from typing import Optional

from filters import is_fashion
from models import VERDICT_INFLATED, VERDICT_TRUE, VERDICT_UNVERIFIED, Deal, Verification
from utils import pct_below

log = logging.getLogger(__name__)


def _fmt(v: Optional[float]) -> str:
    return f"{v:.2f} €".replace(".", ",") if v is not None else "?"


def compute_verdict(price: float, original: Optional[float], displayed_disc: Optional[float],
                    history: Optional[dict], lowest_elsewhere: Optional[float], lowest_shop: str,
                    vcfg: dict) -> tuple[str, Optional[float], list[str]]:
    """Logique des verdicts (par ordre de priorité) :
    1. ✅ prix ≥ 35 % sous la médiane 90 j ET inférieur au plus bas ailleurs ;
    2. ⚠️ prix barré > plus bas 30 j (+5 %), prix barré jamais pratiqué sur 90 j, ou réduction réelle < 30 %
       alors que la réduction affichée est forte ;
    3. ❓ pas d'historique fiable.
    """
    notes: list[str] = []
    median = history.get("median_90") if history else None
    if not median:
        notes.append("Aucun historique de prix fiable trouvé")
        if lowest_elsewhere is not None and price > lowest_elsewhere:
            notes.append(f"Attention : moins cher ailleurs ({_fmt(lowest_elsewhere)} chez {lowest_shop or '?'})")
        return VERDICT_UNVERIFIED, None, notes

    real = pct_below(price, median)
    tol = float(vcfg.get("inflated_tolerance_pct", 5)) / 100
    low_30, max_90 = history.get("low_30"), history.get("max_90")

    inflated: list[str] = []
    if original:
        if low_30 is not None and original > low_30 * (1 + tol):
            inflated.append(f"prix barré {_fmt(original)} > plus bas des 30 derniers jours ({_fmt(low_30)})")
        if max_90 is not None and original > max_90 * (1 + tol):
            inflated.append(f"prix barré jamais pratiqué sur 90 j (max {_fmt(max_90)})")
    strong = float(vcfg.get("strong_displayed_discount_pct", 50))
    min_real = float(vcfg.get("inflated_min_real_discount_pct", 30))
    if displayed_disc is not None and displayed_disc >= strong and real < min_real:
        inflated.append(f"réduction réelle {real:.0f} % seulement (affichée {displayed_disc:.0f} %)")

    if lowest_elsewhere is None:
        elsewhere_ok = not vcfg.get("require_elsewhere_for_true_deal", False)
        if elsewhere_ok:
            notes.append("Comparaison autres marchands indisponible")
    else:
        elsewhere_ok = price < lowest_elsewhere - 0.005

    true_min = float(vcfg.get("true_deal_min_below_median_pct", 35))
    if real >= true_min and elsewhere_ok:
        if inflated:
            notes.append("ℹ️ Prix barré gonflé : " + " ; ".join(inflated))
        return VERDICT_TRUE, real, notes

    if inflated:
        notes.extend(inflated)
    elif not elsewhere_ok:
        notes.append(f"Moins cher ailleurs : {_fmt(lowest_elsewhere)} chez {lowest_shop or '?'}")
    else:
        notes.append(f"Réduction réelle {real:.0f} % < {true_min:.0f} % : pas une vraie affaire")
    notes.append(f"Vrai prix habituel ≈ {_fmt(median)} (médiane 90 j)")
    return VERDICT_INFLATED, real, notes


def amazon_locale(deal: Deal) -> str:
    host = (deal.link_host or "").lower()
    for loc in ("fr", "de", "it", "es"):
        if host.endswith(f"amazon.{loc}"):
            return loc
    return {"DE": "de", "IT": "it", "ES": "es"}.get(deal.site_country.upper(), "fr")


class Verifier:
    def __init__(self, cfg: dict, db, idealo=None, camel=None):
        self.cfg = cfg
        self.vcfg = cfg.get("verification", {})
        self.filters_cfg = cfg.get("filters", {})
        self.db = db
        self.idealo = idealo
        self.camel = camel

    def _camel_history(self, deal: Deal) -> Optional[dict]:
        if not (self.camel and self.camel.enabled and deal.asin):
            return None
        stats = self.camel.history(deal.asin, amazon_locale(deal))
        if not stats:
            return None
        if stats.get("current") and self.db is not None:
            self.db.add_price_observation(f"asin:{deal.asin}", stats["current"], "camel")
        if not stats.get("average"):
            return None
        # camel publie des stats « tout l'historique » : la moyenne sert d'approximation de la médiane 90 j
        return {"median_90": stats.get("average"), "max_90": stats.get("highest"), "low_30": None,
                "approx": True, "url": stats.get("url", "")}

    def _local_history(self, product_key: str) -> Optional[dict]:
        if not product_key or self.db is None:
            return None
        stats = self.db.price_stats(product_key)
        if stats["points"] < int(self.vcfg.get("min_local_history_points", 5)):
            return None
        return {"median_90": stats["median_90"], "low_30": stats["low_30"], "max_90": stats["max_90"],
                "approx": False}

    def verify(self, deal: Deal) -> Verification:
        v = Verification()
        idealo_res = None
        if is_fashion(deal, self.filters_cfg):
            v.skipped_idealo_reason = "mode/vêtements : comparaison Idealo non pertinente"
        elif self.idealo is not None and self.idealo.enabled:
            idealo_res = self.idealo.lookup(deal)
            if not idealo_res:
                v.skipped_idealo_reason = "aucune correspondance fiable sur Idealo"
        else:
            v.skipped_idealo_reason = "Idealo désactivé"

        if idealo_res:
            v.lowest_elsewhere = idealo_res.get("lowest_price")
            v.lowest_elsewhere_shop = idealo_res.get("lowest_shop", "")
            v.idealo_url = idealo_res.get("url", "")
            v.idealo_title = idealo_res.get("title", "")

        histories: dict[str, dict] = {}
        if deal.is_amazon:
            camel_hist = self._camel_history(deal)
            if camel_hist:
                histories["camel"] = camel_hist
        if idealo_res and idealo_res.get("history"):
            histories["idealo"] = {**idealo_res["history"], "approx": False}
        local_key = f"idealo:{idealo_res['product_id']}" if idealo_res and idealo_res.get("product_id") else (
            f"asin:{deal.asin}" if deal.asin else "")
        local = self._local_history(local_key)
        if local:
            histories["local"] = local

        priority = self.vcfg.get("history_priority_amazon" if deal.is_amazon else "history_priority_other",
                                 ["camel", "idealo", "local"])
        chosen_name, chosen = None, None
        for name in priority:          # 1) première source « exacte »
            h = histories.get(name)
            if h and not h.get("approx"):
                chosen_name, chosen = name, h
                break
        if chosen is None:             # 2) sinon première source approximative
            for name in priority:
                if histories.get(name):
                    chosen_name, chosen = name, histories[name]
                    break

        if chosen:
            v.median_90, v.low_30, v.max_90 = chosen.get("median_90"), chosen.get("low_30"), chosen.get("max_90")
            v.history_source = chosen_name or ""
            v.history_approx = bool(chosen.get("approx"))

        price = deal.price_eur if deal.price_eur is not None else deal.price
        if price is None:
            v.verdict = VERDICT_UNVERIFIED
            v.notes.append("Prix actuel inconnu")
            return v
        v.verdict, v.real_discount_pct, notes = compute_verdict(
            price, deal.original_price_eur, deal.displayed_discount_pct, chosen,
            v.lowest_elsewhere, v.lowest_elsewhere_shop, self.vcfg)
        v.notes.extend(notes)
        if v.history_approx:
            v.notes.append("Médiane approximée par la moyenne CamelCamelCamel")
        return v
