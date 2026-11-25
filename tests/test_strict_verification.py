"""LOT 1 : vérification stricte marque / site officiel / multi-marchands, filtrage Telegram, liens directs."""
import copy
import json
from urllib.parse import unquote_plus

import pytest
from conftest import FakeHttp, ddg_html, idealo_search_html, read_fixture

import http_client
from brand_checker import BLOCKED, FAKE, REAL, VERIFIED, BrandChecker, extract_brand, official_domain_from
from comparators import MultiComparator, idealo_other_merchants
from db import StateDB
from http_client import FetchError, HttpClient
from main import Bot
from models import VERDICT_INFLATED, VERDICT_TRUE, VERDICT_UNVERIFIED, Deal, Verification
from official_price import OfficialPriceFinder, extract_official_price
from sources.idealo import IdealoSource
from verifier import Verifier
from web_search import WebSearch, parse_results, registrable_label

FAKE_BRAND_ZALANDO = """<html><body>
<article><a href="https://www.zalando.fr/maison-verone-manteau-en-laine-camel-mv121u00a-o11.html">
  <h3><span>Maison Vérone</span><span>Manteau en laine - camel</span></h3></a>
  <p><span>89,95 €</span></p>
  <p><span>Prix habituel :</span> <span>299,95 €</span> <span>-70%</span></p>
</article>
<article><a href="https://www.zalando.fr/pier-one-chemise-blanc-pi922d0aa-a11.html">
  <h3><span>Pier One</span><span>Chemise - blanc</span></h3></a>
  <p><span>9,99 €</span></p>
  <p><span>Prix habituel :</span> <span>39,99 €</span> <span>-75%</span></p>
</article>
</body></html>"""

SONY_OFFICIAL_PAGE = """<html><head><title>WH-1000XM5 | Sony FR</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Sony WH-1000XM5",
"sku":"WH1000XM5B.CE7","brand":{"@type":"Brand","name":"Sony"},
"offers":{"@type":"Offer","price":"379.00","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}</script>
</head><body><h1>WH-1000XM5</h1></body></html>"""


def ddg_router(table: dict):
    """Réponse DuckDuckGo selon la requête (sous-chaîne de la requête -> liste de (url, titre))."""
    def respond(url: str):
        query = unquote_plus(url.split("q=", 1)[1].split("&", 1)[0]).lower()
        for needle, results in table.items():
            if needle.lower() in query:
                return ddg_html(results)
        return ddg_html([])
    return respond


def bot_config(cfg, **sources_on):
    c = copy.deepcopy(cfg)
    for name in c["sources"]:
        if isinstance(c["sources"][name], dict) and name not in ("idealo", "idealo_de"):
            c["sources"][name]["enabled"] = name in sources_on
    for name, extra in sources_on.items():
        c["sources"][name].update(extra)
    c["general"]["silent_first_run"] = False
    c["sites_catalog"]["sites"] = []
    c["trusted_dtc_brands"]["enabled"] = False
    c["web_search"]["min_delay_seconds"] = 0
    c["comparators"]["geizhals"]["enabled"] = False
    c["sources"]["idealo_de"]["enabled"] = False
    return c


class Capture:
    def __init__(self):
        self.alerts = []

    def send(self, alert, test=False):
        self.alerts.append(alert)
        return True


# ---------------------------------------------------------------- 1) fausse marque Zalando -> ⚠️, non envoyée

