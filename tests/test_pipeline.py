import copy
import time

from conftest import FakeHttp, idealo_search_html, read_fixture

from db import StateDB
from main import Bot, dedup_keys


class CaptureNotifier:
    def __init__(self):
        self.alerts = []

    def send(self, alert, test=False):
        self.alerts.append(alert)
        return True


def fresh_listing() -> str:
    """Fixture Dealabs avec des dates de publication récentes (sinon filtrées par max_deal_age_hours)."""
    return read_fixture("dealabs_listing.html").replace("1795600000", str(int(time.time()) - 600)) \
        .replace("1795590000", str(int(time.time()) - 3600))


def make_bot(cfg, tmp_path, http, silent=False):
    cfg = copy.deepcopy(cfg)
    cfg["general"]["silent_first_run"] = silent
    cfg["http"]["min_delay_seconds"] = cfg["http"]["max_delay_seconds"] = 0
    src = cfg["sources"]
    src["dealabs"]["topics_enabled"] = False
    src["dealabs"]["feeds"] = [src["dealabs"]["feeds"][0]]            # flux « nouveaux » (sans contexte)
    src["pepper_eu"]["sites"] = [s for s in src["pepper_eu"]["sites"] if s["name"] == "mydealz"]
    src["pepper_eu"]["sites"][0]["page_urls"] = ["https://www.mydealz.de/new"]
    src["pepper_eu"]["sites"][0]["rss_urls"] = []
    src["camelcamelcamel"]["locales"] = ["fr"]
    src["camelcamelcamel"]["min_interval_minutes"] = 0
    src["reddit"]["enabled"] = False
    db = StateDB(tmp_path / "state.db")
    notifier = CaptureNotifier()
    return Bot(cfg, None, db, notifier, http=http), notifier, db


def routes(listing):
    return {
        "dealabs.com/nouveaux": listing,
        "mydealz.de/new": read_fixture("mydealz_listing.html").replace("1795600000", str(int(time.time()) - 300)),
        "/top_drops/feed": read_fixture("camel_feed.xml"),
        "camelcamelcamel.com/product/": 403,                  # Cloudflare : historique camel indisponible
        "prechcat.html": idealo_search_html(),
        "/price-chart/": 404,
    }


def test_full_run(cfg, tmp_path):
    http = FakeHttp(routes(fresh_listing()))
    bot, notifier, db = make_bot(cfg, tmp_path, http)
    stats = bot.run_once()
    titles = {a.deal.source: [] for a in notifier.alerts}
    for a in notifier.alerts:
        titles[a.deal.source].append(a.deal.title)
    # Dealabs : écran Samsung (mot-clé + -83 %) ; Lego sans critère ; expiré ignoré
    assert any("Samsung Odyssey" in t for t in titles["dealabs"])
    assert not any("LEGO" in t for t in titles["dealabs"])
    # mydealz : Switch 2 (Preisfehler, -71 %) livrée par amazon.de ; Bose (Amazon, -56 %) déclenché
    # uniquement par la vélocité : 330° en 5 min = 66 °/min ; SSD MediaMarkt non livrable en France
    assert titles["mydealz"] == ["Nintendo Switch 2 + Mario Kart World Bundle [Preisfehler?]",
                                 "Bose QuietComfort Ultra Kopfhörer"]
    bose = next(a for a in notifier.alerts if "Bose" in a.deal.title)
    assert len(bose.triggers) == 1 and bose.triggers[0].startswith("Vélocité")
    # camel : AirPods 4 -74 % ; Grundig -7 % -> rien
    assert titles["camelcamelcamel-fr"] == ["Apple AirPods 4 avec Réduction Active du Bruit"]
    assert stats["sent"] == len(notifier.alerts) == 4

    switch = next(a for a in notifier.alerts if a.deal.source == "mydealz")
    assert switch.velocity is not None and switch.velocity > 5          # 1203° en ~5 min
    assert any("Vélocité" in t for t in switch.triggers)

    # 2e passage : aucun doublon renvoyé
    notifier.alerts.clear()
    stats2 = bot.run_once()
    assert stats2["sent"] == 0 and stats2["duplicates"] >= 4
    db.close()


def test_silent_first_run(cfg, tmp_path):
    http = FakeHttp(routes(fresh_listing()))
    bot, notifier, db = make_bot(cfg, tmp_path, http, silent=True)
    stats = bot.run_once()
    assert stats["sent"] == 0 and stats["bootstrap"] == 4 and notifier.alerts == []
    assert db.is_initialized()
    assert bot.run_once()["sent"] == 0          # déjà mémorisés
    db.close()


def test_cross_source_dedup_by_asin(cfg, tmp_path):
    http = FakeHttp(routes(fresh_listing()))
    bot, notifier, db = make_bot(cfg, tmp_path, http)
    from models import Deal
    a = Deal(source="camelcamelcamel-fr", source_id="B0DGJ7HYG1", title="AirPods 4", url="https://www.amazon.fr/dp/B0DGJ7HYG1",
             asin="B0DGJ7HYG1")
    b = Deal(source="dealabs", source_id="999", title="Apple AirPods 4 ANC à 46€", url="https://www.dealabs.com/bons-plans/x-999",
             merchant_url="https://www.dealabs.com/visit/homenew/999", asin="B0DGJ7HYG1")
    db.record_alert(dedup_keys(a), deal_key=a.key, source=a.source, title=a.title, url=a.url, price_eur=46.7,
                    verdict="x")
    assert db.find_duplicate(dedup_keys(b)) is not None
    assert not any(k.startswith("url:https://dealabs.com/visit") for k in dedup_keys(b))
    db.close()
