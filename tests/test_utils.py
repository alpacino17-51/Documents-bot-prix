import pytest

from utils import (extract_asin, extract_ean, extract_mpn, find_keywords, find_prices, normalize_url, parse_price,
                   search_query_from_title, title_hash, title_similarity, to_eur)


@pytest.mark.parametrize("raw, expected", [
    ("10,79€", (10.79, "EUR")),
    ("1.099,00€", (1099.0, "EUR")),
    ("1 299,99 €", (1299.99, "EUR")),
    ("£12.99", (12.99, "GBP")),
    ("€1,299.99", (1299.99, "EUR")),
    ("$5", (5.0, "USD")),
    ("127,35€", (127.35, "EUR")),
    ("49 EUR", (49.0, "EUR")),
    ("12,5", (12.5, None)),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_find_prices_in_sentence():
    prices = find_prices("Garmin Forerunner 265 à 59,99€ au lieu de 349,99€ (−83 %)")
    assert prices == [(59.99, "EUR"), (349.99, "EUR")]


def test_price_not_glued_to_reference():
    assert find_prices("Robot KitchenAid Artisan 5KSM175 299,00 € 649,00 €") == [(299.0, "EUR"), (649.0, "EUR")]
    assert find_prices("TV 1 299,99 €") == [(1299.99, "EUR")]


def test_to_eur():
    assert to_eur(10, "GBP", {"GBP": 1.17}) == 11.7
    assert to_eur(None, "EUR", {"EUR": 1}) is None
    assert to_eur(10, "JPY", {"EUR": 1}) is None


def test_normalize_url_amazon_and_tracking():
    a = normalize_url("https://www.amazon.fr/Sony-WH-1000XM5/dp/B09Y2MYL5C/ref=sr_1_1?tag=abc-21&th=1#x")
    b = normalize_url("https://amazon.fr/gp/product/B09Y2MYL5C?psc=1")
    assert a == b == "https://amazon.fr/dp/B09Y2MYL5C"
    assert normalize_url("https://www.fnac.com/a123/?utm_source=x&id=5/") == "https://fnac.com/a123?id=5%2F"


def test_extract_ids():
    assert extract_asin("https://www.amazon.de/dp/B0CC5GSSCT?tag=x") == "B0CC5GSSCT"
    assert extract_asin("ASIN : B0DGJ7HYG1") == "B0DGJ7HYG1"
    assert extract_ean("EAN : 8806094767407") == "8806094767407"
    assert extract_ean("EAN : 8806094767405") == ""          # somme de contrôle invalide
    assert extract_ean("commande 1795600000123") == ""        # nombre quelconque
    assert extract_mpn("Référence fabricant : WH1000XM5B.CE7") == "WH1000XM5B.CE7"
    assert extract_mpn("Modèle: abc") == ""                   # pas de chiffre


def test_keywords_word_boundaries_and_accents():
    kws = ["bug", "prix erroné", "Preisfehler"]
    assert find_keywords("Petit BUG sur la fiche", kws) == ["bug"]
    assert find_keywords("debug mode", kws) == []
    assert find_keywords("Prix errone chez Fnac", kws) == ["prix erroné"]
    assert find_keywords("Switch 2 [Preisfehler?]", kws) == ["Preisfehler"]


def test_title_similarity_model_numbers():
    deal = "Casque Sony WH-1000XM5 à réduction de bruit - 99€ sur Amazon"
    assert title_similarity(deal, "Sony WH-1000XM5") > 0.7
    assert title_similarity(deal, "Sony WH-1000XM4") < 0.45
    assert title_similarity(deal, "Apple AirPods Pro 2") < 0.3


def test_title_hash_ignores_price_and_order():
    assert title_hash("Sony WH-1000XM5 à 99€") == title_hash("WH-1000XM5 Sony à 129,99€")
    assert title_hash("") == ""


def test_search_query_from_title():
    q = search_query_from_title("[Prime] Sony WH-1000XM5 Casque sans fil à 199€ sur Amazon")
    assert q.startswith("Sony WH-1000XM5")
    assert "199" not in q