def test_fake_zalando_brand_is_inflated_and_not_sent(cfg, tmp_path):
    c = bot_config(cfg, zalando={"page_urls": ["https://www.zalando.fr/promo-homme/?order=sale"],
                                 "min_interval_minutes": 0})
    http = FakeHttp({
        "zalando.fr/promo-homme": FAKE_BRAND_ZALANDO,
        "duckduckgo.com": ddg_router({
            "maison vérone": [
                ("https://www.zalando.fr/maison-verone/", "Maison Vérone | Zalando"),
                ("https://www.maisonverone-france.fr/", "Maison Vérone France - Soldes -70%"),
                ("https://www.vinted.fr/brand/maison-verone", "Maison Vérone - Vinted"),
            ],
        }),
    })
    db = StateDB(tmp_path / "s.db")
    notifier = Capture()
    bot = Bot(c, None, db, notifier, http=http)
    stats = bot.run_once()

    assert notifier.alerts == []                                   # rien n'est envoyé sur Telegram
    assert stats["verdicts"] == {VERDICT_INFLATED: 2} and stats["filtered"] == 2
    rows = db.conn.execute("SELECT title, verdict, sent FROM alerts ORDER BY id").fetchall()
    assert [(r["verdict"], r["sent"]) for r in rows] == [(VERDICT_INFLATED, 0), (VERDICT_INFLATED, 0)]
    assert db.brand_get("maisonverone")["status"] == FAKE           # mis en cache 30 j
    ddg_calls = [u for u in http.calls if "duckduckgo" in u]
    assert len(ddg_calls) == 1                                     # Pier One : rejet immédiat, sans recherche
    # 2e passage : déjà traités -> jamais revérifiés
    assert bot.run_once()["duplicates"] == 2
    assert len([u for u in http.calls if "duckduckgo" in u]) == 1
    db.close()


def test_fake_brand_check_details(cfg, tmp_path):
    db = StateDB(tmp_path / "b.db")
    http = FakeHttp({"duckduckgo.com": ddg_router({"maison vérone": [
        ("https://www.zalando.fr/maison-verone/", "Maison Vérone"),
        ("https://www.maisonverone-france.fr/", "Maison Vérone France"),
    ]})})
    web = WebSearch({"min_delay_seconds": 0}, http, db)
    checker = BrandChecker(cfg["brands"], cfg["filters"]["resale_brands"], web, db)
    info = checker.check("Maison Vérone", "zalando.fr")
    assert info.status == FAKE and info.official_domain == ""      # le site « -france » est un sosie, pas officiel
    assert checker.check("Pier One", "zalando.fr").status == BLOCKED
    assert checker.check("Nike", "zalando.fr").status == VERIFIED
    db.close()


# ---------------------------------------------------------------- 2) ✅ vérifié : site officiel + multi-marchands

def test_verified_true_deal_with_official_site_and_merchants(cfg, tmp_path):
    db = StateDB(tmp_path / "v.db")
    http = FakeHttp({
        "duckduckgo.com": ddg_router({"site:sony.fr": [
            ("https://www.sony.fr/headphones/products/wh-1000xm5", "WH-1000XM5 | Casque sans fil | Sony FR")]}),
        "sony.fr/headphones": SONY_OFFICIAL_PAGE,
        "prechcat.html": idealo_search_html(),
        "/price-chart/": 404,
    })
    web = WebSearch({"min_delay_seconds": 0}, http, db)
    idealo = IdealoSource({"enabled": True, "min_title_similarity": 0.55}, http)
    idealo.db, idealo.filters_cfg = db, cfg["filters"]
    verifier = Verifier(cfg, db, idealo, None, web=web,
                        brand_checker=BrandChecker(cfg["brands"], cfg["filters"]["resale_brands"], web, db),
                        comparator=MultiComparator({"geizhals": {"enabled": False}}, http, db, idealo_fr=idealo,
                                                   web=web, brands_cfg=cfg["brands"]),
                        official=OfficialPriceFinder({}, http, web, db))
    deal = Deal(source="dealabs", source_id="1", title="Casque Sony WH-1000XM5 noir à 99€", url="https://www.dealabs.com/x",
                merchant="Amazon", merchant_url="https://www.amazon.fr/dp/B09Y2MYL5C", link_host="www.amazon.fr",
                price=99.0, original_price=399.99, price_eur=99.0, original_price_eur=399.99, origin="dealabs")
    v = verifier.verify(deal)
    assert v.verdict == VERDICT_TRUE, v.notes
    assert (v.brand, v.brand_status) == ("Sony", VERIFIED)
    assert v.official_price == 379.0 and v.official_url.startswith("https://www.sony.fr/")
    assert v.others_count == 79 and v.comparators == ["idealo.fr"]
    assert v.lowest_elsewhere == 217.99
    bot = Bot(bot_config(cfg), None, db, Capture(), http=FakeHttp({}))
    assert bot.should_send(deal, v) == (True, "")
    db.close()


