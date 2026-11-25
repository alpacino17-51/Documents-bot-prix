"""LOT 2 : moteurs pilotés par `sites_catalog` (comparateurs EU, Tradeinn, Galaxus, boutiques sneakers)."""
import copy
import json

import pytest
from conftest import FakeHttp, read_fixture

from db import StateDB
from filters import is_fashion, price_ok
from main import Bot
from sources.catalog import build_catalog_sources
from sources.comparators_drops import (IdealoDealsSource, clear_cache, idealo_deals_url, parse_geizhals_deals,
                                       shop_matches)
from sources.galaxus import GalaxusSource, parse_next_data
from sources.sneakers_generic import SneakersGenericSource, parse_cards, parse_hypernova, parse_shopify
from sources.tradeinn import TradeinnSource
from utils import to_eur


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def catalog_site(cfg, sid):
    return copy.deepcopy(next(s for s in cfg["sites_catalog"]["sites"] if s["id"] == sid))


# ---------------------------------------------------------------- comparateurs européens

def test_idealo_deals_url():
    url = idealo_deals_url("es", 50, 200, "DISCOUNT_DESC")
    assert url.startswith("https://www.idealo.es/csr/api/v2/modules/dealsResult?locale=es_ES")
    assert "discountFrom=50" in url and "priceTo=200" in url and url.endswith("&sort=DISCOUNT_DESC")


def test_idealo_es_keeps_only_shops_shipping_to_france(cfg):
    site = catalog_site(cfg, "idealo_es_drops")
    http = FakeHttp({"idealo.es/csr/api": json.loads(read_fixture("idealo_deals_es.json"))})
    deals = IdealoDealsSource(site, http).safe_fetch()
    shops = sorted(d.merchant for d in deals)
    assert shops == ["amazon.es", "bikeinn.com (ES)", "dressinn.com (ES)"]          # tulli.es écarté
    sony = next(d for d in deals if "Sony" in d.title)
    assert sony.source == "idealo_es_drops" and sony.origin == "idealo_es_drops"
    assert sony.site_country == "ES" and sony.price == 59.0 and sony.displayed_discount_pct == 58.0
    assert sony.merchant_url.startswith("https://www.idealo.es/relocator/")
    bike = next(d for d in deals if "Shimano" in d.title)
    assert bike.url == "https://www.idealo.es/precios/301000002/shimano-rc3.html"
    # 2 tris (nouveauté + réduction) mais une seule requête par URL distincte
    assert len(http.calls) == 2


def test_shop_matching():
    assert shop_matches("Amazon Marketplace (ES)", ["amazon"])
    assert shop_matches("bikeinn.com (ES)", ["inn.com"])
    assert not shop_matches("tulli.es", ["amazon", "inn.com"])


def test_geizhals_deals_parser():
    html = """<div class="listview"><article class="listview__item" data-id="1">
      <a class="listview__name-link" href="sony-wh-1000xm5-a2783471.html">Sony WH-1000XM5 schwarz</a>
      <span class="price">€ 199,00</span> <span>statt € 299,00</span> <span>-33%</span></article></div>"""
    deals = parse_geizhals_deals(html)
    assert len(deals) == 1 and deals[0].source_id == "2783471"
    assert (deals[0].price, deals[0].original_price) == (199.0, 299.0)
    assert deals[0].url == "https://geizhals.eu/sony-wh-1000xm5-a2783471.html"


# ---------------------------------------------------------------- Tradeinn & Galaxus

def test_tradeinn_network_from_idealo(cfg):
    site = catalog_site(cfg, "tradeinn")
    site["idealo_countries"] = ["es"]
    http = FakeHttp({"idealo.es/csr/api": json.loads(read_fixture("idealo_deals_es.json"))})
    deals = TradeinnSource(site, http).safe_fetch()
    by_shop = {d.merchant: d for d in deals}
    assert set(by_shop) == {"bikeinn.com (ES)", "dressinn.com (ES)"}
    assert by_shop["bikeinn.com (ES)"].category == "Sport"
    assert by_shop["dressinn.com (ES)"].category == "Mode"
    assert all(d.site_country == "ES" and "vu sur idealo.es" in d.description for d in deals)


