"""LOT 3 : moteur générique `retailer` (JSON-LD, JSON embarqué, Shopify, vignettes HTML)."""
import json

from conftest import FakeHttp, read_fixture

from sources.retailer_generic import (RetailerSource, embedded_json_blobs, parse_embedded, parse_jsonld_discounted,
                                      shopify_collection_url)

BASE = "https://www.enseigne.test"


def test_embedded_json_next_data_and_initial_state():
    html = read_fixture("retailer_embedded.html")
    assert len(list(embedded_json_blobs(html))) == 2
    deals = {d.title: d for d in parse_embedded(html, BASE, "enseigne")}
    assert set(deals) == {"Casque Sony WH-1000XM5 Noir", "Console Nintendo Switch OLED", "Dyson Robot Dyson V15 Detect"} \
        or set(deals) == {"Casque Sony WH-1000XM5 Noir", "Console Nintendo Switch OLED", "Robot Dyson V15 Detect"}
    sony = deals["Casque Sony WH-1000XM5 Noir"]
    assert (sony.price, sony.original_price, sony.brand) == (199.99, 419.99, "Sony")
    assert sony.url == "https://www.enseigne.test/p/casque-sony-wh1000xm5-noir/P1"
    switch = deals["Console Nintendo Switch OLED"]
    assert (switch.price, switch.original_price) == (289.99, 349.99)
    assert switch.url == "https://www.enseigne.test/p/switch-oled/P2"
    dyson = next(d for t, d in deals.items() if "V15" in t)
    assert (dyson.price, dyson.original_price, dyson.brand) == (449.0, 749.0, "Dyson")
    # câble sans ancien prix et produit sans lien : ignorés


def test_jsonld_only_discounted():
    deals = parse_jsonld_discounted(read_fixture("retailer_jsonld.html"), "https://www.sport.test", "sport")
    assert len(deals) == 1
    g = deals[0]
    assert (g.title, g.price, g.original_price, g.brand) == ("Garmin Forerunner 265", 249.99, 449.99, "Garmin")
    assert g.ean == "0753759315658" and g.mpn == ""


def test_shopify_collection_url():
    assert shopify_collection_url("https://www.shop.test/collections/outlet?page=2") == \
        "https://www.shop.test/collections/outlet/products.json?limit=250"
    assert shopify_collection_url("https://www.shop.test/fr/collections/soldes") == \
        "https://www.shop.test/fr/collections/soldes/products.json?limit=250"
    assert shopify_collection_url("https://www.shop.test/promo") is None


def test_retailer_source_strategy_order_and_blocking():
    cfg = {"id": "enseigne", "label": "Enseigne", "base_url": BASE, "category": "High-tech",
           "page_urls": [BASE + "/soldes", BASE + "/destockage"]}
    http = FakeHttp({"/soldes": read_fixture("retailer_embedded.html"),
                     "/destockage": read_fixture("retailer_jsonld.html")})
    deals = RetailerSource(cfg, http).safe_fetch()
    assert len(deals) == 4 and all(d.origin == "enseigne" and d.merchant == "Enseigne" for d in deals)
    assert deals[0].displayed_discount_pct >= deals[-1].displayed_discount_pct      # trié par remise
    assert all(d.category == "High-tech" for d in deals)
    # protection anti-robot : signalée, la 2e page n'est pas demandée
    blocked = FakeHttp({"enseigne.test": 403})
    src = RetailerSource(cfg, blocked)
    assert src.safe_fetch() == [] and src.blocked == "403" and len(blocked.calls) == 1


def test_retailer_shopify_detection():
    page = '<html><head><script src="https://cdn.shopify.com/s/files/theme.js"></script></head><body></body></html>'
    payload = json.loads(read_fixture("shopify_sale.json"))
    cfg = {"id": "shop", "base_url": "https://www.shop.test", "page_urls": ["https://www.shop.test/collections/outlet"]}
    http = FakeHttp({"collections/outlet/products.json": payload, "collections/outlet": page})
    deals = RetailerSource(cfg, http).safe_fetch()
    assert len(deals) == 2 and deals[0].brand == "New Balance"


def test_cards_real_world_price_formats():
    from sources.sneakers_generic import parse_cards
    html = """
    <div class="c-product"><a href="/produit/250213560142.html"><span>Lenovo ThinkPad T14 Gen 1 - Reconditionné</span></a>
      <div class="o-product__prices">Prix promo jusqu'au 30/11 inclus
        <span class="o-product__cut-price">339€<sup>00</sup></span><span class="o-product__price--promo">322€<sup>05</sup></span></div></div>
    <div class="tile"><a href="/p/levi-s/veste-28636619">OFFRE LEVI'S ® Veste mi-saison 'SBEYONCÉ' 147,50 € Tailles disponibles: XS, S</a>
      <span>147,50 €</span><span>À l'origine : 295,00 €</span><span>Dernier prix le plus bas : 140,00 €</span></div>
    <div class="tile"><a href="/pages/detail2_ref_est_in1.html">PC Gamer BARK (Edition limitée)</a>
      <span>400 € de remise avec le code BARK</span><span>1 999,99 €</span><span class="old-price">2 399,99 €</span></div>
    <div class="tile"><a href="/samsung-galaxy-a17/pr-C1849228">(10) SAMSUNG Galaxy A17 5G 128Go - Noir 229,99€ / pce</a>
      <span>229,99€ / pce</span><span>-23 %</span><span class="price--old">299,99€</span></div>
    """
    deals = {d.url.rsplit("/", 1)[-1]: d for d in parse_cards(html, "https://x.test", "t", link_regex=r"/(produit|p|pages|.*pr-C)")}
    lenovo = deals["250213560142.html"]
    assert (lenovo.price, lenovo.original_price) == (322.05, 339.0)
    levis = deals["veste-28636619"]
    assert (levis.price, levis.original_price, levis.merchant_ref) == (147.5, 295.0, {"low_30": 140.0})
    assert levis.title == "LEVI'S ® Veste mi-saison 'SBEYONCÉ'"
    pc = deals["detail2_ref_est_in1.html"]
    assert (pc.price, pc.original_price) == (1999.99, 2399.99)          # « 400 € de remise » n'est pas un prix
    phone = deals["pr-C1849228"]
    assert phone.title == "SAMSUNG Galaxy A17 5G 128Go - Noir" and (phone.price, phone.original_price) == (229.99, 299.99)


def test_retailer_cards_fallback_keeps_only_discounted():
    html = """<ul><li class="tile"><a href="/p/robot-kitchenaid-artisan-5ksm175">Robot KitchenAid Artisan 5KSM175</a>
              <span>299,00 €</span><s>649,00 €</s></li>
              <li class="tile"><a href="/p/fouet-inox">Fouet inox</a><span>12,90 €</span></li></ul>"""
    cfg = {"id": "mda", "base_url": "https://www.mda.test", "page_urls": ["https://www.mda.test/destockage"],
           "link_contains": "/p/"}
    deals = RetailerSource(cfg, FakeHttp({"destockage": html})).safe_fetch()
    assert [(d.title, d.price, d.original_price) for d in deals] == [
        ("Robot KitchenAid Artisan 5KSM175", 299.0, 649.0)]
