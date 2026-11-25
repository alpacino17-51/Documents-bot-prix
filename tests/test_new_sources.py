"""Courir, Galeries Lafayette et marques indépendantes Shopify (trusted_dtc_brands)."""
import copy
import time

from conftest import FakeHttp, read_fixture

from db import StateDB
from filters import evaluate_triggers
from main import Bot
from models import VERDICT_DTC_INTRO, VERDICT_TRUE, Deal
from notifier import format_alert
from sources.courir import CourirSource, page_url, parse_listing
from sources.dtc_shopify import DTCShopifySource, analyse_history, size_matches, variant_size
from sources.galerieslafayette import GaleriesLafayetteSource
from verifier import Verifier

DAY = 86400.0
DAVRIL = "https://davril.com"


def shopify_product(pid, title, price, compare_at=None, sizes=(("S", False), ("M", True), ("L", True)),
                    vendor="DAVRILSUPPLY"):
    return {"id": pid, "title": title, "handle": title.lower().replace(" ", "-"), "vendor": vendor,
            "product_type": "", "options": [{"name": "Size"}],
            "variants": [{"title": s, "option1": s, "option2": None, "price": f"{price:.2f}",
                          "compare_at_price": f"{compare_at:.2f}" if compare_at else None, "available": avail}
                         for s, avail in sizes]}


def dtc_cfg(cfg, **over):
    c = copy.deepcopy(cfg["trusted_dtc_brands"])
    c["brands"] = [{"id": "davril", "name": "Davril Supply", "base_url": DAVRIL, "collections": []}]
    c.update(over)
    return c


# ---------------------------------------------------------------- analyse de l'historique local

def test_history_rules():
    cfg = {"min_drop_pct": 35, "intro_min_compare_at_pct": 50, "min_history_points": 2, "min_history_days": 3}
    now = time.time()
    # prix antérieur plus élevé observé -> historique exploitable -> ✅ si ≥ 35 % sous la médiane et au plus bas
    v = analyse_history([(now - 5 * DAY, 119.95)], 47.98, None, cfg, now)
    assert v["kind"] == "verified" and v["reference"] == 119.95 and v["drop_pct"] == 60.0
    # baisse trop faible
    assert analyse_history([(now - 5 * DAY, 119.95)], 99.95, None, cfg, now) is None
    # prix identique depuis le premier relevé : jamais d'alerte, même avec un prix barré « -50 % »
    assert analyse_history([(now - 10 * DAY, 64.98), (now - 2 * DAY, 64.98)], 64.98, 129.95, cfg, now) is None
    # démarrage : pas d'historique, prix barré -50 % -> ℹ️ promo directe
    intro = analyse_history([], 64.98, 129.95, cfg, now)
    assert intro["kind"] == "intro" and intro["drop_pct"] == 50.0
    assert analyse_history([], 80.0, 129.95, cfg, now) is None            # -38 % < 50 %
    # pas au plus bas jamais relevé -> pas de ✅
    assert analyse_history([(now - 9 * DAY, 120), (now - 6 * DAY, 40), (now - 3 * DAY, 120)], 60.0, None, cfg, now) is None


def test_sizes():
    assert variant_size({"title": "M", "option1": "M"}) == "M"
    assert variant_size({"title": "Default Title", "option1": "Default Title"}) == ""
    assert size_matches("M", ["M", "L"]) and not size_matches("S", ["M", "L"])
    assert size_matches("", ["M"]) and size_matches("XL", [])


# ---------------------------------------------------------------- module dtc_shopify + pipeline

