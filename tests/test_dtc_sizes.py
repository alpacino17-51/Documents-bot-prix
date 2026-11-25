"""Filtre de tailles propre à `trusted_dtc_brands` (divinbydivin.shop, davril.com) — sans effet sur les autres sources."""
import copy

from conftest import FakeHttp, read_fixture

from db import StateDB
from filters import category_ok, evaluate_triggers, price_ok
from models import Alert, Deal, Verification
from notifier import format_alert
from sources.courir import parse_listing as courir_parse
from sources.dtc_shopify import (DTCShopifySource, is_one_size_product, is_size_value, normalize_size,
                                 select_variants, variant_options, variant_size)

WANTED = ["M", "L", "M/L"]


def product(title, variants, option_names=("Size",), pid=1):
    return {"id": pid, "title": title, "handle": title.lower().replace(" ", "-"), "vendor": "DAVRILSUPPLY",
            "options": [{"name": n} for n in option_names],
            "variants": [{"title": " / ".join(opts), "option1": opts[0],
                          "option2": opts[1] if len(opts) > 1 else None,
                          "option3": opts[2] if len(opts) > 2 else None,
                          "price": "47.98", "compare_at_price": "119.95", "available": avail}
                         for opts, avail in variants]}


# ---------------------------------------------------------------- correspondance exacte

def test_exact_matching_never_substring():
    assert normalize_size(" m / l ") == "M/L" and normalize_size("xl") == "XL"
    for size in ("S", "M", "L", "XL", "XXL", "XS", "S/M", "M/L", "2XL", "32", "34/32", "W32", "42.5", "EU 42"):
        assert is_size_value(size), size
    for not_size in ("Black", "Noir", "Camel", "Default Title", "One Size"):
        assert not is_size_value(not_size), not_size
    # « Noir / M/L » : découpage sur « / » entouré d'espaces uniquement -> M/L reste entier
    assert variant_options({"title": "Noir / M/L"}) == ["Noir", "M/L"]
    assert variant_size({"title": "Noir / M/L"}) == "M/L"
    # XL, XXL, XS, S/M ne correspondent jamais à M ou L
    p = product("TEE", [(["XL"], True), (["XXL"], True), (["XS"], True), (["S/M"], True)])
    assert select_variants(p, WANTED, True) == ([], [])


# ---------------------------------------------------------------- cas demandés

def test_1_hoodie_only_m_in_stock_is_kept():
    hoodie = product("OLDSALT HOODIE", [(["S"], False), (["M"], True), (["L"], False), (["XL"], True)])
    matched, sizes = select_variants(hoodie, WANTED, True)
    assert sizes == ["M"] and [v["option1"] for v in matched] == ["M"]


def test_2_jacket_double_size_m_l_is_kept():
    jacket = product("WORK JACKET", [(["S/M"], False), (["M/L"], True), (["L/XL"], False)])
    assert select_variants(jacket, WANTED, True)[1] == ["M/L"]
    # double taille dans le titre, avec la couleur en première option
    jacket2 = product("WORK JACKET", [(["Noir", "M/L"], True), (["Noir", "S/M"], True)], option_names=("Color", "Taille"))
    assert select_variants(jacket2, WANTED, True)[1] == ["M/L"]


def test_3_pants_with_only_s_sm_xl_in_stock_is_ignored():
    pants = product("CARGO PANTS", [(["S"], True), (["S/M"], True), (["M"], False), (["L"], False), (["XL"], True)])
    assert select_variants(pants, WANTED, True) == ([], [])
    # pantalons chiffrés : il suffit d'ajouter « 32 », « 34 » à wanted_sizes
    numeric = product("DENIM", [(["30"], True), (["32"], True), (["34"], False)])
    assert select_variants(numeric, WANTED, True) == ([], [])
    assert select_variants(numeric, WANTED + ["32", "34"], True)[1] == ["32"]