def test_galaxus_next_data_and_idealo(cfg):
    deals = parse_next_data(read_fixture("galaxus_home.html"))
    robo = next(d for d in deals if "Roborock" in d.title)
    assert robo.title == "Roborock Saros 20 Flow Complete (36000 Pa)" and robo.brand == "Roborock"
    assert (robo.price, robo.original_price) == (599.0, 1299.0)
    assert robo.url == "https://www.galaxus.fr/fr/s2/product/roborock-saros-20-flow-complete-76143439"
    site = catalog_site(cfg, "galaxus")
    http = FakeHttp({"galaxus.fr/fr": read_fixture("galaxus_home.html"), "csr/api": {"items": []}})
    out = GalaxusSource(site, http).safe_fetch()
    assert [d.source_id for d in out] == ["76143439"]          # l'iPhone sans ancien prix n'est pas une promo


# ---------------------------------------------------------------- boutiques sneakers

def test_shopify_engine(cfg):
    deals = parse_shopify(json.loads(read_fixture("shopify_sale.json")), "https://www.asphaltgold.com", "asphaltgold")
    assert [d.source_id for d in deals] == ["7712345678901", "7712345678903"]     # hoodie sans remise ignoré
    am = deals[0]
    # variante disponible la plus remisée (la 43 à 59,95 € est épuisée)
    assert (am.price, am.original_price) == (89.95, 159.95)
    assert am.title == "Nike Air Max 1 '86 OG \"Big Bubble\"" and am.brand == "Nike" and am.mpn == "DQ3989-100"
    assert am.url == "https://www.asphaltgold.com/products/nike-air-max-1-86-og-big-bubble-dq3989-100"
    assert is_fashion(am, cfg["filters"])

    site = catalog_site(cfg, "asphaltgold")
    http = FakeHttp({"asphaltgold.com/collections/sale/products.json": json.loads(read_fixture("shopify_sale.json"))})
    out = SneakersGenericSource(site, http).safe_fetch()
    assert out[0].title.startswith("New Balance")           # trié par remise décroissante (70 %)
    assert all(d.merchant == "Asphaltgold" and d.link_host == "asphaltgold.com" for d in out)
    assert len(http.calls) == 1                              # moins de 250 produits : pas de page 2


def test_size_uk_gbp_and_uk_price_cap(cfg):
    site = catalog_site(cfg, "size_uk")
    http = FakeHttp({"size.co.uk/collections/sale/products.json": json.loads(read_fixture("shopify_sale.json"))})
    deals = SneakersGenericSource(site, http).safe_fetch()
    nb = deals[0]
    assert nb.currency == "GBP" and nb.site_country == "UK" and nb.link_host == "size.co.uk"
    nb.price_eur = to_eur(140.0, "GBP", cfg["general"]["fx_to_eur"])       # 163,80 € > 150 € (seuil UK)
    assert price_ok(nb, cfg["general"])[0] is False
    nb.price_eur = to_eur(nb.price, "GBP", cfg["general"]["fx_to_eur"])
    assert price_ok(nb, cfg["general"])[0] is True


def test_footshop_hypernova():
    deals = parse_hypernova(read_fixture("footshop_listing.html"), "https://www.footshop.fr", "footshop")
    assert [d.source_id for d in deals] == ["499120"]        # pas de remise / épuisé : ignorés
    nb = deals[0]
    assert nb.title == "New Balance 9060 Black" and nb.brand == "New Balance"
    assert (nb.price, nb.original_price, nb.mpn, nb.ean) == (71.99, 179.99, "U9060BLK", "196941361819")
    assert nb.url == "https://www.footshop.fr/fr/baskets-homme/499120-new-balance-9060-black.html"


