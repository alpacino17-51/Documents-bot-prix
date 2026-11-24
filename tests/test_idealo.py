import json
from datetime import datetime, timezone

import pytest
from conftest import FakeHttp, idealo_search_html, read_fixture

from db import StateDB
from models import Deal
from sources.idealo import IdealoSource, parse_history, parse_product_page, parse_search_results

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc).timestamp()
CFG = {"enabled": True, "base_url": "https://www.idealo.fr", "site_id": 4, "cache_hours": 12,
       "min_title_similarity": 0.55, "history_api_enabled": True, "history_period": "P3M"}
FILTERS = {"accessory_keywords": ["etui", "coque", "housse", "protection"]}


def test_parse_search_results():
    items = parse_search_results(idealo_search_html())
    assert [i["product_id"] for i in items] == ["201949755", "203052971", "209999001", "200278049"]  # pub exclue
    first = items[0]
    assert first == {"product_id": "201949755", "title": "Sony WH-1000XM5", "price": 217.99, "shop": "Boulanger.com",
                     "offer_count": 80, "used_only": False,
                     "url": "https://www.idealo.fr/prix/201949755/sony-wh-1000xm5.html"}
    assert items[2]["url"] == "https://www.idealo.fr/prix/209999001/etui-sony-wh-1000xm5.html"


def test_parse_product_page():
    info = parse_product_page(read_fixture("idealo_product.html"), "https://www.idealo.fr/prix/201949755/x.html")
    assert info["product_id"] == "201949755"
    assert info["title"] == "Sony WH-1000XM5"
    assert info["gtin"] == "4548736132597" and info["mpn"] == "WH1000XM5B.CE7"
    assert info["lowest_price"] == 217.99 and info["offer_count"] == 66
    assert info["offers"][:2] == [{"shop": "Amazon.fr", "price": 217.99}, {"shop": "Boulanger.com", "price": 229.0}]


def test_parse_history():
    h = parse_history(json.loads(read_fixture("idealo_history.json")), now=NOW)
    # le point de mai (> 90 j) est exclu
    assert h == {"median_90": 259.0, "low_30": 219.0, "max_90": 289.0, "points": 9}
    assert parse_history({"data": []}) is None
    assert parse_history("not json") is None


def _deal(**kw):
    base = dict(source="dealabs", source_id="1", title="Casque Sony WH-1000XM5 noir à 99€ - erreur de prix",
                url="https://www.dealabs.com/x", merchant="Amazon", price=99.0, price_eur=99.0)
    base.update(kw)
    return Deal(**base)


def recent_history() -> dict:
    """Historique relatif à aujourd'hui (le test reste valable quelle que soit la date)."""
    import time
    now = time.time()
    prices = [279, 269, 289, 279, 259, 249, 239, 229, 219]
    return {"data": [{"x": int((now - (80 - i * 9) * 86400) * 1000), "y": p} for i, p in enumerate(prices)]}


@pytest.fixture
def idealo(tmp_path):
    http = FakeHttp({
        "prechcat.html": idealo_search_html(),
        "/price-chart/": recent_history(),
        "/prix/201949755/": read_fixture("idealo_product.html"),
    })
    src = IdealoSource(CFG, http)
    src.filters_cfg = FILTERS
    src.db = StateDB(tmp_path / "t.db")
    yield src
    src.db.close()


def test_lookup_by_title_rejects_accessory_and_other_models(idealo):
    res = idealo.lookup(_deal())
    assert res["product_id"] == "201949755"
    assert res["matched_by"] == "titre"
    assert res["lowest_price"] == 217.99 and res["lowest_shop"] == "Boulanger.com"
    assert res["history"]["median_90"] == 259.0
    assert res["history"]["points"] == 9


def test_lookup_cache(idealo):
    idealo.lookup(_deal())
    n = len(idealo.http.calls)
    again = idealo.lookup(_deal())
    assert again["product_id"] == "201949755"
    assert len(idealo.http.calls) == n          # servi depuis le cache 12 h


def test_lookup_same_merchant_uses_next_offer(idealo):
    res = idealo.lookup(_deal(merchant="Boulanger"))
    # Boulanger est le moins cher dans la recherche -> on prend l'offre la moins chère d'un AUTRE marchand
    assert res["lowest_shop"] == "Amazon.fr" and res["lowest_price"] == 217.99


def test_lookup_by_ean_direct_redirect(tmp_path):
    http = FakeHttp({"prechcat.html?q=4548736132597": ("https://www.idealo.fr/prix/201949755/sony-wh-1000xm5.html",
                                                        read_fixture("idealo_product.html")),
                     "/price-chart/": 404})
    src = IdealoSource(CFG, http)
    src.db = StateDB(tmp_path / "t.db")
    res = src.lookup(_deal(ean="4548736132597", merchant="Cdiscount"))
    assert res["matched_by"] == "EAN" and res["product_id"] == "201949755"
    assert res["history"] is None                # API historique en 404 -> repli local
    assert src._history_broken is True
    src.db.close()


def test_lookup_no_match_is_cached_as_empty(tmp_path):
    http = FakeHttp({"prechcat.html": idealo_search_html()})
    src = IdealoSource(CFG, http)
    src.filters_cfg = FILTERS
    src.db = StateDB(tmp_path / "t.db")
    assert src.lookup(_deal(title="Robot pâtissier KitchenAid Artisan 5KSM175")) is None
    calls = len(http.calls)
    assert src.lookup(_deal(title="Robot pâtissier KitchenAid Artisan 5KSM175")) is None
    assert len(http.calls) == calls
    src.db.close()
