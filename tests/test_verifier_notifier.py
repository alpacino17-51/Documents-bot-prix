from main import simulated_alerts
from models import VERDICT_INFLATED, VERDICT_TRUE, VERDICT_UNVERIFIED, Alert, Deal
from notifier import TelegramNotifier, format_alert
from verifier import Verifier, compute_verdict

V = {"true_deal_min_below_median_pct": 35, "inflated_tolerance_pct": 5, "inflated_min_real_discount_pct": 30,
     "strong_displayed_discount_pct": 50, "require_elsewhere_for_true_deal": False}
H = {"median_90": 200.0, "low_30": 180.0, "max_90": 230.0}


def test_true_deal():
    verdict, real, notes = compute_verdict(100.0, 210.0, 52.4, H, 170.0, "Fnac", V, others=3,
                                           comparison_complete=True)
    assert verdict == VERDICT_TRUE and real == 50.0
    assert any("gonflé" in n for n in notes)        # mention secondaire : barré 210 > 180*1.05


def test_true_deal_never_without_other_merchant_or_official_price():
    verdict, _, notes = compute_verdict(100.0, None, None, H, None, "", V, others=None)
    assert verdict == VERDICT_UNVERIFIED and any("Aucun autre marchand" in n for n in notes)


def test_true_deal_never_from_merchant_reference_only():
    verdict, _, _ = compute_verdict(100.0, 300.0, 66.7, {"median_90": 300.0}, None, "", V, others=2,
                                    comparison_complete=True, history_eligible=False)
    assert verdict != VERDICT_TRUE


def test_true_deal_from_official_price():
    verdict, _, _ = compute_verdict(60.0, 199.0, 69.8, None, 90.0, "Fnac", V, others=1, official_price=120.0)
    assert verdict == VERDICT_TRUE                  # 50 % sous le prix officiel et moins cher que partout


def test_official_price_rules():
    v1, _, n1 = compute_verdict(110.0, 250.0, 56.0, None, None, "", V, others=2, official_price=119.0)
    assert v1 == VERDICT_INFLATED
    assert any("prix barré 250,00 € > prix officiel 119,00 €" in n for n in n1)
    assert any("promo proche du prix officiel" in n for n in n1)


def test_brand_and_exclusivity_rules():
    assert compute_verdict(50.0, 300.0, 83.0, None, None, "", V, brand_status="blocked")[0] == VERDICT_INFLATED
    assert compute_verdict(50.0, 300.0, 83.0, None, None, "", V, brand_status="fake_or_exclusive")[0] == VERDICT_INFLATED
    v, _, notes = compute_verdict(50.0, 300.0, 83.0, H, None, "", V, brand_status="real", others=0,
                                  comparison_complete=True)
    assert v == VERDICT_INFLATED and "exclusivement" in notes[0]
    # contrôles incomplets (budget / blocage) : pas de conclusion d'exclusivité
    v2, _, _ = compute_verdict(50.0, None, None, None, None, "", V, brand_status="real", others=0,
                               comparison_complete=False)
    assert v2 == VERDICT_UNVERIFIED


def test_true_deal_requires_cheaper_than_elsewhere():
    verdict, _, notes = compute_verdict(100.0, None, None, H, 95.0, "Boulanger", V)
    assert verdict == VERDICT_INFLATED
    assert any("Moins cher ailleurs" in n for n in notes)


def test_inflated_strike_price():
    verdict, real, notes = compute_verdict(150.0, 500.0, 70.0, H, 160.0, "Fnac", V)
    assert verdict == VERDICT_INFLATED and real == 25.0
    joined = " ".join(notes)
    assert "plus bas des 30 derniers jours" in joined
    assert "jamais pratiqué" in joined
    assert "réduction réelle 25 %" in joined
    assert "Vrai prix habituel" in joined


def test_strike_within_tolerance_not_inflated():
    # barré 188 <= 180*1.05 = 189 -> pas gonflé ; réduction réelle 15 % -> pas une vraie affaire
    verdict, _, notes = compute_verdict(170.0, 188.0, 9.6, H, None, "", V)
    assert verdict == VERDICT_INFLATED
    assert any("réduction faible face au marché" in n for n in notes)


def test_unverified():
    verdict, real, notes = compute_verdict(50.0, 200.0, 75.0, None, None, "", V)
    assert verdict == VERDICT_UNVERIFIED and real is None


class StubIdealo:
    enabled = True

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def lookup(self, deal):
        self.calls += 1
        return self.result


def test_verifier_skips_idealo_for_fashion(cfg, tmp_path):
    from db import StateDB
    db = StateDB(tmp_path / "v.db")
    idealo = StubIdealo({"product_id": "1", "lowest_price": 50.0, "lowest_shop": "X", "url": "u", "title": "t",
                         "history": {"median_90": 200.0, "low_30": 190.0, "max_90": 210.0}})
    v = Verifier(cfg, db, idealo, None)
    shoe = Deal(source="hotukdeals", source_id="1", title="Nike Dunk Low Retro", url="u", price=30.0, price_eur=30.0)
    res = v.verify(shoe)
    assert idealo.calls == 0 and res.verdict == VERDICT_UNVERIFIED
    assert "mode" in res.skipped_idealo_reason
    tv = Deal(source="dealabs", source_id="2", title="Écran Samsung Odyssey G5", url="u", price=99.0, price_eur=99.0)
    res = v.verify(tv)
    assert idealo.calls == 1
    assert res.verdict == VERDICT_INFLATED            # 99 € > plus bas ailleurs 50 €
    assert res.history_source == "idealo" and res.median_90 == 200.0
    db.close()


def test_format_alert_contains_all_fields_and_escapes():
    alerts = simulated_alerts()
    text = format_alert(alerts[0], test=True)
    for label in ("Source", "Marchand", "Pays du site", "Prix actuel", "Prix barré", "Réduction affichée",
                  "Réduction réelle vs médiane 90 j", "Plus bas ailleurs (Idealo)", "Verdict", "Critère(s)",
                  "Température", "Lien marchand", "Fiche Dealabs", "Marque", "Prix officiel",
                  "Marchands comparés"):
        assert label in text, label
    assert "✅ Vraie affaire" in text and "-64 %" in text
    assert "⚠️" in format_alert(alerts[1]) and "≈ 229,00 €" in format_alert(alerts[1])
    uk = format_alert(alerts[2])
    assert "❓ Non vérifié" in uk and "£24,99 (≈ 29,24 €)" in uk
    d = Deal(source="x", source_id="1", title="<script>alert(1)</script> & co", url="https://a.b/?x=1&y=2")
    from models import Verification
    t = format_alert(Alert(d, Verification(), ["t"]))
    assert "<script>" not in t and "&lt;script&gt;" in t and "x=1&amp;y=2" in t


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def post(self, url, json=None, timeout=None):
        self.payloads.append(json)
        return self.responses.pop(0)


def test_telegram_send_and_rate_limit(monkeypatch):
    import notifier
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    session = FakeSession([FakeResp(429, {"ok": False, "parameters": {"retry_after": 1}}), FakeResp(200, {"ok": True})])
    tg = TelegramNotifier("123:ABC", "42", {"send_delay_seconds": 0}, session=session)
    assert tg.send(simulated_alerts()[0], test=True)
    assert session.payloads[-1]["parse_mode"] == "HTML" and session.payloads[-1]["chat_id"] == "42"
    bad = TelegramNotifier("123:ABC", "42", {"send_delay_seconds": 0},
                           session=FakeSession([FakeResp(400, {"ok": False, "description": "chat not found"})]))
    assert bad.send_text("x") is False
