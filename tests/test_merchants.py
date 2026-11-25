import json
import time

import pytest
from conftest import FakeHttp, read_fixture

import http_client
from db import StateDB
from filters import category_ok, evaluate_triggers, is_fashion
from http_client import FetchError, HttpClient, detect_antibot
from models import VERDICT_INFLATED, Deal
from sources.boulanger import BoulangerSource
from sources.boulanger import parse_listing as boulanger_parse
from sources.dealabs import DealabsSource, page_url
from sources.decathlon import DecathlonSource
from sources.decathlon import parse_listing as decathlon_parse
from sources.fnac import FnacSource
from sources.fnac import parse_listing as fnac_parse
from sources.idealo_drops import IdealoDropsSource, item_to_deal
from sources.zalando import ZalandoSource
from sources.zalando import parse_listing as zalando_parse
from verifier import Verifier, compute_verdict


# ------------------------------------------------------------------ Decathlon

def test_decathlon_cards_and_jsonld():
    deals = decathlon_parse(read_fixture("decathlon_listing.html"))
    by_id = {d.source_id: d for d in deals}
    assert set(by_id) == {"355950", "308000", "341111", "301234"}
    vtt = by_id["308000"]
    assert vtt.title == "VTT ROCKRIDER ST 120 27,5 pouces"
    assert vtt.url == "https://www.decathlon.fr/p/velo-tout-terrain-rockrider-st-120/308000/c100m8529611"
    assert (vtt.price, vtt.original_price, vtt.displayed_discount_pct) == (89.99, 349.99, 74.3)
    assert vtt.category == "Sport" and vtt.mpn == "8529611" and vtt.merchant == "Decathlon"
    garmin = by_id["355950"]
    assert (garmin.price, garmin.original_price) == (174.99, 199.99)
    assert "Promotion du 14/11/2026" in garmin.description
    tent = by_id["301234"]                      # lu depuis le JSON-LD (prix barré StrikethroughPrice + EAN)
    assert (tent.price, tent.original_price, tent.ean) == (19.99, 49.99, "3608439571237")
    assert by_id["341111"].original_price is None
    assert deals[0].source_id == "308000"       # trié par réduction décroissante


# ------------------------------------------------------------------ Boulanger

def test_boulanger_tc_vars_and_itemlist():
    deals = boulanger_parse(read_fixture("boulanger_listing.html"))
    by_id = {d.source_id: d for d in deals}
    assert set(by_id) == {"1231149", "1225928", "9000724577"}          # reconditionné exclu par défaut
    manette = by_id["1225928"]
    assert manette.title == "Manette SONY DualSense V2 Blanche"
    assert manette.url == "https://www.boulanger.com/ref/1225928"
    assert manette.price == 19.99 and manette.displayed_discount_pct == 73.0
    assert manette.original_price == pytest.approx(74.04, abs=0.01)
    assert manette.ean == "0711719575856" and manette.category == "console - gaming / accessoire console"
    assert by_id["9000724577"].merchant == "Boulanger (vendeur : micromania)"
    assert by_id["1231149"].original_price is None
    refurb = boulanger_parse(read_fixture("boulanger_listing.html"), include_refurbished=True)
    assert "1240690" in {d.source_id for d in refurb}


# ------------------------------------------------------------------ Fnac

def test_fnac_listing():
    deals = fnac_parse(read_fixture("fnac_listing.html"))
    by_id = {d.source_id: d for d in deals}
    airpods = by_id["19734211"]
    assert airpods.title.startswith("Apple AirPods Pro 2")
    assert (airpods.price, airpods.original_price) == (79.99, 279.0)       # format « 79€<sup>99</sup> »
    assert airpods.url.startswith("https://www.fnac.com/a19734211/") and "#" not in airpods.url
    pad = by_id["17123456"]
    assert pad.url == "https://www.fnac.com/a17123456/Manette-Xbox-sans-fil-Carbon-Black"
    assert (pad.price, pad.original_price) == (39.99, 64.99)
    assert pad.merchant == "Fnac (Vendu par GamesPlanet)"


