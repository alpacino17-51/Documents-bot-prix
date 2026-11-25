"""Fonctions utilitaires : prix, normalisation de texte/URL, identifiants produit, similarité."""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------- texte

_WS = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    """Minuscules, sans accents, apostrophes unifiées, espaces compactés."""
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = text.replace("’", "'").replace("`", "'").replace("ß", "ss")
    text = strip_accents(text).lower()
    return _WS.sub(" ", text).strip()


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment, "lxml")
    return _WS.sub(" ", soup.get_text(" ")).strip()


def keyword_regex(keyword: str) -> re.Pattern:
    kw = re.escape(normalize_text(keyword))
    return re.compile(rf"(?<![a-z0-9]){kw}(?![a-z0-9])")


def find_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    """Retourne les mots-clés (forme d'origine) présents dans `text` (comparaison normalisée, mots entiers)."""
    norm = normalize_text(text)
    found = []
    for kw in keywords:
        if kw and keyword_regex(kw).search(norm):
            found.append(kw)
    return found


def remove_patterns(text: str, patterns: Iterable[str]) -> str:
    norm = normalize_text(text)
    for pat in patterns:
        try:
            norm = re.sub(normalize_text(pat), " ", norm)
        except re.error:
            norm = norm.replace(normalize_text(pat), " ")
    return norm


# ---------------------------------------------------------------- prix

CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "eur": "EUR", "euro": "EUR", "euros": "EUR",
                    "gbp": "GBP", "usd": "USD", "chf": "CHF", "zl": "PLN", "zł": "PLN", "pln": "PLN"}

_NUM = r"\d{1,3}(?:[   .,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CUR = r"€|£|\$|eur(?:os?)?\b|gbp\b|usd\b|chf\b|pln\b|zł"
# (?<![\w.,]) : un prix ne commence pas au milieu d'une référence (« 5KSM175 299,00 € » ≠ 175 299 €)
_PRICE_AFTER = re.compile(rf"(?<![\w.,])(?P<num>{_NUM})\s*(?P<cur>{_CUR})", re.I)
_PRICE_BEFORE = re.compile(rf"(?P<cur>{_CUR})\s*(?P<num>{_NUM})", re.I)


def _to_float(num: str) -> Optional[float]:
    s = num.replace(" ", " ").replace(" ", " ").strip()
    s = s.replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if len(tail) == 3 and head:          # 1,299 -> milliers
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
            if s.count(".") > 1:
                parts = s.split(".")
                s = "".join(parts[:-1]) + "." + parts[-1]
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3 and head:          # 1.299 -> milliers (format DE/FR)
            s = s.replace(".", "")
        elif s.count(".") > 1:
            parts = s.split(".")
            s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except ValueError:
        return None


def normalize_price_text(text: str) -> str:
    """Recolle les prix éclatés par le HTML : « 0 , 99 € » -> « 0,99 € » ; « 13 €01 » / « 339€ 00 » -> « 339,00 € »
    (la partie entière ne doit pas déjà avoir de décimales : « 29,99 € 12 avis » reste intact)."""
    text = re.sub(r"(\d)\s+,\s*(\d{2})\s*(€)", r"\1,\2 \3", text)
    return re.sub(r"(?<![\d.,])(\d{1,4})\s?€\s?(\d{2})(?!\d)", r"\1,\2 €", text)


def find_price_spans(text: str) -> list[tuple[int, float, str]]:
    """(position, valeur, devise) de chaque prix du texte (déjà normalisé par normalize_price_text)."""
    hits: list[tuple[int, float, str]] = []
    taken: list[tuple[int, int]] = []
    for rx in (_PRICE_AFTER, _PRICE_BEFORE):
        for m in rx.finditer(text):
            span = m.span()
            if any(not (span[1] <= a or span[0] >= b) for a, b in taken):
                continue
            value = _to_float(m.group("num"))
            if value is None:
                continue
            taken.append(span)
            hits.append((span[0], value, CURRENCY_SYMBOLS.get(m.group("cur").lower(), "EUR")))
    hits.sort()
    return hits


