"""Étape A : la marque existe-t-elle vraiment hors du site source ?

Statuts :
- `verified`          : grande marque établie (liste `brands.verified_brands` + `filters.resale_brands`) ;
- `blocked`           : marque distributeur / exclusive connue pour ses prix barrés gonflés (`brands.blocked_brands`) ;
- `real`              : recherche web -> site officiel crédible trouvé OU au moins N revendeurs indépendants ;
- `fake_or_exclusive` : recherche web faite, ni site officiel ni revendeur indépendant ;
- `unknown`           : marque non identifiée, ou recherche impossible (budget / blocage) -> jamais ✅.
Résultats `real` / `fake_or_exclusive` mis en cache 30 jours (table `brand_cache`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from models import Deal
from utils import find_keywords, normalize_text
from web_search import WebSearch, registrable_label

log = logging.getLogger("brand_checker")

VERIFIED = "verified"
BLOCKED = "blocked"
REAL = "real"
FAKE = "fake_or_exclusive"
UNKNOWN = "unknown"

STATUS_LABELS = {VERIFIED: "vérifiée", REAL: "réelle (site officiel / revendeurs)", BLOCKED: "marque distributeur",
                 FAKE: "introuvable hors site source", UNKNOWN: "non vérifiée"}


@dataclass
class BrandInfo:
    name: str
    status: str
    official_domain: str = ""
    resellers: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def trusted(self) -> bool:
        return self.status in (VERIFIED, REAL)


def brand_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_text(name))


def _in_list(name: str, names: list[str]) -> Optional[str]:
    slug = brand_slug(name)
    for n in names:
        if slug and brand_slug(n) == slug:
            return n
    return None


def extract_brand(deal: Deal, brands_cfg: dict, resale_brands: list[str]) -> str:
    """Marque explicite de la source, sinon marque connue citée dans le titre, sinon 1er mot en MAJUSCULES."""
    if deal.brand:
        return deal.brand.strip()
    known = list(brands_cfg.get("blocked_brands", [])) + list(brands_cfg.get("verified_brands", [])) + list(resale_brands)
    title = deal.title or ""
    norm_title = normalize_text(title)
    # marque en tête du titre (cas Zalando / enseignes : « Pier One Chemise ... »)
    for name in sorted(known, key=len, reverse=True):
        if norm_title.startswith(normalize_text(name) + " ") or norm_title == normalize_text(name):
            return name
    # ailleurs dans le titre : marques d'au moins 3 caractères (évite « On », « LG »... pris pour des mots courants)
    hits = [h for h in find_keywords(title, known) if len(brand_slug(h)) >= 3]
    if hits:
        return max(hits, key=len)
    m = re.match(r"^\s*([A-Z][A-Z0-9&'\-]{1,24})(?:\s|$)", title)
    if m and not m.group(1).isdigit():
        return m.group(1)
    return ""


def official_domain_from(results: list[dict], slug: str, cfg: dict) -> str:
    """Domaine dont le nom correspond strictement à la marque (rejette les sites « marque-france », « -outlet »...)."""
    excluded = set(cfg.get("non_retail_domains", [])) | set(cfg.get("marketplace_domains", []))
    suspicious = [normalize_text(w) for w in cfg.get("suspicious_domain_words", [])]
    suffixes = [""] + list(cfg.get("official_domain_suffixes", ["official", "group", "running"]))
    for r in results:
        label = registrable_label(r["domain"]).replace("-", "")
        if not label or label in excluded:
            continue
        if any(w and w in label.replace(slug, "") for w in suspicious):
            continue
        if any(label == slug + s for s in suffixes) or (len(label) >= 4 and slug.startswith(label)):
            return r["domain"]
    return ""


def independent_resellers(results: list[dict], slug: str, source_host: str, cfg: dict) -> list[str]:
    """Domaines marchands distincts, hors marketplaces, hors réseaux sociaux, hors site source et hors sites
    « sosies » (nom de domaine reprenant la marque, typiques des fausses boutiques)."""
    excluded = set(cfg.get("non_retail_domains", [])) | set(cfg.get("marketplace_domains", []))
    source_label = registrable_label(source_host)
    found: list[str] = []
    for r in results:
        label = registrable_label(r["domain"]).replace("-", "")
        if not label or label in excluded or label == source_label or (slug and slug in label):
            continue
        if r["domain"] not in found:
            found.append(r["domain"])
    return found


class BrandChecker:
    def __init__(self, brands_cfg: dict, resale_brands: list[str], web: Optional[WebSearch], db=None):
        self.cfg = brands_cfg or {}
        self.resale = list(resale_brands or [])
        self.web = web
        self.db = db

    def check(self, brand: str, source_host: str = "") -> BrandInfo:
        if not brand:
            return BrandInfo("", UNKNOWN, reason="marque non identifiée")
        hit = _in_list(brand, self.cfg.get("blocked_brands", []))
        if hit:
            return BrandInfo(hit, BLOCKED, reason="marque distributeur / exclusive à prix barrés gonflés")
        slug = brand_slug(brand)
        domains = {brand_slug(k): v for k, v in (self.cfg.get("official_domains") or {}).items()}
        verified = _in_list(brand, list(self.cfg.get("verified_brands", [])) + self.resale)
        cached = self.db.brand_get(slug, float(self.cfg.get("cache_days", 30))) if self.db is not None else None
        if verified:
            official = domains.get(slug) or (cached or {}).get("official_domain", "")
            return BrandInfo(verified, VERIFIED, official_domain=official, reason="grande marque établie")
        if cached:
            return BrandInfo(brand, cached["status"], cached.get("official_domain", ""), cached.get("resellers", []),
                             cached.get("reason", "cache"))
        if len(slug) < 2 or self.web is None:
            return BrandInfo(brand, UNKNOWN, reason="recherche web désactivée")

        results = self.web.search(f"\"{brand}\" site officiel")
        if results is None:
            return BrandInfo(brand, UNKNOWN, reason="recherche web indisponible pour ce passage")
        official = domains.get(slug) or official_domain_from(results, slug, self.cfg)
        resellers = independent_resellers(results, slug, source_host, self.cfg)
        min_resellers = int(self.cfg.get("min_independent_resellers", 2))
        if official or len(resellers) >= min_resellers:
            info = BrandInfo(brand, REAL, official, resellers[:8],
                             f"site officiel {official}" if official else f"{len(resellers)} revendeurs indépendants")
        else:
            info = BrandInfo(brand, FAKE, "", resellers[:8], "aucun site officiel ni revendeur indépendant trouvé")
        if self.db is not None:
            self.db.brand_put(slug, info.status, info.official_domain,
                              {"resellers": info.resellers, "reason": info.reason, "name": brand})
        log.info("Marque « %s » : %s (%s)", brand, info.status, info.reason)
        return info

    def find_official_domain(self, info: BrandInfo) -> str:
        """Pour une marque vérifiée sans domaine connu : recherche (mise en cache avec la marque)."""
        if info.official_domain or self.web is None or info.status not in (VERIFIED, REAL):
            return info.official_domain
        slug = brand_slug(info.name)
        results = self.web.search(f"\"{info.name}\" site officiel")
        if not results:
            return ""
        domain = official_domain_from(results, slug, self.cfg)
        if domain and self.db is not None:
            self.db.brand_put(slug, info.status, domain, {"reason": info.reason, "name": info.name})
        info.official_domain = domain
        return domain