def test_fnac_datadome_is_reported_and_stops(monkeypatch):
    assert detect_antibot(read_fixture("fnac_datadome.html")) == "DataDome"
    http = FakeHttp({"fnac.com": 403})
    src = FnacSource({"enabled": True, "page_urls": ["https://www.fnac.com/a", "https://www.fnac.com/b"]}, http)
    assert src.safe_fetch() == []
    assert src.blocked == "403"
    assert len(http.calls) == 1                 # n'insiste pas sur la 2e page


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text, self.headers, self.url = status, text, {}, "https://www.fnac.com/x"


class _Session:
    def __init__(self, resp):
        self.resp = resp

    def request(self, *a, **k):
        return self.resp


def test_http_client_flags_antibot_page(monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    for status in (403, 200):
        c = HttpClient({"min_delay_seconds": 0, "max_delay_seconds": 0},
                       session=_Session(_Resp(status, read_fixture("fnac_datadome.html"))))
        with pytest.raises(FetchError) as exc:
            c.get("https://www.fnac.com/x")
        assert exc.value.antibot == "DataDome" and exc.value.blocked
    assert detect_antibot("<rss><item>datadome</item></rss>") == ""     # un flux RSS n'est jamais un captcha


# ------------------------------------------------------------------ Zalando

def test_zalando_listing_and_reference_prices(cfg):
    deals = zalando_parse(read_fixture("zalando_listing.html"))
    by_id = {d.source_id: d for d in deals}
    assert set(by_id) == {"21M22Q04X-K11", "ONM42A04B-A11", "GU122O11V-Q11"}
    gilet = by_id["21M22Q04X-K11"]
    assert gilet.title == "Mo Gilet - navy offwhite"
    assert gilet.price == 19.99 and gilet.original_price == 129.95 and gilet.displayed_discount_pct == 84.6
    assert gilet.merchant_ref == {"usual": 129.95, "low_30": 29.99}
    assert gilet.category == "Mode" and is_fashion(gilet, cfg["filters"])
    on = by_id["ONM42A04B-A11"]
    assert (on.price, on.original_price, on.merchant_ref) == (115.0, 170.0, {"low_30": 170.0})
    assert by_id["GU122O11V-Q11"].merchant_ref == {"median": 29.95}
    assert deals[0] is gilet


def test_zalando_inflated_by_omnibus_low30(cfg, tmp_path):
    gilet = zalando_parse(read_fixture("zalando_listing.html"))[0]
    gilet.price_eur, gilet.original_price_eur = gilet.price, gilet.original_price
    db = StateDB(tmp_path / "z.db")
    v = Verifier(cfg, db, idealo=None, camel=None).verify(gilet)
    assert v.verdict == VERDICT_INFLATED                       # 129,95 € barré alors que le plus bas 30 j = 29,99 €
    assert any("plus bas des 30 derniers jours (29,99 €)" in n for n in v.notes)
    assert "mode" in v.skipped_idealo_reason
    db.close()


def test_verdict_low30_only():
    verdict, real, notes = compute_verdict(19.99, 129.95, 84.6, {"low_30": 29.99}, None, "", {})
    assert verdict == VERDICT_INFLATED and real is None


# ------------------------------------------------------------------ Idealo bons plans

def test_idealo_drops_items():
    items = json.loads(read_fixture("idealo_deals.json"))["items"]
    deals = [d for d in (item_to_deal(i) for i in items) if d]
    assert [d.source_id for d in deals] == ["214133333", "200811096"]     # occasion et pub exclues
    poke = deals[0]
    assert poke.merchant == "Amazon.fr" and poke.link_host == "amazon.fr"
    assert poke.price == 55.99 and poke.displayed_discount_pct == 67.0
    assert poke.original_price == pytest.approx(169.67, abs=0.01)
    assert poke.merchant_url.startswith("https://www.idealo.fr/relocator/relocate?offerKey=")
    assert poke.category_hint == "Cartes Pokémon" and poke.merchant_ref["market_median"] == poke.original_price
    gpu = deals[1]
    assert gpu.url == "https://www.idealo.fr/prix/200811096/gigabyte-geforce-rtx-3070-aorus-master.html"
    assert gpu.merchant_url == gpu.url


def test_idealo_drops_source_and_html_fallback():
    import base64
    payload = base64.b64encode(read_fixture("idealo_deals.json").encode()).decode()
    html = f"<script>window.csr.initialData['searchResult'] = \"{payload}\";</script>"
    cfg = {"enabled": True, "api_urls": ["https://www.idealo.fr/csr/api/v2/modules/dealsResult?x=1"],
           "page_url": "https://www.idealo.fr/bons-plans"}
    ok = IdealoDropsSource(cfg, FakeHttp({"dealsResult": json.loads(read_fixture("idealo_deals.json"))})).safe_fetch()
    assert len(ok) == 2 and ok[0].origin == "idealo_drops"
    fallback = IdealoDropsSource(cfg, FakeHttp({"dealsResult": 500, "/bons-plans": html})).safe_fetch()
    assert len(fallback) == 2


def test_idealo_hashed_href_and_same_merchant(cfg, tmp_path):
    from sources.idealo import idealo_url
    assert idealo_url("8630036ad86e46da6fc6") == ""             # lien haché vu en conditions réelles
    assert idealo_url("/prix/1/x.html") == "https://www.idealo.fr/prix/1/x.html"

    class StubIdealo:
        enabled = True

        def lookup(self, deal):
            return {"product_id": "1", "lowest_price": 146.9, "lowest_shop": "Cdiscount.com (Marketplace)",
                    "lowest_is_same_merchant": True, "url": "", "title": "VIST PC", "history": None}

    db = StateDB(tmp_path / "m.db")
    d = Deal(source="idealo-drops", source_id="1", title="VIST PC Ryzen 7 5700X RX 9060 XT", url="u",
             merchant="Cdiscount.com (Marketplace)", price=146.9, price_eur=146.9, original_price=1469.0,
             original_price_eur=1469.0, merchant_ref={"median": 1469.0})
    v = Verifier(cfg, db, StubIdealo(), None).verify(d)
    assert v.lowest_elsewhere is None and any("Seule offre" in n for n in v.notes)
    assert not any("Moins cher ailleurs" in n for n in v.notes)
    db.close()


def test_per_source_threshold_override(cfg, tmp_path):
    import copy
    from main import Bot
    c = copy.deepcopy(cfg)
    c["sources"]["zalando"]["min_displayed_discount_pct"] = 90
    db = StateDB(tmp_path / "o.db")
    bot = Bot(c, None, db, notifier=None, http=FakeHttp({}))
    gilet = zalando_parse(read_fixture("zalando_listing.html"))[0]          # -85 %
    gilet.origin = "zalando"
    assert evaluate_triggers(gilet, bot._triggers_for(gilet, c["triggers"]), None) == []
    gilet.origin = "decathlon"
    assert evaluate_triggers(gilet, bot._triggers_for(gilet, c["triggers"]), None)
    db.close()


def test_category_hint_and_resale_threshold(cfg):
    gpu = Deal(source="idealo-drops", source_id="1", title="GigaByte GeForce RTX 3070 AORUS Master", url="u",
               category_hint="Carte graphique")
    assert category_ok(gpu, cfg["filters"])[0]
    poke = Deal(source="idealo-drops", source_id="2", title="Pokémon Coffret Dresseur d'Élite", url="u",
                price=55.99, original_price=169.67)
    found = evaluate_triggers(poke, cfg["triggers"], None, cfg["filters"]["resale_brands"])
    assert found == ["Réduction affichée 67 % (≥ 40 %, marque revente « Pokémon »)"]
    generic = Deal(source="x", source_id="3", title="Tapis de yoga", url="u", price=40.0, original_price=100.0)
    assert evaluate_triggers(generic, cfg["triggers"], None, cfg["filters"]["resale_brands"]) == []


# ------------------------------------------------------------------ Dealabs : pagination + pages marchands

def _fresh(html: str, shift: int = 600) -> str:
    return html.replace("1795600000", str(int(time.time()) - shift))


def test_dealabs_pagination_and_merchant_pages():
    listing = read_fixture("dealabs_listing.html")
    page2 = listing.replace("3470001", "3479901").replace("3470002", "3479902").replace("3470003", "3479903")
    merchant = read_fixture("mydealz_listing.html")      # marchands : Amazon x2, MediaMarkt
    http = FakeHttp({"nouveaux?page=2": page2, "dealabs.com/nouveaux": listing, "search?q=amazon": merchant})
    cfg = {"enabled": True, "pages_per_feed": 2, "topics_enabled": False, "fetch_order": ["json"],
           "feeds": [{"name": "nouveaux", "page_url": "https://www.dealabs.com/nouveaux", "paginate": True}],
           "merchant_pages": [{"name": "amazon", "url": "https://www.dealabs.com/search?q=amazon&sortBy=new",
                               "merchant_filter": "amazon"}]}
    deals = DealabsSource(cfg, http).fetch()
    ids = [d.source_id for d in deals]
    assert ids[:6] == ["3470001", "3470002", "3470003", "3479901", "3479902", "3479903"]
    assert "https://www.dealabs.com/nouveaux?page=2" in http.calls
    assert ids[6:] == ["2890001", "2890003"]                 # MediaMarkt filtré par le nom du marchand
    assert page_url("https://x/search?q=a", 2) == "https://x/search?q=a&page=2"


# ------------------------------------------------------------------ pause après blocage + 1er passage par source

def test_blocked_source_is_paused_and_new_source_bootstraps(cfg, tmp_path):
    import copy
    from main import Bot

    class Capture:
        def __init__(self):
            self.alerts = []

        def send(self, alert, test=False):
            self.alerts.append(alert)
            return True

    c = copy.deepcopy(cfg)
    c["sites_catalog"]["sites"] = []
    c["trusted_dtc_brands"]["enabled"] = False
    c["sources"]["courir"]["enabled"] = c["sources"]["galerieslafayette"]["enabled"] = False
    for name in ("dealabs", "pepper_eu", "camelcamelcamel", "reddit", "decathlon", "boulanger", "idealo_drops"):
        c["sources"][name]["enabled"] = False
    c["sources"]["idealo"]["enabled"] = False
    c["sources"]["zalando"]["page_urls"] = ["https://www.zalando.fr/promo-homme/?order=sale"]
    c["sources"]["fnac"]["page_urls"] = ["https://www.fnac.com/ventes-flash"]
    c["sources"]["zalando"]["min_interval_minutes"] = c["sources"]["fnac"]["min_interval_minutes"] = 0
    http = FakeHttp({"zalando.fr": read_fixture("zalando_listing.html"), "fnac.com": 403})
    db = StateDB(tmp_path / "s.db")
    db.mark_initialized()                       # installation existante : seule Zalando est nouvelle
    db.set_meta("per_source_init", "1")
    notifier = Capture()
    bot = Bot(c, None, db, notifier, http=http)
    s1 = bot.run_once()
    assert s1["sent"] == 0 and s1["bootstrap"] == 1         # Zalando (gilet -85 %) mémorisé sans alerte
    assert db.paused_for("fnac") > 50 and "BLOQUÉ" in bot.report["fnac"]
    assert db.is_source_initialized("zalando")
    calls = len(http.calls)
    s2 = bot.run_once()
    assert "pause" in bot.report["fnac"]
    assert not any("fnac" in u for u in http.calls[calls:])   # aucune requête Fnac pendant la pause
    assert s2["sent"] == 0 and s2["duplicates"] == 1
    db.close()