def test_notification_filter_rules(cfg, tmp_path):
    db = StateDB(tmp_path / "n.db")
    bot = Bot(bot_config(cfg), None, db, Capture(), http=FakeHttp({}))
    community = Deal(source="dealabs", source_id="1", title="x", url="u", origin="dealabs")
    direct = Deal(source="zalando", source_id="2", title="x", url="u", origin="zalando")
    assert bot.should_send(community, Verification(verdict=VERDICT_INFLATED))[0] is False
    assert bot.should_send(community, Verification(verdict=VERDICT_UNVERIFIED))[0] is True
    assert bot.should_send(direct, Verification(verdict=VERDICT_UNVERIFIED))[0] is False
    assert bot.should_send(direct, Verification(verdict=VERDICT_TRUE))[0] is True
    db.close()


# ---------------------------------------------------------------- 3) lien marchand direct Dealabs

def test_dealabs_link_resolution(cfg, tmp_path):
    db = StateDB(tmp_path / "l.db")
    visit = "https://www.dealabs.com/visit/homenew/3467746"
    final = ("https://www.kickz.com/fr-FR/adidas-Sportswear-VL-Court-3-0/p/ID6285"
             "?utm_source=dealabs&utm_medium=affiliate&awc=123_abc&size=42")
    http = FakeHttp({}, redirects={visit: final})
    bot = Bot(bot_config(cfg), None, db, Capture(), http=http)
    deal = Deal(source="dealabs", source_id="3467746", title="adidas VL Court 3.0", url="https://www.dealabs.com/bons-plans/x-3467746",
                merchant_url=visit, link_host="www.kickz.com")
    bot.resolve_merchant_link(deal)
    assert deal.merchant_url == "https://www.kickz.com/fr-FR/adidas-Sportswear-VL-Court-3-0/p/ID6285?size=42"
    assert deal.visit_url == visit and deal.link_host == "kickz.com"
    bot.resolve_merchant_link(Deal(source="dealabs", source_id="2", title="t", url="u", merchant_url=visit))
    assert sum(1 for c in http.calls if c.startswith("REDIRECT")) == 1          # mis en cache
    # échec de résolution : on garde le lien /visit/ en secours
    fail = Deal(source="dealabs", source_id="3", title="t", url="u", merchant_url="https://www.dealabs.com/visit/homenew/9")
    bot.resolve_merchant_link(fail)
    assert fail.merchant_url == "https://www.dealabs.com/visit/homenew/9"
    db.close()


class _Resp:
    def __init__(self, url, status=200, text=""):
        self.url, self.status_code, self.text, self.headers = url, status, text, {}


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def request(self, method, url, **kw):
        self.calls.append((url, kw.get("allow_redirects")))
        return self.resp


