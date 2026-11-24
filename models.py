"""Structures de données partagées entre les modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

VERDICT_TRUE = "true_deal"
VERDICT_INFLATED = "inflated"
VERDICT_UNVERIFIED = "unverified"

VERDICT_LABELS = {
    VERDICT_TRUE: "✅ Vraie affaire",
    VERDICT_INFLATED: "⚠️ Prix gonflé / à vérifier",
    VERDICT_UNVERIFIED: "❓ Non vérifié",
}


@dataclass
class Deal:
    source: str                      # ex. "dealabs", "mydealz", "camelcamelcamel-fr"
    source_id: str                   # identifiant unique dans la source
    title: str
    url: str                         # page du deal (ou page produit)
    site_country: str = "FR"         # pays du site source
    merchant: str = ""
    merchant_url: str = ""           # lien direct marchand (si connu)
    link_host: str = ""              # domaine du marchand (ex. www.amazon.de)
    price: Optional[float] = None    # dans la devise `currency`
    original_price: Optional[float] = None
    currency: str = "EUR"
    temperature: Optional[float] = None
    category: str = ""
    description: str = ""
    asin: str = ""
    ean: str = ""
    mpn: str = ""
    published_at: Optional[float] = None   # timestamp UNIX
    expired: bool = False
    price_error_context: bool = False      # vient d'un groupe/topic "erreur de prix"
    displayed_discount_override: Optional[float] = None  # % fourni directement par la source

    # rempli par le pipeline
    price_eur: Optional[float] = None
    original_price_eur: Optional[float] = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def displayed_discount_pct(self) -> Optional[float]:
        if self.price is not None and self.original_price and self.original_price > self.price:
            return round((1 - self.price / self.original_price) * 100, 1)
        if self.displayed_discount_override:
            return float(self.displayed_discount_override)
        return None

    @property
    def is_amazon(self) -> bool:
        host = (self.link_host or "").lower()
        return bool(self.asin) or "amazon." in host or "amazon" in (self.merchant or "").lower()

    def text_blob(self) -> str:
        return f"{self.title}\n{self.description}"


@dataclass
class Verification:
    verdict: str = VERDICT_UNVERIFIED
    median_90: Optional[float] = None
    low_30: Optional[float] = None
    max_90: Optional[float] = None
    history_source: str = ""          # "camel", "idealo", "local"
    history_approx: bool = False      # True si la "médiane" est une approximation (moyenne)
    lowest_elsewhere: Optional[float] = None
    lowest_elsewhere_shop: str = ""
    idealo_url: str = ""
    idealo_title: str = ""
    real_discount_pct: Optional[float] = None
    notes: list[str] = field(default_factory=list)
    skipped_idealo_reason: str = ""


@dataclass
class Alert:
    deal: Deal
    verification: Verification
    triggers: list[str]
    velocity: Optional[float] = None
