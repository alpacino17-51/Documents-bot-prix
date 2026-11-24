"""Bot de veille tarifaire / erreurs de prix -> alertes Telegram.

Exemples :
    python main.py --once            # un seul passage (utilisé par GitHub Actions)
    python main.py --loop 120        # boucle locale toutes les 120 s
    python main.py --test            # envoie 3 alertes simulées (✅ ⚠️ ❓) sur Telegram
    python main.py --once --dry-run  # n'envoie rien, affiche les alertes dans la console
    python main.py --get-chat-id     # affiche le chat_id des derniers messages reçus par le bot
    python main.py --velocity-report # propose un seuil de vélocité (°/min) d'après l'historique
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import statistics
import sys
import time
from pathlib import Path

from db import StateDB
from filters import category_ok, evaluate_triggers, price_ok
from http_client import HttpClient
from models import VERDICT_INFLATED, VERDICT_TRUE, VERDICT_UNVERIFIED, Alert, Deal, Verification
from notifier import ConsoleNotifier, TelegramNotifier
from settings import ROOT, load_config, load_dotenv_if_present, load_secrets
from sources import FEED_SOURCES, CamelSource, IdealoSource
from utils import extract_asin, extract_ean, extract_mpn, normalize_url, title_hash, to_eur
from verifier import Verifier

log = logging.getLogger("bot")


# ------------------------------------------------------------------ logs

def setup_logging(level: str, to_file: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        (ROOT / "logs").mkdir(exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=2_000_000,
                                                             backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers, force=True)


# ------------------------------------------------------------------ pipeline

def dedup_keys(deal: Deal) -> list[str]:
    keys = [f"deal:{deal.key}"]
    if deal.url:
        keys.append("url:" + normalize_url(deal.url))
    if deal.merchant_url and "/visit/" not in deal.merchant_url:
        keys.append("url:" + normalize_url(deal.merchant_url))
    if deal.asin:
        keys.append(f"asin:{deal.asin}")
    if deal.ean:
        keys.append(f"ean:{deal.ean}")
    th = title_hash(deal.title)
    if th:
        keys.append(f"title:{th}")
    return list(dict.fromkeys(keys))


class Bot:
    def __init__(self, cfg: dict, secrets, db: StateDB, notifier, http: HttpClient | None = None):
        self.cfg = cfg
        self.secrets = secrets
        self.db = db
        self.notifier = notifier
        self.http = http or HttpClient(cfg.get("http", {}), secrets)
        general = cfg.get("general", {})
        src_cfg = cfg.get("sources", {})
        self.sources = []
        for name, cls in FEED_SOURCES.items():
            src = cls(src_cfg.get(name, {}), self.http, general)
            src.db = db
            self.sources.append(src)
        self.camel = next((s for s in self.sources if isinstance(s, CamelSource)), None)
        self.idealo = IdealoSource(src_cfg.get("idealo", {}), self.http, general)
        self.idealo.db = db
        self.idealo.filters_cfg = cfg.get("filters", {})
        self.verifier = Verifier(cfg, db, self.idealo, self.camel)

    def collect(self) -> list[Deal]:
        deals: list[Deal] = []
        for src in self.sources:
            if not src.enabled:
                continue
            if not self.db.source_due(src.name, src.min_interval_minutes):
                log.debug("%s : intervalle minimal non écoulé, ignorée ce passage", src.name)
                continue
            deals.extend(src.safe_fetch())
            self.db.mark_source_run(src.name)
        return deals

    def run_once(self) -> dict:
        general = self.cfg.get("general", {})
        triggers_cfg = self.cfg.get("triggers", {})
        vel_cfg = triggers_cfg.get("velocity", {})
        fx = general.get("fx_to_eur", {"EUR": 1.0})
        bootstrap = general.get("silent_first_run", True) and not self.db.is_initialized()
        max_alerts = int(general.get("max_alerts_per_run", 15))
        max_age = float(general.get("max_deal_age_hours", 48)) * 3600

        started = time.time()
        if hasattr(self.http, "reset_run"):
            self.http.reset_run()
        deals = self.collect()
        now = time.time()
        stats = {"deals": len(deals), "triggered": 0, "duplicates": 0, "sent": 0, "bootstrap": 0, "failed": 0}
        seen_this_run: set[str] = set()

        for deal in deals:
            deal.price_eur = to_eur(deal.price, deal.currency, fx)
            deal.original_price_eur = to_eur(deal.original_price, deal.currency, fx)
            if deal.temperature is not None:
                self.db.add_temperature(deal.key, deal.temperature, now, deal.published_at)
            if deal.expired:
                continue
            if deal.published_at and now - deal.published_at > max_age:
                continue
            ok, why = price_ok(deal, general)
            if not ok:
                log.debug("Ignoré (%s) : %s", why, deal.title[:80])
                continue
            ok, why = category_ok(deal, self.cfg.get("filters", {}))
            if not ok:
                log.debug("Ignoré (%s) : %s", why, deal.title[:80])
                continue

            velocity = None
            if deal.temperature is not None:
                velocity = self.db.velocity(
                    deal.key, now=now, min_window_min=float(vel_cfg.get("min_window_minutes", 4)),
                    max_window_min=float(vel_cfg.get("max_window_minutes", 60)),
                    published_at=deal.published_at if vel_cfg.get("use_publication_time", True) else None,
                    current_temp=deal.temperature)
            triggers = evaluate_triggers(deal, triggers_cfg, velocity)
            if not triggers:
                continue
            stats["triggered"] += 1

            deal.asin = deal.asin or extract_asin(deal.merchant_url, deal.url, deal.description)
            deal.ean = deal.ean or extract_ean(deal.title, deal.description)
            deal.mpn = deal.mpn or extract_mpn(deal.title, deal.description)
            keys = dedup_keys(deal)
            if seen_this_run.intersection(keys) or self.db.find_duplicate(keys):
                stats["duplicates"] += 1
                continue
            seen_this_run.update(keys)

            if bootstrap:
                self.db.record_alert(keys, deal_key=deal.key, source=deal.source, title=deal.title, url=deal.url,
                                     price_eur=deal.price_eur, verdict="bootstrap", sent=False)
                stats["bootstrap"] += 1
                continue
            if stats["sent"] >= max_alerts:
                if not stats.get("capped"):
                    log.warning("Limite de %d alertes atteinte pour ce passage : le reste attendra le suivant",
                                max_alerts)
                stats["capped"] = 1
                continue        # on continue la boucle pour enregistrer les températures

            log.info("Déclenché [%s] %s — %s", deal.source, deal.title[:90], " | ".join(triggers))
            verification = self.verifier.verify(deal)
            alert = Alert(deal=deal, verification=verification, triggers=triggers, velocity=velocity)
            if self.notifier.send(alert):
                self.db.record_alert(keys, deal_key=deal.key, source=deal.source, title=deal.title, url=deal.url,
                                     price_eur=deal.price_eur, verdict=verification.verdict)
                stats["sent"] += 1
            else:
                stats["failed"] += 1

        if bootstrap:
            self.db.mark_initialized()
            log.info("Premier passage : %d deal(s) déjà en ligne mémorisé(s) SANS alerte (silent_first_run). "
                     "Les alertes commencent au prochain passage.", stats["bootstrap"])
        self.db.purge()
        log.info("Passage terminé en %.0fs : %d deals lus, %d déclenchés, %d doublons, %d alerte(s) envoyée(s)%s. "
                 "HTTP: %d requêtes, %d bloquées (403).", time.time() - started, stats["deals"], stats["triggered"],
                 stats["duplicates"], stats["sent"], f", {stats['failed']} échec(s)" if stats["failed"] else "",
                 self.http.stats["requests"], self.http.stats["blocked"])
        return stats


# ------------------------------------------------------------------ mode --test

def simulated_alerts() -> list[Alert]:
    now = time.time()
    true_deal = Deal(
        source="dealabs", source_id="test-1", title="Sony WH-1000XM5 Casque Bluetooth à réduction de bruit — erreur de prix ?",
        url="https://www.dealabs.com/groupe/erreur-de-prix", site_country="FR", merchant="Amazon",
        merchant_url="https://www.amazon.fr/dp/B09Y2MYL5C", link_host="amazon.fr", price=99.0, original_price=399.99,
        temperature=412, category="High-tech", published_at=now - 900, price_error_context=True, asin="B09Y2MYL5C")
    true_deal.price_eur, true_deal.original_price_eur = 99.0, 399.99
    v1 = Verification(verdict=VERDICT_TRUE, median_90=279.0, low_30=259.0, max_90=329.0, history_source="idealo",
                      lowest_elsewhere=239.0, lowest_elsewhere_shop="Boulanger", real_discount_pct=64.5,
                      idealo_url="https://www.idealo.fr/prix/201949755/sony-wh-1000xm5.html",
                      notes=["ℹ️ Prix barré gonflé : prix barré 399,99 € > plus bas des 30 derniers jours (259,00 €)"])

    inflated = Deal(
        source="mydealz", source_id="test-2", title="DJI Mini 4K Drohne Fly More Combo",
        url="https://www.mydealz.de/", site_country="DE", merchant="Amazon", link_host="amazon.de",
        merchant_url="https://www.amazon.de/", price=189.0, original_price=699.0, temperature=96, category="Elektronik")
    inflated.price_eur, inflated.original_price_eur = 189.0, 699.0
    v2 = Verification(verdict=VERDICT_INFLATED, median_90=229.0, low_30=199.0, max_90=299.0, history_source="camel",
                      history_approx=True, lowest_elsewhere=205.0, lowest_elsewhere_shop="Fnac",
                      real_discount_pct=17.5, idealo_url="https://www.idealo.fr/",
                      notes=["prix barré 699,00 € > plus bas des 30 derniers jours (199,00 €)",
                             "prix barré jamais pratiqué sur 90 j (max 299,00 €)",
                             "réduction réelle 18 % seulement (affichée 73 %)",
                             "Vrai prix habituel ≈ 229,00 € (médiane 90 j)"])

    unverified = Deal(
        source="hotukdeals", source_id="test-3", title="Nike Dunk Low Retro — price error at £24.99",
        url="https://www.hotukdeals.com/", site_country="UK", merchant="Nike", price=24.99, original_price=109.99,
        currency="GBP", temperature=240, category="Fashion & Accessories")
    unverified.price_eur, unverified.original_price_eur = 29.24, 128.69
    v3 = Verification(verdict=VERDICT_UNVERIFIED, skipped_idealo_reason="mode/vêtements : comparaison Idealo non pertinente",
                      notes=["Aucun historique de prix fiable trouvé"])

    return [
        Alert(true_deal, v1, ["Mot-clé : « erreur de prix »", "Réduction affichée 75 % (≥ 70 %)",
                              "Vélocité 27.5 °/min (≥ 8)"], velocity=27.5),
        Alert(inflated, v2, ["Réduction affichée 73 % (≥ 70 %)"], velocity=None),
        Alert(unverified, v3, ["Mot-clé : « price error »", "Réduction affichée 77 % (≥ 70 %)"], velocity=12.0),
    ]


# ------------------------------------------------------------------ outils CLI

def velocity_report(db: StateDB, cfg: dict) -> None:
    vcfg = cfg.get("triggers", {}).get("velocity", {})
    samples = sorted(db.velocity_samples(float(vcfg.get("min_window_minutes", 4)),
                                         float(vcfg.get("max_window_minutes", 60))))
    print(f"\nDeals suivis avec au moins 2 mesures exploitables : {len(samples)}")
    if len(samples) < 30:
        print("Pas encore assez de données (il en faut ~30+). Laisse tourner le bot quelques heures puis relance.")
        return
    q = statistics.quantiles(samples, n=100)
    current = float(vcfg.get("threshold_deg_per_min", 5.0))
    print(f"  médiane : {statistics.median(samples):.2f} °/min")
    for p in (75, 90, 95, 98, 99):
        print(f"  {p}e centile : {q[p - 1]:.2f} °/min")
    print(f"  max : {samples[-1]:.2f} °/min")
    hits = sum(1 for s in samples if s >= current)
    print(f"\nSeuil actuel {current:g} °/min -> {hits} deal(s) sur {len(samples)} ({100 * hits / len(samples):.1f} %) "
          f"auraient déclenché.")
    print(f"Suggestion : {q[97]:.1f} °/min (≈ 2 % des deals, les plus explosifs) ; "
          f"{q[94]:.1f} °/min si tu veux plus d'alertes (≈ 5 %).")
    print("Modifie triggers.velocity.threshold_deg_per_min dans config.json.")


def print_chat_ids(secrets) -> int:
    if not secrets.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN manquant dans .env")
        return 1
    updates = TelegramNotifier(secrets.telegram_bot_token, "").get_updates()
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not chats:
        print("Aucun message trouvé. Envoie d'abord un message (ex. « bonjour ») à ton bot sur Telegram, puis relance.")
        return 1
    print("chat_id trouvés (copie la valeur dans TELEGRAM_CHAT_ID) :")
    for cid, name in chats.items():
        print(f"  {cid}    ({name})")
    return 0


# ------------------------------------------------------------------ entrée

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Veille erreurs de prix -> Telegram")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="un seul passage puis sortie")
    mode.add_argument("--loop", type=int, nargs="?", const=-1, metavar="SECONDES",
                      help="boucle (défaut : general.loop_interval_seconds)")
    mode.add_argument("--test", action="store_true", help="envoie 3 alertes simulées (✅ ⚠️ ❓)")
    mode.add_argument("--velocity-report", action="store_true", help="propose un seuil de vélocité")
    mode.add_argument("--get-chat-id", action="store_true", help="affiche les chat_id reçus par le bot")
    parser.add_argument("--dry-run", action="store_true", help="n'envoie rien sur Telegram (affiche en console)")
    parser.add_argument("--config", default=None, help="chemin de config.json")
    parser.add_argument("--log-file", action="store_true", help="écrit aussi les logs dans logs/bot.log")
    parser.add_argument("-v", "--verbose", action="store_true", help="logs détaillés (DEBUG)")
    args = parser.parse_args(argv)

    load_dotenv_if_present()
    cfg = load_config(args.config)
    general = cfg["general"]
    setup_logging("DEBUG" if args.verbose else general.get("log_level", "INFO"), args.log_file)
    secrets = load_secrets()

    if args.get_chat_id:
        return print_chat_ids(secrets)

    if args.dry_run:
        notifier = ConsoleNotifier()
    elif secrets.telegram_ready:
        notifier = TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id, cfg.get("telegram"))
    else:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants (fichier .env ou secrets GitHub). "
                  "Utilise --dry-run pour tester sans Telegram.")
        return 2

    if args.test:
        ok = all(notifier.send(a, test=True) for a in simulated_alerts())
        log.info("Mode test : %s", "3 alertes envoyées ✅" if ok else "échec d'envoi (voir erreurs ci-dessus)")
        return 0 if ok else 1

    db_path = Path(general.get("db_path", "state.db"))
    db = StateDB(db_path if db_path.is_absolute() else ROOT / db_path)
    try:
        if args.velocity_report:
            velocity_report(db, cfg)
            return 0
        bot = Bot(cfg, secrets, db, notifier)
        if args.loop is not None:
            interval = args.loop if args.loop and args.loop > 0 else int(general.get("loop_interval_seconds", 120))
            log.info("Boucle toutes les %ds (Ctrl+C pour arrêter)", interval)
            while True:
                try:
                    bot.run_once()
                except Exception:
                    log.exception("Erreur pendant le passage (le bot continue)")
                time.sleep(interval)
        bot.run_once()
        return 0
    except KeyboardInterrupt:
        log.info("Arrêt demandé (Ctrl+C)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