def test_kickz_cards():
    deals = parse_cards(read_fixture("kickz_listing.html"), "https://www.kickz.com", "kickz")
    by_url = {d.url: d for d in deals}
    conv = by_url["https://www.kickz.com/fr-FR/Converse-Ct-Throwback-OX-Chaussure-de-sport-Noir/p/A19080C-001"]
    assert conv.title == "Converse Ct Throwback OX Chaussure de sport Noir"
    assert (conv.price, conv.original_price) == (63.96, 79.95)
    aj = by_url["https://www.kickz.com/fr-FR/Jordan-Air-Jordan-1-Low-Chaussure-Blanc/p/553558-141"]
    assert (aj.price, aj.original_price) == (39.98, 119.95)
    assert len(deals) == 2                                   # le lien « Livraison » n'est pas un produit


# ---------------------------------------------------------------- catalogue

def test_catalog_builds_sources_from_config(cfg):
    sources = build_catalog_sources(cfg, FakeHttp({}), cfg["general"], reserved={"dealabs"})
    ids = [s.name for s in sources]
    assert {"idealo_de_drops", "idealo_es_drops", "idealo_it_drops", "geizhals_deals", "tradeinn", "galaxus",
            "asphaltgold", "overkillshop", "sneakersnstuff", "size_uk", "footshop", "kickz"} <= set(ids)
    disabled = {s.name for s in sources if not s.enabled}
    assert {"snipes", "bstn", "sivasdescalzo", "endclothing"} <= disabled
    # LOT 3 : enseignes actives et enseignes bloquées (désactivées avec la raison)
    enabled = {s.name for s in sources if s.enabled}
    assert {"rueducommerce", "materielnet", "pccomponentes", "micromania", "carrefour", "auchan", "aboutyou",
            "alltricks", "snowleader", "alpiniste", "avenuedesjeux", "joybuy"} <= enabled
    assert {"darty", "cdiscount", "intersport", "dyson_outlet"} <= disabled
    assert all(s.cfg.get("_note") for s in sources if s.name in ("darty", "cdiscount", "intersport"))
    assert next(s for s in sources if s.name == "tradeinn").min_interval_minutes == 15


def test_catalog_rejects_bad_entries(cfg):
    c = copy.deepcopy(cfg)
    c["sites_catalog"]["sites"] = [{"id": "dealabs", "engine": "shopify"}, {"id": "x", "engine": "inconnu"},
                                   {"id": "ok", "engine": "shopify", "base_url": "https://a.test"},
                                   {"id": "ok", "engine": "shopify"}]
    sources = build_catalog_sources(c, FakeHttp({}), c["general"], reserved={"dealabs"})
    assert [s.name for s in sources] == ["ok"]


def test_bot_runs_catalog_source_with_its_own_interval_and_threshold(cfg, tmp_path):
    c = copy.deepcopy(cfg)
    for name, sc in c["sources"].items():
        if isinstance(sc, dict) and name not in ("idealo", "idealo_de"):
            sc["enabled"] = False
    site = catalog_site(cfg, "asphaltgold")
    site["min_displayed_discount_pct"] = 65                  # seuil propre à ce site
    c["sites_catalog"]["sites"] = [site]
    c["general"]["silent_first_run"] = False
    c["web_search"]["enabled"] = False
    c["trusted_dtc_brands"]["enabled"] = False
    http = FakeHttp({"asphaltgold.com/collections/sale/products.json": json.loads(read_fixture("shopify_sale.json"))})
    db = StateDB(tmp_path / "c.db")

    class Capture:
        alerts = []

        def send(self, alert, test=False):
            self.alerts.append(alert)
            return True

    bot = Bot(c, None, db, Capture(), http=http)
    stats = bot.run_once()
    assert "asphaltgold" in bot.report and stats["deals"] == 2
    assert stats["triggered"] == 1                           # NB 2002R -70 % (≥ 65) ; Air Max -44 % non
    assert bot.run_once()["deals"] == 0 and bot.report["asphaltgold"] == "intervalle non écoulé"
    db.close()
