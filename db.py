"""Persistance SQLite (`state.db`) : dédoublonnage, températures, caches (Idealo, marques, prix officiels,
comparateurs), historique de prix local, pauses de domaines. Utilisable depuis plusieurs threads."""
from __future__ import annotations

import functools
import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    deal_key TEXT NOT NULL,
    source TEXT,
    title TEXT,
    url TEXT,
    price_eur REAL,
    verdict TEXT,
    sent INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS alert_keys (
    key TEXT PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES alerts(id)
);
CREATE TABLE IF NOT EXISTS temperature_history (
    deal_key TEXT NOT NULL,
    ts REAL NOT NULL,
    temperature REAL NOT NULL,
    published_at REAL
);
CREATE INDEX IF NOT EXISTS idx_temp_key_ts ON temperature_history(deal_key, ts);
CREATE TABLE IF NOT EXISTS idealo_cache (
    cache_key TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_observations (
    product_key TEXT NOT NULL,
    ts REAL NOT NULL,
    price REAL NOT NULL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_key_ts ON price_observations(product_key, ts);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS brand_cache (
    brand_key TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    official_domain TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS kv_cache (
    namespace TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (namespace, cache_key)
);
CREATE TABLE IF NOT EXISTS domain_status (
    host TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0
);
"""

DAY = 86400.0


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _synchronized(cls):
    """Protège toutes les méthodes publiques par un verrou (sources lues en parallèle)."""
    for name, attr in list(vars(cls).items()):
        if callable(attr) and not name.startswith("_"):
            setattr(cls, name, _locked(attr))
    return cls


@_synchronized
class StateDB:
    def __init__(self, path: str | Path = "state.db"):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ caches génériques
    def kv_get(self, namespace: str, key: str, max_age_hours: float) -> Optional[Any]:
        row = self.conn.execute("SELECT ts, payload FROM kv_cache WHERE namespace=? AND cache_key=?",
                                (namespace, key)).fetchone()
        if not row or time.time() - row["ts"] > max_age_hours * 3600:
            return None
        return json.loads(row["payload"])

    def kv_put(self, namespace: str, key: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv_cache(namespace, cache_key, ts, payload) VALUES(?,?,?,?) "
            "ON CONFLICT(namespace, cache_key) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
            (namespace, key, time.time(), json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    # ------------------------------------------------------------ marques (cache 30 jours)
    def brand_get(self, brand_key: str, max_age_days: float = 30) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM brand_cache WHERE brand_key=?", (brand_key,)).fetchone()
        if not row or time.time() - row["ts"] > max_age_days * DAY:
            return None
        data = json.loads(row["payload"] or "{}")
        data.update({"status": row["status"], "official_domain": row["official_domain"] or ""})
        return data

    def brand_put(self, brand_key: str, status: str, official_domain: str = "", payload: Optional[dict] = None) -> None:
        self.conn.execute(
            "INSERT INTO brand_cache(brand_key, ts, status, official_domain, payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(brand_key) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
            "official_domain=excluded.official_domain, payload=excluded.payload",
            (brand_key, time.time(), status, official_domain, json.dumps(payload or {}, ensure_ascii=False)))
        self.conn.commit()

    # ------------------------------------------------------------ pause automatique par domaine
    def domain_cooldown_left(self, host: str, now: Optional[float] = None) -> float:
        """Minutes de pause restantes pour ce domaine (0 = utilisable)."""
        row = self.conn.execute("SELECT cooldown_until FROM domain_status WHERE host=?", (host,)).fetchone()
        left = (row["cooldown_until"] - (now or time.time())) if row else 0
        return max(0.0, left / 60)

    def domain_failure(self, host: str, threshold: int, minutes: float, now: Optional[float] = None) -> bool:
        """Compte un 403/429 ; au-delà de `threshold` échecs consécutifs, pause le domaine. Renvoie True si en pause."""
        now = now or time.time()
        row = self.conn.execute("SELECT failures FROM domain_status WHERE host=?", (host,)).fetchone()
        failures = (row["failures"] if row else 0) + 1
        until = now + minutes * 60 if failures >= threshold else 0
        self.conn.execute(
            "INSERT INTO domain_status(host, failures, cooldown_until) VALUES(?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET failures=excluded.failures, cooldown_until=excluded.cooldown_until",
            (host, 0 if until else failures, until))
        self.conn.commit()
        return bool(until)

    def domain_success(self, host: str) -> None:
        self.conn.execute("UPDATE domain_status SET failures=0 WHERE host=? AND failures>0", (host,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, str(value)))
        self.conn.commit()

    def is_initialized(self) -> bool:
        return self.get_meta("initialized") == "1"

    def mark_initialized(self) -> None:
        self.set_meta("initialized", "1")

    def pause_source(self, name: str, minutes: float, now: Optional[float] = None) -> None:
        self.set_meta(f"paused_until:{name}", (now or time.time()) + minutes * 60)

    def paused_for(self, name: str, now: Optional[float] = None) -> float:
        """Minutes de pause restantes (0 si la source n'est pas en pause)."""
        until = self.get_meta(f"paused_until:{name}")
        left = float(until) - (now or time.time()) if until else 0
        return max(0.0, left / 60)

    def is_source_initialized(self, name: str) -> bool:
        return self.get_meta(f"initialized:{name}") == "1"

    def mark_source_initialized(self, name: str) -> None:
        self.set_meta(f"initialized:{name}", "1")

    def source_due(self, name: str, min_interval_minutes: float, now: Optional[float] = None) -> bool:
        now = now or time.time()
        if self.paused_for(name, now) > 0:
            return False
        if not min_interval_minutes:
            return True
        last = self.get_meta(f"last_run:{name}")
        return last is None or now - float(last) >= min_interval_minutes * 60

    def mark_source_run(self, name: str, now: Optional[float] = None) -> None:
        self.set_meta(f"last_run:{name}", now or time.time())

    # ------------------------------------------------------------ dédoublonnage
    def find_duplicate(self, keys: Iterable[str]) -> Optional[sqlite3.Row]:
        keys = [k for k in keys if k]
        if not keys:
            return None
        marks = ",".join("?" * len(keys))
        return self.conn.execute(
            f"SELECT a.* FROM alert_keys k JOIN alerts a ON a.id = k.alert_id WHERE k.key IN ({marks}) LIMIT 1",
            keys,
        ).fetchone()

    def record_alert(self, keys: Iterable[str], *, deal_key: str, source: str, title: str, url: str,
                     price_eur: Optional[float], verdict: str, sent: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts(created_at, deal_key, source, title, url, price_eur, verdict, sent) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), deal_key, source, title, url, price_eur, verdict, 1 if sent else 0),
        )
        alert_id = int(cur.lastrowid)
        self.conn.executemany("INSERT OR IGNORE INTO alert_keys(key, alert_id) VALUES(?, ?)",
                              [(k, alert_id) for k in keys if k])
        self.conn.commit()
        return alert_id

    # ------------------------------------------------------------ températures
    def add_temperature(self, deal_key: str, temperature: float, ts: Optional[float] = None,
                        published_at: Optional[float] = None) -> None:
        ts = ts or time.time()
        last = self.conn.execute(
            "SELECT ts, temperature FROM temperature_history WHERE deal_key=? ORDER BY ts DESC LIMIT 1", (deal_key,)
        ).fetchone()
        # évite les doublons quand une même source est lue plusieurs fois dans la même minute
        if last and ts - last["ts"] < 30 and abs(last["temperature"] - temperature) < 0.01:
            return
        self.conn.execute("INSERT INTO temperature_history(deal_key, ts, temperature, published_at) VALUES(?,?,?,?)",
                          (deal_key, ts, float(temperature), published_at))
        self.conn.commit()

    def temperature_points(self, deal_key: str) -> list[tuple[float, float]]:
        rows = self.conn.execute(
            "SELECT ts, temperature FROM temperature_history WHERE deal_key=? ORDER BY ts", (deal_key,)
        ).fetchall()
        return [(r["ts"], r["temperature"]) for r in rows]

    def velocity(self, deal_key: str, *, now: Optional[float] = None, min_window_min: float = 4,
                 max_window_min: float = 60, published_at: Optional[float] = None,
                 current_temp: Optional[float] = None) -> Optional[float]:
        """Montée en °/min.

        1) Avec l'historique : pente entre le dernier point et le plus ancien point situé dans
           [max_window ; min_window] minutes avant.
        2) Sinon, si la date de publication est connue : température / minutes depuis la publication
           (un deal démarre à 0°).
        """
        pts = self.temperature_points(deal_key)
        now = now or (pts[-1][0] if pts else time.time())
        if pts:
            t_last, temp_last = pts[-1]
            if current_temp is not None:
                temp_last = current_temp
            for ts, temp in pts:
                age_min = (t_last - ts) / 60
                if min_window_min <= age_min <= max_window_min:
                    return round((temp_last - temp) / age_min, 2)
        if published_at and current_temp is not None:
            age_min = (now - published_at) / 60
            if min_window_min <= age_min <= max_window_min:
                return round(current_temp / age_min, 2)
        return None

    def velocity_samples(self, min_window_min: float = 4, max_window_min: float = 60) -> list[float]:
        """Vitesse max observée pour chaque deal suivi (pour proposer un seuil)."""
        keys = [r["deal_key"] for r in self.conn.execute("SELECT DISTINCT deal_key FROM temperature_history")]
        samples: list[float] = []
        for key in keys:
            pts = self.temperature_points(key)
            best: Optional[float] = None
            for i, (t1, temp1) in enumerate(pts):
                for t0, temp0 in pts[:i]:
                    age = (t1 - t0) / 60
                    if min_window_min <= age <= max_window_min:
                        v = (temp1 - temp0) / age
                        best = v if best is None else max(best, v)
            if best is not None:
                samples.append(round(best, 2))
        return samples

    # ------------------------------------------------------------ cache Idealo
    def cache_get(self, cache_key: str, max_age_hours: float) -> Optional[Any]:
        row = self.conn.execute("SELECT ts, payload FROM idealo_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if not row or time.time() - row["ts"] > max_age_hours * 3600:
            return None
        return json.loads(row["payload"])

    def cache_put(self, cache_key: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO idealo_cache(cache_key, ts, payload) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
            (cache_key, time.time(), json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    # ------------------------------------------------------------ historique de prix local
    def add_price_observation(self, product_key: str, price: float, source: str, ts: Optional[float] = None) -> None:
        if not product_key or price is None:
            return
        ts = ts or time.time()
        last = self.conn.execute(
            "SELECT ts, price FROM price_observations WHERE product_key=? ORDER BY ts DESC LIMIT 1", (product_key,)
        ).fetchone()
        if last and ts - last["ts"] < 3600 and abs(last["price"] - price) < 0.01:
            return
        self.conn.execute("INSERT INTO price_observations(product_key, ts, price, source) VALUES(?,?,?,?)",
                          (product_key, ts, float(price), source))
        self.conn.commit()

    def price_stats(self, product_key: str, now: Optional[float] = None) -> dict[str, Any]:
        now = now or time.time()
        rows = self.conn.execute(
            "SELECT ts, price FROM price_observations WHERE product_key=? AND ts>=? ORDER BY ts",
            (product_key, now - 90 * DAY),
        ).fetchall()
        prices_90 = [r["price"] for r in rows]
        prices_30 = [r["price"] for r in rows if r["ts"] >= now - 30 * DAY]
        return {
            "points": len(prices_90),
            "median_90": statistics.median(prices_90) if prices_90 else None,
            "max_90": max(prices_90) if prices_90 else None,
            "low_30": min(prices_30) if prices_30 else None,
        }

    # ------------------------------------------------------------ entretien
    def purge(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.conn.execute("DELETE FROM temperature_history WHERE ts < ?", (now - 7 * DAY,))
        self.conn.execute("DELETE FROM idealo_cache WHERE ts < ?", (now - 3 * DAY,))
        self.conn.execute("DELETE FROM price_observations WHERE ts < ?", (now - 120 * DAY,))
        self.conn.execute("DELETE FROM kv_cache WHERE ts < ?", (now - 7 * DAY,))
        self.conn.execute("DELETE FROM brand_cache WHERE ts < ?", (now - 31 * DAY,))
        self.conn.commit()