def find_prices(text: str) -> list[tuple[float, str]]:
    """Toutes les valeurs monétaires accompagnées d'une devise, dans l'ordre d'apparition."""
    if not text:
        return []
    text = normalize_price_text(html_lib.unescape(text))
    return [(v, c) for _, v, c in find_price_spans(text)]


def parse_price(text, default_currency: Optional[str] = None) -> tuple[Optional[float], Optional[str]]:
    """Analyse un prix isolé ("1.299,99 €", "£12.99", "12,5", 19.9...)."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return float(text), default_currency
    prices = find_prices(str(text))
    if prices:
        return prices[0]
    m = re.search(_NUM, str(text))
    if m:
        return _to_float(m.group(0)), default_currency
    return None, None


def to_eur(value: Optional[float], currency: Optional[str], fx: dict[str, float]) -> Optional[float]:
    if value is None:
        return None
    rate = fx.get((currency or "EUR").upper())
    if rate is None:
        return None
    return round(value * rate, 2)


def pct_below(price: float, reference: float) -> float:
    """Pourcentage d'écart sous la référence (positif = moins cher)."""
    return round((1 - price / reference) * 100, 1)


# ---------------------------------------------------------------- URL

_TRACKING_PREFIXES = ("utm_", "pf_rd_", "pd_rd_", "ref", "tag", "linkcode", "creative", "camp", "ascsubtag",
                      "gclid", "fbclid", "affid", "aff_", "awc", "ranmid", "ranEaid", "ransiteid", "psc",
                      "smid", "th", "spla", "content-id", "sr", "qid", "keywords", "crid", "sprefix", "dib",
                      "dib_tag", "mc_", "srsltid", "_encoding", "xtor", "cmp", "origin", "eaf-", "affiliate",
                      "aff", "subid", "clickref", "cjevent", "irclickid", "wt_mc", "ito", "emc")
_AMAZON_ASIN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin|product|o/asin)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    url = html_lib.unescape(url.strip())
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if "amazon." in host:
        m = _AMAZON_ASIN_URL.search(parts.path)
        if m:
            return f"https://{host}/dp/{m.group(1).upper()}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith(_TRACKING_PREFIXES)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def clean_url(url: str) -> str:
    """Retire les paramètres de suivi/affiliation et l'ancre, sans toucher au domaine ni au chemin."""
    if not url:
        return ""
    try:
        parts = urlsplit(html_lib.unescape(url.strip()))
    except ValueError:
        return url
    if "amazon." in (parts.hostname or ""):
        m = _AMAZON_ASIN_URL.search(parts.path)
        if m:
            return f"{parts.scheme or 'https'}://{parts.netloc}/dp/{m.group(1).upper()}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(_TRACKING_PREFIXES)]
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urlencode(query), ""))


def url_host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- identifiants produit

def extract_asin(*texts: str) -> str:
    for t in texts:
        if not t:
            continue
        m = _AMAZON_ASIN_URL.search(t)
        if m:
            return m.group(1).upper()
        m = re.search(r"\bASIN\s*[:#]?\s*([A-Z0-9]{10})\b", t, re.I)
        if m:
            return m.group(1).upper()
    return ""


def _gtin_valid(code: str) -> bool:
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def extract_ean(*texts: str) -> str:
    for t in texts:
        if not t:
            continue
        for m in re.finditer(r"\b(?:ean(?:-?13)?|gtin(?:-?1[34])?|upc|code[- ]barres?)\s*[:#]?\s*(\d{8,14})\b", t, re.I):
            if _gtin_valid(m.group(1)):
                return m.group(1)
        for m in re.finditer(r"(?<![\d.,])(\d{13})(?![\d.,])", t):
            if _gtin_valid(m.group(1)) and not m.group(1).startswith(("1", "2")):
                return m.group(1)
    return ""