def test_davril_drop_from_119_95_to_47_98_is_verified_and_sent(cfg, tmp_path):
    db = StateDB(tmp_path / "d.db")
    key = f"dtc:davril.com:9497521291593"
    db.add_price_observation(key, 119.95, "dtc-davril", ts=time.time() - 6 * DAY)
    db.add_price_observation(key, 119.95, "dtc-davril", ts=time.time() - 2 * DAY)
    catalog = {"products": [
        shopify_product(9497521291593, "R0CKSTAR DENIM", 47.98, 169.95),
        shopify_product(9497507332425, "DRAGOON T-SHIRT", 44.95,
                        sizes=(("S", False), ("M", False), ("L", False))),                 # épuisé
        shopify_product(9497507332426, "BASIC TEE", 39.95),                                 # jamais soldé
    ]}
    c = copy.deepcopy(cfg)
    for name, sc in c["sources"].items():
        if isinstance(sc, dict):
            sc["enabled"] = False
    c["sites_catalog"]["sites"] = []
    c["trusted_dtc_brands"] = dtc_cfg(cfg)
    c["web_search"]["enabled"] = False
    http = FakeHttp({"davril.com/products.json": catalog})

    class Capture:
        alerts = []

        def send(self, alert, test=False):
            self.alerts.append(alert)
            return True

    notifier = Capture()
    bot = Bot(c, None, db, notifier, http=http)
    stats = bot.run_once()
    assert stats["sent"] == 1
    alert = notifier.alerts[0]
    assert alert.deal.title == "R0CKSTAR DENIM" and alert.deal.price == 47.98
    assert alert.verification.verdict == VERDICT_TRUE
    assert alert.verification.label_override == "✅ Baisse vérifiée (historique local)"
    assert alert.deal.sizes == ["M", "L"]                                    # S épuisée
    text = format_alert(alert)
    assert "Baisse vérifiée (historique local)" in text and "Tailles en stock :</b> M, L" in text
    assert "marque indépendante" in text                                    # pas de comparateur
    # historique complet : les 3 produits (épuisé et non soldé compris) sont enregistrés
    assert db.price_history("dtc:davril.com:9497507332425") and db.price_history("dtc:davril.com:9497507332426")
    # aucune requête vers la recherche web / Idealo pour une marque de confiance
    assert not any("duckduckgo" in u or "idealo" in u for u in http.calls)
    # passage suivant au même prix : déjà signalé
    assert bot.run_once()["sent"] == 0
    db.close()


def test_sold_out_product_is_ignored(cfg, tmp_path):
    db = StateDB(tmp_path / "s.db")
    db.add_price_observation("dtc:davril.com:1", 119.95, "dtc-davril", ts=time.time() - 6 * DAY)
    http = FakeHttp({"davril.com/products.json": {"products": [
        shopify_product(1, "SOLD OUT JACKET", 29.95, 119.95, sizes=(("S", False), ("M", False)))]}})
    src = DTCShopifySource(dtc_cfg(cfg), http)
    src.db = db
    assert src.safe_fetch() == []
    assert db.price_history("dtc:davril.com:1")[-1][1] == 29.95            # prix tout de même enregistré
    db.close()


def test_intro_promo_then_permanent_fake_promo(cfg, tmp_path):
    db = StateDB(tmp_path / "i.db")
    product = shopify_product(10014303191369, "GREY OLDSALT ZIPS", 64.98, 129.95)
    http = FakeHttp({"davril.com/products.json": {"products": [product]}})
    src = DTCShopifySource(dtc_cfg(cfg), http)
    src.db = db
    first = src.safe_fetch()
    assert len(first) == 1 and first[0].dtc["kind"] == "intro"
    v = Verifier(cfg, db).verify(first[0])
    assert v.verdict == VERDICT_DTC_INTRO and "en construction" in " ".join(v.notes)
    # 10 jours plus tard, toujours 64,98 € : prix jamais changé -> plus aucune alerte
    db.conn.execute("UPDATE price_observations SET ts = ts - ?", (10 * DAY,))
    db.conn.commit()
    assert src.safe_fetch() == []
    db.close()


def test_wanted_sizes_filter(cfg, tmp_path):
    db = StateDB(tmp_path / "w.db")
    product = shopify_product(5, "ZIP", 50.0, 120.0, sizes=(("S", True), ("M", False), ("L", False)))
    http = FakeHttp({"davril.com/products.json": {"products": [product]}})
    only_ml = DTCShopifySource(dtc_cfg(cfg, wanted_sizes=["M", "L"]), http)
    only_ml.db = db
    assert only_ml.safe_fetch() == []                                       # seule la taille S est en stock
    db.close()


