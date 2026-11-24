from conftest import FakeHttp, read_fixture

from sources.camel import CamelSource, parse_drop_title, parse_feed, parse_product_stats, parse_top_drops_html
from sources.reddit import RedditSource, parse_atom, parse_listing_json, parse_old_html


def test_camel_drop_title():
    info = parse_drop_title("Apple AirPods 4 - down 74.12% (133,80€) to 46,72€ from 180,52€")
    assert info == {"title": "Apple AirPods 4", "price": 46.72, "original": 180.52, "pct": 74.12, "currency": "EUR"}


def test_camel_feed():
    deals = parse_feed(read_fixture("camel_feed.xml"), "fr")
    assert [d.asin for d in deals] == ["B0CN1SRLDQ", "B0DGJ7HYG1"]
    air = deals[1]
    assert air.source == "camelcamelcamel-fr"
    assert air.url == "https://www.amazon.fr/dp/B0DGJ7HYG1"
    assert air.link_host == "amazon.fr" and air.is_amazon
    assert air.price == 46.72 and air.original_price == 180.52
    assert air.displayed_discount_pct == 74.1
    assert deals[0].title.startswith("GRUNDIG KSM 5330")


def test_camel_product_stats():
    stats = parse_product_stats(read_fixture("camel_product.html"))
    assert stats == {"highest": 199.0, "lowest": 119.0, "current": 46.72, "average": 164.9}


def test_camel_html_fallback():
    html = """<table><tr><td><a href="/product/B0TEST1234" title="Lego Technic 42143">Lego Technic 42143</a></td>
              <td>-72%</td><td>99,99 €</td><td>359,99 €</td></tr></table>"""
    deals = parse_top_drops_html(html, "de")
    assert len(deals) == 1
    d = deals[0]
    assert d.asin == "B0TEST1234" and d.link_host == "amazon.de"
    assert d.price == 99.99 and d.original_price == 359.99 and d.displayed_discount_pct == 72.2


def test_camel_source_history_and_blocked_page():
    cfg = {"enabled": True, "locales": ["fr"], "history_lookup_enabled": True}
    http = FakeHttp({"/top_drops/feed": read_fixture("camel_feed.xml"), "/top_drops": 403,
                     "/product/B0DGJ7HYG1": read_fixture("camel_product.html"), "/product/": 403})
    src = CamelSource(cfg, http)
    assert len(src.fetch()) == 2
    assert src.history("B0DGJ7HYG1", "fr")["average"] == 164.9
    assert src.history("B000BLOCKED", "fr") is None          # 403 Cloudflare -> None, pas d'exception


def test_reddit_json():
    import json
    deals = parse_listing_json(json.loads(read_fixture("reddit_new.json")), "BuildAPCSalesUK")
    assert [d.source_id for d in deals] == ["1abc002", "1abc003"]   # post épinglé ignoré
    d = deals[0]
    assert d.price == 49.99 and d.original_price == 159.99
    assert d.asin == "B0CC5GSSCT" and d.link_host == "amazon.de"
    assert d.url.startswith("https://www.reddit.com/r/BuildAPCSalesUK/comments/1abc002")
    assert deals[1].merchant_url == ""


def test_reddit_atom_and_old_html():
    a = parse_atom(read_fixture("reddit_new.atom"), "GameDeals")
    assert a[0].source_id == "1xyz001" and a[0].merchant_url == "https://www.fanatical.com/fr/game/elden-ring"
    assert a[0].price == 11.99 and a[0].published_at is not None
    o = parse_old_html(read_fixture("old_reddit.html"), "frugalmalefashion")
    assert len(o) == 1
    assert o[0].price == 39.99 and o[0].original_price == 119.99 and o[0].link_host == "nike.com"
    assert o[0].published_at == 1795600000


def test_reddit_source_fallback_chain():
    cfg = {"enabled": True, "subreddits": ["GameDeals"], "fetch_order": ["json", "rss", "html"]}
    http = FakeHttp({"new.json": 429, "/new/.rss": read_fixture("reddit_new.atom")})
    deals = RedditSource(cfg, http).fetch()
    assert len(deals) == 1 and deals[0].source == "reddit-gamedeals"
