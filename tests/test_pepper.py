from conftest import FakeHttp, read_fixture

from sources.dealabs import DealabsSource
from sources.pepper_common import parse_listing_html, parse_listing_json, parse_rss, parse_topic_comments
from sources.pepper_eu import PepperEUSource, ships_to_france

DEALABS = {"name": "dealabs", "country": "FR", "base_url": "https://www.dealabs.com", "deal_path": "bons-plans",
           "visit_path": "visit/homenew", "currency": "EUR"}


def test_dealabs_listing_json():
    deals = parse_listing_json(read_fixture("dealabs_listing.html"), DEALABS, "dealabs", price_error_context=True)
    ids = [d.source_id for d in deals]
    assert ids == ["3470001", "3470002", "3470003"]           # la discussion est ignorée
    d = deals[0]
    assert d.title.startswith("Écran PC 27\" Samsung Odyssey G5")
    assert d.price == 49.99 and d.original_price == 299.99
    assert d.displayed_discount_pct == 83.3
    assert d.temperature == 523.4
    assert d.category == "High-Tech"
    assert d.merchant == "Cdiscount" and d.link_host == "www.cdiscount.com"
    assert d.url == "https://www.dealabs.com/bons-plans/ecran-pc-27-samsung-odyssey-g5-ls27cg510euxen-qhd-165hz-3470001"
    assert d.merchant_url == "https://www.dealabs.com/visit/homenew/3470001"
    assert d.published_at == 1795600000
    assert "l'écran passe" in d.description
    assert d.price_error_context is True
    assert deals[2].expired is True


def test_dealabs_rss():
    deals = parse_rss(read_fixture("dealabs_rss.xml"), DEALABS, "dealabs", price_error_context=True)
    assert len(deals) == 2
    a, b = deals
    assert a.source_id == "3462249" and a.merchant == "Fanatical" and a.price == 10.79
    assert a.category == "Consoles & Jeux vidéo"
    assert a.published_at is not None
    assert b.price == 1099.0 and b.asin == "B0CXYZ1234"
    assert b.temperature is None


def test_dealabs_html_fallback():
    deals = parse_listing_html(read_fixture("dealabs_listing_legacy.html"), DEALABS, "dealabs")
    a = deals[0]
    assert a.source_id == "3480001"
    assert a.title == "Apple AirPods Pro 2 (USB-C) - Prix erroné"
    assert a.url == "https://www.dealabs.com/bons-plans/apple-airpods-pro-2-usb-c-3480001"
    assert (a.price, a.original_price, a.temperature, a.merchant) == (39.99, 279.0, 845.0, "Rakuten")
    assert deals[1].expired is True


def test_topic_comments():
    topic = {"name": "[SUIVI] Erreurs de prix", "url": "https://www.dealabs.com/discussions/suivi-erreurs-de-prix-1063390"}
    deals = parse_topic_comments(read_fixture("dealabs_topic.html"), topic, DEALABS, "dealabs-topic")
    assert [d.source_id for d in deals] == ["52400001", "52400003"]   # le commentaire sans lien ni prix est ignoré
    g = deals[0]
    assert g.price == 59.99 and g.original_price == 349.99
    assert g.url == "https://www.boulanger.com/ref/1189222"
    assert g.price_error_context
    assert deals[1].asin == "B0CG19FGQ5"


def test_dealabs_source_falls_back_to_rss():
    cfg = {"enabled": True, "fetch_order": ["json", "rss", "html"], "topics_enabled": False,
           "feeds": [{"name": "groupe", "page_url": "https://www.dealabs.com/groupe/erreur-de-prix",
                      "rss_url": "https://www.dealabs.com/rss/groupe/erreur-de-prix", "price_error_context": True}]}
    http = FakeHttp({"/rss/groupe/": read_fixture("dealabs_rss.xml"), "/groupe/erreur-de-prix": 403})
    deals = DealabsSource(cfg, http).fetch()
    assert len(deals) == 2 and all(d.price_error_context for d in deals)


FCFG = {
    "allowed_link_hosts": ["amazon.fr", "amazon.de", "amazon.it", "amazon.es"],
    "amazon_merchant_patterns": ["amazon"],
    "excluded_link_hosts": ["amazon.co.uk", "amazon.com"],
    "ship_to_france_keywords": ["versand nach frankreich", "ships to france"],
}
MYDEALZ = {"name": "mydealz", "country": "DE", "base_url": "https://www.mydealz.de", "deal_path": "deals",
           "currency": "EUR", "page_urls": ["https://www.mydealz.de/new"], "rss_urls": []}


def test_ships_to_france_filter():
    deals = parse_listing_json(read_fixture("mydealz_listing.html"), MYDEALZ, "mydealz")
    kept = [d.source_id for d in deals if ships_to_france(d, FCFG)]
    assert kept == ["2890001", "2890003"]          # amazon.de explicite + marchand Amazon sur site DE
    bose = deals[2]
    assert bose.link_host == "amazon.de"          # domaine déduit du pays du site


def test_uk_amazon_excluded():
    deals = parse_listing_json(read_fixture("mydealz_listing.html"), MYDEALZ, "hotukdeals")
    d = deals[2]
    d.site_country, d.link_host = "UK", ""
    assert ships_to_france(d, FCFG) is False      # Amazon UK : non retenu
    d.link_host = "www.amazon.co.uk"
    assert ships_to_france(d, FCFG) is False
    d.description = "Ships to France with Amazon Global"
    d.link_host = "www.currys.co.uk"
    assert ships_to_france(d, FCFG) is True


def test_pepper_eu_source():
    cfg = {"enabled": True, "fetch_order": ["json", "rss"], "france_delivery": FCFG, "sites": [MYDEALZ]}
    http = FakeHttp({"mydealz.de/new": read_fixture("mydealz_listing.html")})
    deals = PepperEUSource(cfg, http).fetch()
    assert [d.source for d in deals] == ["mydealz", "mydealz"]
    assert deals[0].site_country == "DE"
