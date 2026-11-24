"""Persistance SQLite (`state.db`) : dédoublonnage, températures, cache Idealo, historique de prix local."""
from __future__ import annotations

import json
import sqlite3
import statistics
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
"""

DAY = 86400.0


class StateDB:
    def __init__(self, path: str | Path = "state.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
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

    def source_due(self, name: str, min_interval_minutes: float, now: Optional[float] = None) -> bool:
        if not min_interval_minutes:
            return True
        now = now or time.time()
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
        self.conn.commit()
