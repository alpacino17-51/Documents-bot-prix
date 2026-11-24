"""Envoi des alertes Telegram (parse_mode HTML). Le jeton n'apparaît jamais dans les logs."""
from __future__ import annotations

import html
import logging
import time
from typing import Optional

from models import VERDICT_LABELS, Alert

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
COUNTRY_FLAGS = {"FR": "🇫🇷", "DE": "🇩🇪", "UK": "🇬🇧", "GB": "🇬🇧", "ES": "🇪🇸", "IT": "🇮🇹", "US": "🇺🇸"}
CURRENCY_SIGNS = {"EUR": "€", "GBP": "£", "USD": "$"}
HISTORY_LABELS = {"camel": "CamelCamelCamel", "idealo": "Idealo", "local": "historique local"}


def e(text) -> str:
    return html.escape(str(text), quote=True)


def money(value: Optional[float], currency: str = "EUR") -> str:
    if value is None:
        return "—"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    sign = CURRENCY_SIGNS.get(currency, currency)
    return f"£{s}" if currency == "GBP" else (f"${s}" if currency == "USD" else f"{s} {sign}")


def format_alert(alert: Alert, test: bool = False) -> str:
    d, v = alert.deal, alert.verification
    title = d.title if len(d.title) <= 220 else d.title[:217] + "…"
    flag = COUNTRY_FLAGS.get((d.site_country or "").upper(), "🌐")
    lines = []
    if test:
        lines.append("🧪 <b>[TEST — alerte simulée]</b>")
    lines.append(f"🚨 <b>{e(title)}</b>")
    lines.append("")
    lines.append(f"📰 <b>Source :</b> {e(d.source)}")
    lines.append(f"🏪 <b>Marchand :</b> {e(d.merchant or '—')}")
    lines.append(f"{flag} <b>Pays du site :</b> {e(d.site_country or '—')}")

    price = money(d.price, d.currency)
    if d.currency != "EUR" and d.price_eur is not None:
        price += f" (≈ {money(d.price_eur)})"
    lines.append(f"💶 <b>Prix actuel :</b> <b>{e(price)}</b>")
    if d.original_price:
        orig = money(d.original_price, d.currency)
        lines.append(f"🏷️ <b>Prix barré :</b> <s>{e(orig)}</s>")
    else:
        lines.append("🏷️ <b>Prix barré :</b> —")
    disc = d.displayed_discount_pct
    lines.append(f"📉 <b>Réduction affichée :</b> {f'-{disc:.0f} %' if disc is not None else '—'}")

    if v.real_discount_pct is not None and v.median_90 is not None:
        src = HISTORY_LABELS.get(v.history_source, v.history_source)
        approx = "≈ " if v.history_approx else ""
        real = v.real_discount_pct
        real_txt = f"-{real:.0f} %" if real >= 0 else f"+{abs(real):.0f} % (plus cher que d'habitude)"
        lines.append(f"📊 <b>Réduction réelle vs médiane 90 j :</b> {real_txt} "
                     f"(médiane {approx}{e(money(v.median_90))}, {e(src)})")
    else:
        lines.append("📊 <b>Réduction réelle vs médiane 90 j :</b> —")

    if v.lowest_elsewhere is not None:
        shop = f" chez {e(v.lowest_elsewhere_shop)}" if v.lowest_elsewhere_shop else ""
        lines.append(f"🔎 <b>Plus bas ailleurs (Idealo) :</b> {e(money(v.lowest_elsewhere))}{shop}")
    else:
        reason = f" ({e(v.skipped_idealo_reason)})" if v.skipped_idealo_reason else ""
        lines.append(f"🔎 <b>Plus bas ailleurs (Idealo) :</b> —{reason}")

    lines.append("")
    lines.append(f"🧾 <b>Verdict : {e(VERDICT_LABELS.get(v.verdict, v.verdict))}</b>")
    for note in v.notes[:5]:
        lines.append(f"   • {e(note)}")
    lines.append(f"🎯 <b>Critère(s) :</b> {e(' | '.join(alert.triggers))}")
    if d.temperature is not None:
        vel = f" (+{alert.velocity:.1f} °/min)" if alert.velocity is not None else ""
        lines.append(f"🌡️ <b>Température :</b> {d.temperature:.0f}°{vel}")

    links = [f'<a href="{e(d.url)}">👉 Voir l\'offre</a>']
    if d.merchant_url and d.merchant_url != d.url:
        links.append(f'<a href="{e(d.merchant_url)}">Lien marchand</a>')
    if v.idealo_url:
        links.append(f'<a href="{e(v.idealo_url)}">Idealo</a>')
    lines.append("")
    lines.append(" · ".join(links))
    text = "\n".join(lines)
    return text[:4000]


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, tg_cfg: Optional[dict] = None, session=None):
        self.token = token
        self.chat_id = chat_id
        self.cfg = tg_cfg or {}
        self._session = session

    @property
    def session(self):
        if self._session is None:
            from curl_cffi import requests as cffi_requests

            self._session = cffi_requests.Session()
        return self._session

    def _call(self, method: str, payload: dict, retries: int = 3) -> Optional[dict]:
        url = API.format(token=self.token, method=method)
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                data = resp.json()
            except Exception as exc:  # ne jamais logguer l'URL (elle contient le jeton)
                log.warning("Telegram %s : erreur réseau %s (essai %d/%d)", method, type(exc).__name__, attempt, retries)
                time.sleep(2 * attempt)
                continue
            if data.get("ok"):
                return data
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after:
                log.warning("Telegram : limite de débit, pause %ss", retry_after)
                time.sleep(float(retry_after) + 1)
                continue
            log.error("Telegram %s refusé : %s %s", method, resp.status_code, data.get("description"))
            return None
        return None

    def send_text(self, text: str) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": bool(self.cfg.get("disable_web_page_preview", False)),
        }
        ok = self._call("sendMessage", payload) is not None
        time.sleep(float(self.cfg.get("send_delay_seconds", 1.2)))
        return ok

    def send(self, alert: Alert, test: bool = False) -> bool:
        return self.send_text(format_alert(alert, test=test))

    def get_updates(self) -> list[dict]:
        data = self._call("getUpdates", {"timeout": 0}) or {}
        return data.get("result", [])


class ConsoleNotifier:
    """Mode --dry-run : affiche les messages au lieu de les envoyer."""

    def send(self, alert: Alert, test: bool = False) -> bool:
        import re

        text = format_alert(alert, test=test)
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        print("\n" + "=" * 70 + "\n" + plain + "\n" + "=" * 70)
        return True
