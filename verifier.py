"""Vérification stricte des prix (anti « faux bons plans ») -> verdict ✅ / ⚠️ / ❓.

Pipeline (toutes catégories, mode comprise) :
  A. marque : vérifiée / distributeur bloquée / réelle (site officiel ou revendeurs) / introuvable  [brand_checker]
  B. prix sur le site officiel de la marque                                                     [official_price]
  C. multi-comparateurs (idealo.fr, idealo.de, geizhals.eu) + présence web multi-marchands        [comparators]
  D. historique (CamelCamelCamel, Idealo, historique local) puis verdict strict ci-dessous.
Un prix barré affiché par le marchand ne suffit JAMAIS à obtenir ✅.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from brand_checker import BLOCKED, FAKE, REAL, UNKNOWN, VERIFIED, BrandChecker, extract_brand
from comparators import MultiComparator
from filters import is_fashion
from models import VERDICT_INFLATED, VERDICT_TRUE, VERDICT_UNVERIFIED, Deal, Verification
from official_price import OfficialPriceFinder
from utils import pct_below, url_host

log = logging.getLogger(__name__)

#: historiques qui reflètent le marché (et non un prix de référence affiché par le marchand lui-même)
MARKET_HISTORIES = {"camel", "idealo", "local", "idealo_bargain"}


def _fmt(v: Optional[float]) -> str:
    return f"{v:.2f} €".replace(".", ",") if v is not None else "?"


def compute_verdict(price: float, original: Optional[float], displayed_disc: Optional[float],
                    history: Optional[dict], lowest_elsewhere: Optional[float], lowest_shop: str,
                    vcfg: dict, *, brand_status: str = VERIFIED, others: Optional[int] = None,
                    comparison_complete: bool = False, official_price: Optional[float] = None,
                    history_eligible: bool = True, history_approx: bool = False
                    ) -> tuple[str, Optional[float], list[str]]:
    """Verdict strict.

    ⚠️ : marque bloquée / introuvable ; vendu exclusivement sur le site source ; prix barré > prix officiel (+10 %)
         ou > plus bas 30 j (+5 %) ou jamais pratiqué sur 90 j ; promo proche du prix officiel ; réduction réelle
         faible face au marché ; moins cher ailleurs.
    ✅ : marque vérifiée/réelle + ≥ 1 autre marchand (ou prix officiel vérifié) + prix ≥ 35 % sous la médiane 90 j
         (historique de marché) OU ≥ 30 % sous le prix officiel, ET inférieur au prix le plus bas ailleurs.
    ❓ : le reste (marque réelle mais pas assez de données, ou marque non vérifiable).
    """
    notes: list[str] = []
    if brand_status == BLOCKED:
        return VERDICT_INFLATED, None, ["Prix gonflé : marque distributeur / exclusive (liste blocked_brands)"]
    if brand_status == FAKE:
        return VERDICT_INFLATED, None, ["Prix gonflé : marque sans site officiel ni revendeur indépendant"]
    if others == 0 and comparison_complete and official_price is None:
        return VERDICT_INFLATED, None, ["Prix gonflé : vendu exclusivement sur le site source "
                                        "(aucun autre marchand indépendant trouvé)"]

    tol = float(vcfg.get("inflated_tolerance_pct", 5)) / 100
    off_tol = float(vcfg.get("official_inflated_tolerance_pct", 10)) / 100
    inflated: list[str] = []
    promo_close = False
    if official_price:
        if original and original > official_price * (1 + off_tol):
            inflated.append(f"prix barré {_fmt(original)} > prix officiel {_fmt(official_price)}")
        if price >= official_price * (1 - off_tol):
            promo_close = True
            inflated.append(f"prix promo proche du prix officiel ({_fmt(official_price)})")

    median = history.get("median_90") if history else None
    low_30 = history.get("low_30") if history else None
    max_90 = history.get("max_90") if history else None
    real = pct_below(price, median) if median else None
    if original:
        if low_30 and original > low_30 * (1 + tol):
            inflated.append(f"prix barré {_fmt(original)} > plus bas des 30 derniers jours ({_fmt(low_30)})")
        if max_90 and original > max_90 * (1 + tol):
            inflated.append(f"prix barré jamais pratiqué sur 90 j (max {_fmt(max_90)})")
    strong = float(vcfg.get("strong_displayed_discount_pct", 50))
    min_real = float(vcfg.get("inflated_min_real_discount_pct", 30))
    if real is not None and displayed_disc is not None and displayed_disc >= strong and real < min_real:
        inflated.append(f"réduction réelle {real:.0f} % seulement (affichée {displayed_disc:.0f} %)")

    elsewhere_ok = lowest_elsewhere is None or price < lowest_elsewhere - 0.005
    cheaper_than_market = lowest_elsewhere is not None and price < lowest_elsewhere - 0.005
    true_min = float(vcfg.get("true_deal_min_below_median_pct", 35))
    official_min = float(vcfg.get("true_deal_min_below_official_pct", 30))
    brand_ok = brand_status in (VERIFIED, REAL)
    market_ok = (others or 0) >= 1 or official_price is not None
    # ✅ exige une référence réelle ET d'être moins cher que le plus bas prix connu ailleurs
    # une référence approximative (prix habituel comparateur, moyenne camel) ne suffit pas seule pour ✅ :
    # elle peut être gonflée par des revendeurs (ex. sets LEGO retirés) -> il faut aussi le prix officiel
    median_ok = (real is not None and history_eligible and real >= true_min and cheaper_than_market
                 and (not history_approx or official_price is not None))
    approx_only = (real is not None and history_eligible and real >= true_min and cheaper_than_market
                   and history_approx and official_price is None)
    official_ok = bool(official_price and price <= official_price * (1 - official_min / 100)) and elsewhere_ok
    ref_ok = median_ok or official_ok

    if brand_ok and market_ok and ref_ok and not promo_close:
        barred = [i for i in inflated if i.startswith("prix barré")]
        if barred:
            notes.append("ℹ️ Prix barré gonflé : " + " ; ".join(barred))
        return VERDICT_TRUE, real, notes

    usual = f"Vrai prix habituel ≈ {_fmt(median)} (médiane 90 j)" if median else (
        f"Prix officiel : {_fmt(official_price)}" if official_price else "")
    if inflated:
        notes.extend(inflated)
    elif not elsewhere_ok:
        notes.append(f"Moins cher ailleurs : {_fmt(lowest_elsewhere)} chez {lowest_shop or '?'}")
    elif real is not None and real < true_min:
        notes.append(f"Réduction réelle {real:.0f} % < {true_min:.0f} % : réduction faible face au marché")
    if inflated or not elsewhere_ok or (real is not None and real < true_min):
        if usual:
            notes.append(usual)
        return VERDICT_INFLATED, real, notes

    if not brand_ok:
        notes.append("Marque non vérifiable pour l'instant")
    elif not market_ok:
        notes.append("Aucun autre marchand ni prix officiel confirmé")
    elif approx_only:
        notes.append("Référence de prix approximative : confirmation par le prix officiel nécessaire")
    elif real is not None and lowest_elsewhere is None:
        notes.append("Prix des autres marchands inconnu : impossible de confirmer qu'il est le plus bas")
    else:
        notes.append("Aucun historique de prix ni comparatif exploitable")
    return VERDICT_UNVERIFIED, real, notes


def amazon_locale(deal: Deal) -> str:
    host = (deal.link_host or "").lower()
    if host.endswith("amazon.co.uk"):
        return "uk"
    for loc in ("fr", "de", "it", "es"):
        if host.endswith(f"amazon.{loc}"):
            return loc
    return {"DE": "de", "IT": "it", "ES": "es", "UK": "uk"}.get(deal.site_country.upper(), "fr")


def source_host(deal: Deal) -> str:
    """Site où le deal est vendu (pour la règle d'exclusivité)."""
    return (deal.link_host or url_host(deal.merchant_url) or url_host(deal.url) or "").lower().removeprefix("www.")


class Verifier:
    def __init__(self, cfg: dict, db, idealo=None, camel=None, *, brand_checker: Optional[BrandChecker] = None,
                 comparator: Optional[MultiComparator] = None, official: Optional[OfficialPriceFinder] = None,
                 web=None):
        self.cfg = cfg
        self.vcfg = cfg.get("verification", {})
        self.filters_cfg = cfg.get("filters", {})
        self.brands_cfg = cfg.get("brands", {})
        self.resale = self.filters_cfg.get("resale_brands", [])
        self.db = db
        self.idealo = idealo
        self.camel = camel
        self.web = web
        self.brand_checker = brand_checker or BrandChecker(self.brands_cfg, self.resale, web, db)
        http = getattr(idealo, "http", None)
        self.comparator = comparator or MultiComparator(cfg.get("comparators", {}), http, db, idealo_fr=idealo,
                                                        web=web, brands_cfg=self.brands_cfg)
        self.official = official or OfficialPriceFinder(cfg.get("official_price", {}), http, web, db)
        self.run_started = time.monotonic()

    def reset_run(self) -> None:
        self.run_started = time.monotonic()
        if hasattr(self.comparator, "reset_run"):
            self.comparator.reset_run()

    # ------------------------------------------------------------------ budget
    def _web_budget_out(self) -> bool:
        w = self.web
        return w is not None and w.enabled and not w.blocked and w.budget_exhausted

    def _defer_without_web(self, deal: Deal) -> bool:
        """La recherche web est nécessaire mais indisponible : on reporte la vérification au passage suivant.
        Budget épuisé -> toujours ; recherche bloquée (anti-robot) -> seulement pour les sites marchands directs
        (les erreurs de prix communautaires sont urgentes : elles partent en ❓ plutôt que d'attendre)."""
        w = self.web
        if w is None or not w.enabled:
            return False
        if self._web_budget_out():
            return True
        community = self.cfg.get("notifications", {}).get(
            "community_sources", ["dealabs", "pepper_eu", "reddit", "camelcamelcamel"])
        return w.blocked and deal.origin not in community

    def _time_out(self) -> bool:
        limit = float(self.vcfg.get("max_seconds_per_run", 150))
        return limit > 0 and time.monotonic() - self.run_started > limit

    # ------------------------------------------------------------------ historiques
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

    def _choose_history(self, deal: Deal, idealo_res: Optional[dict]) -> tuple[str, Optional[dict]]:
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
        ref = deal.merchant_ref or {}
        if ref.get("market_median"):          # prix habituel calculé par un comparateur (Idealo « bons plans »)
            histories["idealo_bargain"] = {"median_90": ref["market_median"], "low_30": None, "max_90": None,
                                           "approx": True}
        if ref.get("median") or ref.get("low_30"):
            # références affichées par le marchand lui-même : utiles pour détecter un prix gonflé, jamais pour ✅
            histories["merchant"] = {"median_90": ref.get("median"), "low_30": ref.get("low_30"), "max_90": None,
                                     "approx": True}
        priority = list(self.vcfg.get("history_priority_amazon" if deal.is_amazon else "history_priority_other",
                                      ["camel", "idealo", "local"]))
        for extra in ("idealo_bargain", "merchant"):
            if extra not in priority:
                priority.append(extra)
        for name in priority:                 # 1) première source exacte
            h = histories.get(name)
            if h and not h.get("approx"):
                chosen_name, chosen = name, h
                break
        else:                                 # 2) sinon première source approximative
            chosen_name, chosen = next(((n, histories[n]) for n in priority if histories.get(n)), ("", None))
        if chosen and chosen.get("low_30") is None and ref.get("low_30"):
            chosen = {**chosen, "low_30": ref["low_30"]}     # le plus bas 30 j légal affiché par le marchand
        return chosen_name, chosen

    # ------------------------------------------------------------------ pipeline
    def verify(self, deal: Deal) -> Verification:
        v = Verification()
        if self._time_out():
            v.deferred = True
            return v
        host = source_host(deal)
        fashion = is_fashion(deal, self.filters_cfg)

        # Étape A — marque
        brand = extract_brand(deal, self.brands_cfg, self.resale)
        binfo = self.brand_checker.check(brand, host)
        v.brand, v.brand_status = binfo.name or brand, binfo.status
        if binfo.status == UNKNOWN and brand and self._defer_without_web(deal):
            v.deferred = True
            return v
        if binfo.status in (BLOCKED, FAKE):
            v.verdict, v.real_discount_pct, notes = compute_verdict(
                deal.price_eur or deal.price or 0, deal.original_price_eur, deal.displayed_discount_pct, None, None,
                "", self.vcfg, brand_status=binfo.status)
            v.notes.extend(notes + ([binfo.reason] if binfo.reason else []))
            return v

        # Étape C — comparateurs + présence web
        cmp = self.comparator.compare(deal, fashion=fashion, brand=binfo.name or brand, source_host=host)
        if not cmp.get("complete") and self._defer_without_web(deal):
            v.deferred = True
            return v
        v.others_count = cmp.get("others")
        v.comparators = list(cmp.get("checked", []))
        v.lowest_elsewhere, v.lowest_elsewhere_shop = cmp.get("lowest"), cmp.get("lowest_shop", "")
        idealo_res = cmp.get("idealo")
        if idealo_res:
            v.idealo_url, v.idealo_title = idealo_res.get("url", ""), idealo_res.get("title", "")
            if idealo_res.get("lowest_is_same_merchant"):
                v.notes.append(f"Seule offre Idealo : celle de {idealo_res.get('lowest_shop') or 'ce marchand'}")
        if fashion:
            v.skipped_idealo_reason = "mode : comparaison par recherche web multi-marchands"
        elif not idealo_res:
            v.skipped_idealo_reason = "aucune correspondance fiable sur Idealo"

        # Étape B — prix sur le site officiel
        official_unavailable = False
        if binfo.trusted:
            web_ok = self.web is None or self.web.available
            domain = binfo.official_domain or self.brand_checker.find_official_domain(binfo)
            official = self.official.find(deal, domain, binfo.name) if domain else None
            if official:
                v.official_price, v.official_url = official["price"], official["url"]
            # recherche impossible (budget / blocage) : domaine inconnu ou page officielle non cherchée
            official_unavailable = official is None and (not web_ok or getattr(self.official, "last_unavailable", False)
                                                         or (not domain and not web_ok))

        # Étape D — historique puis verdict
        chosen_name, chosen = self._choose_history(deal, idealo_res)
        if chosen:
            v.median_90, v.low_30, v.max_90 = chosen.get("median_90"), chosen.get("low_30"), chosen.get("max_90")
            v.history_source = chosen_name
            v.history_approx = bool(chosen.get("approx"))
        price = deal.price_eur if deal.price_eur is not None else deal.price
        if price is None:
            v.verdict = VERDICT_UNVERIFIED
            v.notes.append("Prix actuel inconnu")
            return v
        v.verdict, v.real_discount_pct, notes = compute_verdict(
            price, deal.original_price_eur, deal.displayed_discount_pct, chosen, v.lowest_elsewhere,
            v.lowest_elsewhere_shop, self.vcfg, brand_status=binfo.status, others=v.others_count,
            comparison_complete=bool(cmp.get("complete")), official_price=v.official_price,
            history_eligible=chosen_name in MARKET_HISTORIES, history_approx=v.history_approx)
        if (v.verdict == VERDICT_UNVERIFIED and any(n.startswith("Référence de prix approximative") for n in notes)
                and official_unavailable and self._defer_without_web(deal)):
            v.deferred = True       # ✅ possible : on attend de pouvoir vérifier le prix officiel
            return v
        v.notes.extend(notes)
        if v.history_approx and v.median_90 is not None:
            v.notes.append("Médiane approximée par la moyenne CamelCamelCamel" if chosen_name == "camel"
                           else "Médiane approximée (prix habituel comparateur / référence marchand)")
        return v