def test_4_one_size_accessories_depend_on_allow_one_size():
    bag = product("TACTIC SHOULDER BAG", [(["Default Title"], True)], option_names=("Title",))
    balaclava = product("BALACLAVA", [(["One Size"], True)])
    colours = product("BEANIE", [(["Black"], True), (["Camel"], False)], option_names=("Color",))   # que des couleurs
    for p in (bag, balaclava, colours):
        assert is_one_size_product(p)
        assert select_variants(p, WANTED, True)[1] == ["Taille unique"]
        assert select_variants(p, WANTED, False) == ([], [])
    sold_out_bag = product("BAG", [(["Default Title"], False)], option_names=("Title",))
    assert select_variants(sold_out_bag, WANTED, True) == ([], [])


def test_5_other_sources_are_not_affected(cfg):
    # Dealabs / Courir : aucun filtre de taille, même avec wanted_sizes = ["M", "L", "M/L"] dans la config
    assert cfg["trusted_dtc_brands"]["wanted_sizes"] == WANTED
    dealabs = Deal(source="dealabs", source_id="1", title="Sweat Nike Tech Fleece - Taille S et XL",
                   url="https://www.dealabs.com/x", price=19.99, original_price=99.99, price_eur=19.99,
                   category="Mode & Accessoires", origin="dealabs")
    assert price_ok(dealabs, cfg["general"])[0] and category_ok(dealabs, cfg["filters"])[0]
    assert evaluate_triggers(dealabs, cfg["triggers"], None, cfg["filters"]["resale_brands"])
    assert dealabs.sizes == []
    html = read_fixture("courir_outlet.html").replace("GEL-1130", "GEL-1130 XL").replace("gel-1130", "gel-1130 taille S")
    courir = {d.source_id: d for d in courir_parse(html)}
    assert courir["1599001"].title == "Asics Gel-1130 Xl" and courir["1599001"].sizes == []
    # l'alerte d'une autre source n'affiche aucune ligne de tailles
    assert "📏" not in format_alert(Alert(dealabs, Verification(), ["t"]))


# ---------------------------------------------------------------- module complet + alerte

def test_source_filters_alert_but_records_whole_catalog(cfg, tmp_path):
    db = StateDB(tmp_path / "z.db")
    catalog = {"products": [
        product("OLDSALT HOODIE", [(["S"], False), (["M"], True), (["L"], False)], pid=11),
        product("CARGO PANTS", [(["S"], True), (["S/M"], True), (["M"], False), (["L"], False)], pid=12),
        product("TACTIC SHOULDER BAG", [(["Default Title"], True)], option_names=("Title",), pid=13),
    ]}
    c = copy.deepcopy(cfg["trusted_dtc_brands"])
    c["brands"] = [{"id": "davril", "name": "Davril Supply", "base_url": "https://davril.com", "collections": []}]
    src = DTCShopifySource(c, FakeHttp({"davril.com/products.json": catalog}))
    src.db = db
    deals = {d.title: d for d in src.safe_fetch()}
    assert set(deals) == {"OLDSALT HOODIE", "TACTIC SHOULDER BAG"}             # pantalon : pas de M/L en stock
    assert deals["OLDSALT HOODIE"].sizes == ["M"] and deals["TACTIC SHOULDER BAG"].sizes == ["Taille unique"]
    # historique : les 3 produits sont enregistrés, y compris le pantalon filtré
    assert all(db.price_history(f"dtc:davril.com:{pid}") for pid in (11, 12, 13))
    one = format_alert(Alert(deals["OLDSALT HOODIE"], Verification(), ["t"]))
    assert "Taille dispo :</b> M" in one
    assert "📏 <b>Taille unique</b>" in format_alert(Alert(deals["TACTIC SHOULDER BAG"], Verification(), ["t"]))
    # allow_one_size: false par marque -> la sacoche disparaît
    c["brands"][0]["allow_one_size"] = False
    db2 = StateDB(tmp_path / "z2.db")
    src2 = DTCShopifySource(c, FakeHttp({"davril.com/products.json": catalog}))
    src2.db = db2
    assert {d.title for d in src2.safe_fetch()} == {"OLDSALT HOODIE"}
    db.close()
    db2.close()
