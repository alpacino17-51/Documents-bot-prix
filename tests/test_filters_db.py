import time

from db import StateDB
from filters import category_ok, evaluate_triggers, is_fashion, price_ok
from models import Deal


def mk(**kw):
    base = dict(source="dealabs", source_id="1", title="Produit", url="https://x")
    base.update(kw)
    return Deal(**base)


def test_price_filter(cfg):
    g = cfg["general"]
    assert price_ok(mk(price_eur=199.99), g)[0]
    assert not price_ok(mk(price_eur=200.01), g)[0]
    assert not price_ok(mk(price_eur=None), g)[0]


def test_category_filter(cfg):
    f = cfg["filters"]
    assert category_ok(mk(category="High-Tech"), f)[0]
    assert category_ok(mk(category="Consoles & Jeux vidéo"), f)[0]
    assert category_ok(mk(category="Sports & plein air"), f)[0]
    assert category_ok(mk(category="Elektronik"), f)[0]
    assert not category_ok(mk(category="Épicerie", title="Café en grains 1 kg"), f)[0]
    # hors catégorie mais marque à fort potentiel de revente
    ok, why = category_ok(mk(category="Jeux & Jouets", title="LEGO Star Wars 75375"), f)
    assert ok and "Lego" in why
    assert category_ok(mk(category="Maison", title="Robot KitchenAid Artisan"), f)[0]
    assert category_ok(mk(category="Cuisine", title="Cocotte Le Creuset 24 cm"), f)[0]
    # sans catégorie (CamelCamelCamel) : mots-clés du titre
    assert category_ok(mk(title="Samsung 990 PRO SSD NVMe 2 To"), f)[0]
    assert not category_ok(mk(title="Lessive liquide 3 L"), f)[0]
    # accessoires à faible intérêt exclus (coques, verres trempés...)
    assert not category_ok(mk(title="OtterBox Cover Slim MagSafe per iPhone 14 Plus"), f)[0]
    assert not category_ok(mk(title="Coque Apple iPhone 16 Pro", category="High-Tech"), f)[0]


def test_fashion_detection(cfg):
    f = cfg["filters"]
    assert is_fashion(mk(title="Nike Dunk Low Retro"), f)
    assert is_fashion(mk(title="Veste The North Face Nuptse"), f)
    assert is_fashion(mk(title="Adidas Samba OG EU 42 2/3"), f)
    assert is_fashion(mk(title="Polo Lacoste", category="Mode & Accessoires"), f)
    assert not is_fashion(mk(title="Sony WH-1000XM5", category="High-Tech"), f)
    assert not is_fashion(mk(title="Écran Samsung Odyssey G5 27 pouces"), f)


def test_triggers(cfg):
    t = cfg["triggers"]
    d = mk(title="Switch 2 [Preisfehler?]", price=149.0, original_price=509.99, temperature=900.0)
    found = evaluate_triggers(d, t, velocity=12.0)
    assert len(found) == 3
    assert any("preisfehler" in x.lower() for x in found)
    assert any("Réduction affichée 71" in x for x in found)
    assert any("Vélocité 12.0" in x for x in found)
    # négation : « pas une erreur de prix » ne déclenche pas
    assert evaluate_triggers(mk(title="Bon prix, pas une erreur de prix"), t, None) == []
    # contexte groupe « Erreur de prix »
    assert evaluate_triggers(mk(title="Écran 27\"", price_error_context=True), t, None)
    # vélocité sous le seuil / température trop basse
    assert evaluate_triggers(mk(title="x", temperature=900.0), t, velocity=2.0) == []
    assert evaluate_triggers(mk(title="x", temperature=10.0), t, velocity=20.0) == []
    # réduction 69 % : non
    assert evaluate_triggers(mk(title="x", price=31.0, original_price=100.0), t, None) == []


def test_db_dedup(tmp_path):
    db = StateDB(tmp_path / "s.db")
    keys = ["url:https://amazon.fr/dp/B0TEST0001", "asin:B0TEST0001", "title:abc"]
    assert db.find_duplicate(keys) is None
    db.record_alert(keys, deal_key="dealabs:1", source="dealabs", title="t", url="u", price_eur=9.9, verdict="true_deal")
    # même produit vu sur une autre source : seul l'ASIN est commun
    assert db.find_duplicate(["deal:mydealz:9", "asin:B0TEST0001"])["deal_key"] == "dealabs:1"
    assert db.find_duplicate(["asin:B0OTHER000"]) is None
    db.close()


def test_db_velocity(tmp_path):
    db = StateDB(tmp_path / "s.db")
    t0 = 1_800_000_000.0
    db.add_temperature("d:1", 50, t0)
    db.add_temperature("d:1", 80, t0 + 120)          # +2 min (fenêtre trop courte seule)
    db.add_temperature("d:1", 150, t0 + 600)         # +10 min
    assert db.velocity("d:1", min_window_min=4, max_window_min=60) == 10.0   # (150-50)/10
    # premier passage : pas d'historique -> température / âge depuis publication
    db.add_temperature("d:2", 300, t0)
    assert db.velocity("d:2", now=t0, published_at=t0 - 600, current_temp=300) == 30.0
    assert db.velocity("d:2", now=t0, published_at=t0 - 60, current_temp=300) is None   # trop récent
    assert db.velocity_samples() == [10.0]
    db.close()


def test_db_cache_and_price_stats(tmp_path):
    db = StateDB(tmp_path / "s.db")
    db.cache_put("idealo:x", {"a": 1})
    assert db.cache_get("idealo:x", 12) == {"a": 1}
    assert db.cache_get("idealo:x", 0) is None
    now = time.time()
    for days, price in [(80, 300), (60, 280), (40, 260), (20, 250), (5, 240), (1, 245)]:
        db.add_price_observation("idealo:1", price, "idealo", ts=now - days * 86400)
    s = db.price_stats("idealo:1", now=now)
    assert s == {"points": 6, "median_90": 255.0, "max_90": 300, "low_30": 240}
    db.close()


def test_source_interval(tmp_path):
    db = StateDB(tmp_path / "s.db")
    assert db.source_due("camel", 10)
    db.mark_source_run("camel")
    assert not db.source_due("camel", 10)
    assert db.source_due("camel", 0)
    db.close()