def extract_mpn(*texts: str) -> str:
    rx = re.compile(
        r"(?:\br[ée]f(?:[ée]rence)?(?: fabricant| constructeur)?|\bmpn|\bmod[eè]le|\bmodellnummer|\bmodel(?: no\.?| number)?"
        r"|\bpart number|\bart\.?-nr\.?|\bherstellernummer)\s*[:#]\s*([A-Z0-9][A-Z0-9\-_/.]{2,30})",
        re.I,
    )
    for t in texts:
        if not t:
            continue
        m = rx.search(t)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).strip(".-/").upper()
    return ""


# ---------------------------------------------------------------- titres et similarité

_STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "a", "au", "aux", "en", "pour", "sur", "avec", "chez",
    "via", "the", "and", "for", "with", "of", "in", "at", "on", "der", "die", "das", "und", "mit", "fur", "bei",
    "el", "los", "las", "con", "por", "para", "il", "lo", "di", "da", "per", "con", "prix", "offre", "promo",
    "livraison", "gratuite", "offert", "offerte", "deal", "inclus", "incl", "dont", "ttc", "neuf", "new",
    "amazon", "fr", "de", "es", "it", "uk", "prime", "membres", "code", "coupon", "reduction", "remise",
}
_NOISE = re.compile(
    r"\[[^\]]*\]|\([^)]*\)|"                         # crochets / parenthèses
    rf"(?:{_NUM})\s*(?:{_CUR})|(?:{_CUR})\s*(?:{_NUM})|"   # prix
    r"-?\d{1,3}\s?%|"                                  # pourcentages
    r"[|•~*!?:;\"«»“”]",
    re.I,
)


def _join_model_tokens(text: str) -> str:
    # "wh-1000xm5" -> "wh1000xm5", "rtx 4070" reste séparé
    return re.sub(r"(?<=[a-z0-9])[-_/](?=[a-z0-9])", "", text)


def tokens(text: str) -> list[str]:
    norm = normalize_text(_NOISE.sub(" ", html_lib.unescape(text or "")))
    norm = _join_model_tokens(norm)
    return [t for t in re.findall(r"[a-z0-9+]+", norm) if t not in _STOPWORDS and len(t) > 1 or t.isdigit()]


def normalize_title(text: str) -> str:
    return " ".join(sorted(set(tokens(text))))


def title_hash(text: str) -> str:
    norm = normalize_title(text)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20] if norm else ""


def model_tokens(toks: Iterable[str]) -> set[str]:
    return {t for t in toks if re.search(r"\d", t) and re.search(r"[a-z]", t) and len(t) >= 3}


def title_similarity(deal_title: str, candidate_title: str) -> float:
    ta, tb = set(tokens(deal_title)), set(tokens(candidate_title))
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    containment = len(inter) / min(len(ta), len(tb))
    jaccard = len(inter) / len(ta | tb)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    score = 0.5 * containment + 0.25 * jaccard + 0.25 * seq
    ma, mb = model_tokens(ta), model_tokens(tb)
    if ma and mb and not (ma & mb):
        score *= 0.4           # références modèles différentes (ex. XM4 vs XM5)
    elif ma and not mb:
        score *= 0.6
    return round(score, 3)


def search_query_from_title(title: str, max_tokens: int = 7) -> str:
    """Titre nettoyé (marque + modèle) pour une recherche comparateur."""
    raw = _NOISE.sub(" ", html_lib.unescape(title or ""))
    raw = re.split(r"\s(?:-|–|—|chez|bei|at|en|sur|via|à|a|für|pour|por|per)\s", raw, maxsplit=1)[0] or raw
    words = [w for w in re.findall(r"[\w+\-]+", raw, re.UNICODE)
             if normalize_text(w) not in _STOPWORDS and len(w) > 1]
    return " ".join(words[:max_tokens])