def test_html_fallback_when_products_json_disabled(cfg, tmp_path):
    db = StateDB(tmp_path / "h.db")
    html = """<ul><li class="grid__item"><a href="/products/icicle-scarf">Icicle Scarf écharpe</a>
              <span class="price-item--sale">32,48 €</span><s class="price-item--regular">64,95 €</s></li></ul>"""
    c = dtc_cfg(cfg)
    c["brands"][0]["collections"] = ["/collections/all"]
    http = FakeHttp({"products.json": 404, "davril.com/collections/all": html})
    src = DTCShopifySource(c, http)
    src.db = db
    deals = src.safe_fetch()
    assert len(deals) == 1 and deals[0].dtc["kind"] == "intro" and deals[0].price == 32.48
    db.close()


# ---------------------------------------------------------------- Courir

def test_courir_outlet_without_strike_price(cfg):
    deals = {d.source_id: d for d in parse_listing(read_fixture("courir_outlet.html"))}
    asics = deals["1599001"]
    assert (asics.title, asics.brand, asics.price, asics.original_price) == ("Asics Gel-1130", "Asics", 59.99, None)
    assert asics.allow_no_strike and asics.force_market_compare and asics.mpn == "1201A256-020"
    assert asics.url == "https://www.courir.com/fr/p/asics-gel-1130-1599001.html"
    nb = deals["1588877"]
    assert (nb.price, nb.original_price) == (79.99, 119.99) and not nb.allow_no_strike
    resale = cfg["filters"]["resale_brands"]
    # marque revente sans prix barré : envoyée au vérificateur (comparaison au marché)
    assert evaluate_triggers(asics, cfg["triggers"], None, resale) == [
        "Outlet sans prix barré — marque revente « Asics » (comparaison au prix du marché)"]
    # Lacoste n'est pas une marque revente : pas de déclenchement sans réduction
    assert evaluate_triggers(deals["1607125"], cfg["triggers"], None, resale) == []


def test_courir_source_pagination_and_price_cap(cfg):
    assert page_url("https://www.courir.com/fr/c/outlet/", 40, 40) == "https://www.courir.com/fr/c/outlet/?start=40&sz=40"
    http = FakeHttp({"outlet/?start=0": read_fixture("courir_outlet.html"), "outlet/?start=40": "<html></html>"})
    c = copy.deepcopy(cfg["sources"]["courir"])
    c["page_urls"] = ["https://www.courir.com/fr/c/outlet/"]
    deals = CourirSource(c, http, cfg["general"]).safe_fetch()
    assert {d.source_id for d in deals} == {"1607125", "1599001", "1588877"}    # Air Max Plus 219,99 € > 200 €
    assert len(http.calls) == 2                                                 # page 2 vide : arrêt


def test_courir_no_strike_deal_is_compared_to_market(cfg, tmp_path):
    seen = {}

    class StubComparator:
        def reset_run(self):
            return None

        def compare(self, deal, *, fashion, brand="", source_host=""):
            seen["fashion"] = fashion
            return {"others": 4, "lowest": 89.0, "lowest_shop": "Zalando", "checked": ["idealo.fr"],
                    "complete": True, "idealo": None}

    db = StateDB(tmp_path / "c.db")
    deal = parse_listing(read_fixture("courir_outlet.html"))
    asics = next(d for d in deal if d.brand == "Asics")
    asics.price_eur = asics.price
    v = Verifier(cfg, db, comparator=StubComparator()).verify(asics)
    assert seen["fashion"] is False                                  # mode, mais comparaison marché forcée
    assert v.brand_status == "verified" and v.lowest_elsewhere == 89.0
    db.close()


# ---------------------------------------------------------------- Galeries Lafayette

def test_galeries_lafayette_from_dealabs(cfg):
    listing = read_fixture("dealabs_listing.html").replace('"merchantName":"Cdiscount"',
                                                           '"merchantName":"Galeries Lafayette"')
    c = copy.deepcopy(cfg["sources"]["galerieslafayette"])
    c["page_urls"] = []
    c["idealo_shop_patterns"] = []
    http = FakeHttp({"dealabs.com/search": listing})
    deals = GaleriesLafayetteSource(c, http).safe_fetch()
    assert [d.source_id for d in deals] == ["3470001"]                # seuls les deals du marchand GL
    assert deals[0].origin == "galerieslafayette" and deals[0].merchant == "Galeries Lafayette"