def test_http_resolve_redirect(monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    final = "https://www.fnac.com/Enceinte-JBL-Charge/a22579612/w-4?eaf-publisher=AWIN"
    c = HttpClient({"min_delay_seconds": 0, "max_delay_seconds": 0}, session=_Session(_Resp(final, 403)))
    assert c.resolve_redirect("https://www.dealabs.com/visit/homenew/1") == final     # même si la page finale = 403
    assert c.session.calls[0][1] is True
    js = '<html><script>window.location.href = "https://www.boulanger.com/ref/1225928";</script></html>'
    c2 = HttpClient({"min_delay_seconds": 0, "max_delay_seconds": 0},
                    session=_Session(_Resp("https://www.dealabs.com/visit/homenew/2", 200, js)))
    assert c2.resolve_redirect("https://www.dealabs.com/visit/homenew/2") == "https://www.boulanger.com/ref/1225928"


# ---------------------------------------------------------------- moteur : cooldown, UK, briques

def test_domain_cooldown_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    db = StateDB(tmp_path / "c.db")
    c = HttpClient({"min_delay_seconds": 0, "max_delay_seconds": 0, "max_retries": 0,
                    "domain_cooldown_after_failures": 2, "domain_cooldown_minutes": 60},
                   session=_Session(_Resp("https://www.fnac.com/x", 403, "blocked")))
    c.cooldown_store = db
    for _ in range(2):
        with pytest.raises(FetchError):
            c.get("https://www.fnac.com/x")
    assert db.domain_cooldown_left("fnac.com") > 59
    with pytest.raises(FetchError) as exc:
        c.get("https://www.fnac.com/y")                # refusé localement, sans requête
    assert exc.value.antibot == "cooldown" and len(c.session.calls) == 2
    db.close()


def test_uk_price_cap(cfg):
    from filters import price_ok
    g = cfg["general"]
    uk = Deal(source="hotukdeals", source_id="1", title="x", url="u", link_host="www.endclothing.com", price_eur=170.0)
    assert price_ok(uk, g)[0] is False
    uk.price_eur = 149.0
    assert price_ok(uk, g)[0] is True
    fr = Deal(source="dealabs", source_id="2", title="x", url="u", link_host="www.fnac.com", price_eur=170.0)
    assert price_ok(fr, g)[0] is True


def test_resale_threshold_40(cfg):
    from filters import evaluate_triggers
    d = Deal(source="x", source_id="1", title="Apple AirPods 4", url="u", price=60.0, original_price=100.0)
    assert evaluate_triggers(d, cfg["triggers"], None, cfg["filters"]["resale_brands"]) == [
        "Réduction affichée 40 % (≥ 40 %, marque revente « Apple »)"]


def test_building_blocks(cfg):
    results = parse_results(ddg_html([("https://www.newbalance.fr/", "New Balance - Site officiel"),
                                      ("https://www.newbalance-outlet.fr/", "New Balance Outlet -80%")]))
    assert [r["domain"] for r in results] == ["newbalance.fr", "newbalance-outlet.fr"]
    assert official_domain_from(results, "newbalance", cfg["brands"]) == "newbalance.fr"
    assert official_domain_from(list(reversed(results)), "newbalance", cfg["brands"]) == "newbalance.fr"
    assert registrable_label("shop.bbc.co.uk") == "bbc"
    price, name = extract_official_price(SONY_OFFICIAL_PAGE)
    assert (price, name) == (379.0, "Sony WH-1000XM5")
    meta = '<html><head><meta property="og:title" content="Bose QC Ultra"><meta property="product:price:amount" content="449.95"></head></html>'
    assert extract_official_price(meta) == (449.95, "Bose QC Ultra")
    assert idealo_other_merchants({"offer_count": 1, "lowest_shop": "Zalando", "lowest_price": 50}, "Zalando") == 0
    assert idealo_other_merchants({"offer_count": 5, "lowest_shop": "Fnac", "lowest_price": 50}, "Amazon") == 4
    d = Deal(source="z", source_id="1", title="GARMIN Forerunner 265", url="u")
    assert extract_brand(d, cfg["brands"], cfg["filters"]["resale_brands"]) == "Garmin"
    d2 = Deal(source="h", source_id="2", title="Price drop on Amazon for SSD", url="u")
    assert extract_brand(d2, cfg["brands"], cfg["filters"]["resale_brands"]) != "On"


def test_web_search_budget_and_antibot(tmp_path):
    db = StateDB(tmp_path / "w.db")
    http = FakeHttp({"duckduckgo.com": ddg_html([("https://a.com/", "A")])})
    web = WebSearch({"min_delay_seconds": 0, "max_queries_per_run": 1}, http, db)
    assert web.search("q1") and web.search("q1")          # 2e appel servi par le cache
    assert web.search("q2") is None and web.budget_exhausted
    blocked = FakeHttp({"duckduckgo.com": "<html><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too</div></html>"})
    w2 = WebSearch({"min_delay_seconds": 0}, blocked, None)
    assert w2.search("x") is None and w2.blocked
    db.close()
