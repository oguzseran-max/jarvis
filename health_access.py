"""
JARVIS Health Access — near-real-time heart-rate monitoring + spoken alerts.

This is "Stage 2" of the health feature: a dependency-light bridge that ingests
heart-rate readings pushed from an iOS app (e.g. *Health Auto Export*, which
reads Apple Watch data from HealthKit and POSTs it on a schedule, ~1 reading/min)
and decides whether Marion should say something out loud.

⚠️  NOT a medical device. The validated safety net is the Apple Watch's own
"High/Low Heart Rate" and "Irregular Rhythm" notifications (Watch app → Heart) —
enable those. This layer only gives Marion a *voice* on top of them at home, and
the export path is delayed by ~1-2 minutes, so it catches *sustained* tachycardia,
not a single instantaneous beat. For true beat-by-beat streaming you need the
custom watchOS app ("Stage 3"), which POSTs to this same endpoint.

Configuration (see .env.example):
  HEALTH_WEBHOOK_TOKEN   — shared secret; the iOS app must include ?token=… (recommended)
  HEALTH_HR_MAX          — alert above this many bpm (default 120)
  HEALTH_HR_MIN          — alert below this many bpm (default 40)
  HEALTH_ALERT_COOLDOWN  — seconds between repeat alerts of the same kind (default 600)
  HEALTH_ALERT_LANG      — voice language for alerts: en | fr | tr (default en)

Run `python -m health_access` for a status check; feed it a sample payload on
stdin to dry-run the parser/threshold logic without the server.
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.health")

DB_PATH = Path(__file__).parent / "data" / "health.db"


def _load_env_file() -> None:
    """Load HEALTH_* (and any) vars from a sibling .env if present.

    Dependency-free and non-destructive — mirrors spotify_access so the module
    works under `python -m health_access` without the server having loaded .env.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_env_file()


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "").strip()))
    except (ValueError, TypeError):
        return default


WEBHOOK_TOKEN = os.getenv("HEALTH_WEBHOOK_TOKEN", "").strip()
HR_MAX = _int_env("HEALTH_HR_MAX", 120)
HR_MIN = _int_env("HEALTH_HR_MIN", 40)
ALERT_COOLDOWN = _int_env("HEALTH_ALERT_COOLDOWN", 600)
ALERT_LANG = (os.getenv("HEALTH_ALERT_LANG", "en").strip().lower() or "en")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    ts: float           # unix seconds
    bpm: int
    source: str         # e.g. "heart_rate", "resting_heart_rate"


@dataclass
class Alert:
    kind: str           # "high" | "low"
    bpm: int
    text_by_lang: dict  # {"en": "...", "fr": "...", "tr": "..."}

    def text(self, lang: str | None = None) -> str:
        lang = (lang or ALERT_LANG)
        return self.text_by_lang.get(lang, self.text_by_lang["en"])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS readings (
               ts REAL NOT NULL,
               bpm INTEGER NOT NULL,
               source TEXT NOT NULL DEFAULT 'heart_rate'
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
    # One row per alert-kind tracking when we last spoke it (cooldown state).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alert_state (
               kind TEXT PRIMARY KEY,
               last_ts REAL NOT NULL
           )"""
    )
    return conn


def _store_readings(readings: list[Reading]) -> None:
    if not readings:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO readings (ts, bpm, source) VALUES (?, ?, ?)",
            [(r.ts, r.bpm, r.source) for r in readings],
        )
        conn.commit()
    finally:
        conn.close()


def _last_alert_ts(conn: sqlite3.Connection, kind: str) -> float:
    row = conn.execute("SELECT last_ts FROM alert_state WHERE kind = ?", (kind,)).fetchone()
    return float(row[0]) if row else 0.0


def _mark_alerted(conn: sqlite3.Connection, kind: str, ts: float) -> None:
    conn.execute(
        "INSERT INTO alert_state (kind, last_ts) VALUES (?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET last_ts = excluded.last_ts",
        (kind, ts),
    )
    conn.commit()


def latest_status() -> dict:
    """Most recent reading + config, for diagnostics and a 'how's my heart' query."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT ts, bpm, source FROM readings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    finally:
        conn.close()
    latest = None
    if row:
        latest = {"ts": row[0], "bpm": row[1], "source": row[2],
                  "age_seconds": max(0.0, time.time() - row[0])}
    return {
        "configured": is_configured(),
        "thresholds": {"high": HR_MAX, "low": HR_MIN},
        "cooldown_seconds": ALERT_COOLDOWN,
        "alert_lang": ALERT_LANG,
        "total_readings": count,
        "latest": latest,
    }


def is_configured() -> bool:
    """True once a webhook token is set (the only thing the iOS side strictly needs)."""
    return bool(WEBHOOK_TOKEN)


def token_ok(provided: str | None) -> bool:
    """Validate the shared secret. If no token is configured, accept (local-only)."""
    if not WEBHOOK_TOKEN:
        return True
    return bool(provided) and provided == WEBHOOK_TOKEN


# ---------------------------------------------------------------------------
# Payload parsing — tolerant of two shapes
# ---------------------------------------------------------------------------

# Sources we treat as live heart rate. Resting/walking averages are recorded but
# never trigger the "too fast" alert (they're not real-time spikes).
_LIVE_HR_NAMES = {"heart_rate", "heartrate", "hr"}
_RECORDED_HR_NAMES = _LIVE_HR_NAMES | {"resting_heart_rate", "walking_heart_rate_average"}


def _parse_date(raw) -> float:
    """Best-effort parse of a Health Auto Export date string to unix seconds."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return time.time()
    s = raw.strip()
    # Common Health Auto Export format: "2024-06-05 14:23:00 +0000"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return time.time()


def _coerce_bpm(point: dict) -> Optional[int]:
    """Pull a representative bpm from a data point (qty, Avg, value, or Max)."""
    for key in ("qty", "Avg", "avg", "value", "bpm", "Max", "max"):
        if key in point and point[key] is not None:
            try:
                bpm = int(round(float(point[key])))
                if 20 <= bpm <= 300:  # sanity window
                    return bpm
            except (ValueError, TypeError):
                continue
    return None


def parse_readings(payload: dict) -> list[Reading]:
    """Extract heart-rate readings from either supported payload shape.

    Shape A — Health Auto Export REST:
        {"data": {"metrics": [{"name": "heart_rate", "data": [{"date":..,"Avg":72}]}]}}
    Shape B — simple (future watchOS app):
        {"bpm": 132, "date": "...", "source": "heart_rate"}  (or a list of these)
    """
    readings: list[Reading] = []

    # Shape A
    metrics = (payload.get("data") or {}).get("metrics") if isinstance(payload.get("data"), dict) else None
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            name = str(metric.get("name", "")).lower()
            if name not in _RECORDED_HR_NAMES:
                continue
            for point in metric.get("data", []) or []:
                if not isinstance(point, dict):
                    continue
                bpm = _coerce_bpm(point)
                if bpm is None:
                    continue
                readings.append(Reading(ts=_parse_date(point.get("date")), bpm=bpm, source=name))
        return readings

    # Shape B (single object or list)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        bpm = _coerce_bpm(item)
        if bpm is None:
            continue
        source = str(item.get("source", "heart_rate")).lower()
        readings.append(Reading(ts=_parse_date(item.get("date") or item.get("ts")), bpm=bpm, source=source))
    return readings


# ---------------------------------------------------------------------------
# Threshold evaluation
# ---------------------------------------------------------------------------

def _alert_text(kind: str, bpm: int) -> dict:
    if kind == "high":
        return {
            "en": f"Sir, your heart rate is rather high — {bpm} beats per minute. Do take a moment.",
            "fr": f"Monsieur, votre rythme cardiaque est élevé — {bpm} battements par minute. Prenez un instant.",
            "tr": f"Efendim, kalp atış hızınız oldukça yüksek — dakikada {bpm}. Lütfen bir an durun.",
        }
    return {
        "en": f"Sir, your heart rate is unusually low — {bpm} beats per minute.",
        "fr": f"Monsieur, votre rythme cardiaque est anormalement bas — {bpm} battements par minute.",
        "tr": f"Efendim, kalp atış hızınız alışılmadık derecede düşük — dakikada {bpm}.",
    }


def evaluate(readings: list[Reading]) -> Optional[Alert]:
    """Return an Alert if a LIVE heart-rate reading breaches a threshold and we
    are past the cooldown for that alert kind; otherwise None.

    Only live `heart_rate` readings can fire — resting/walking averages are
    recorded for context but never alerted on. The most extreme breaching
    reading in the batch wins.
    """
    live = [r for r in readings if r.source in _LIVE_HR_NAMES]
    if not live:
        return None

    high = max((r for r in live if r.bpm > HR_MAX), key=lambda r: r.bpm, default=None)
    low = min((r for r in live if r.bpm < HR_MIN), key=lambda r: r.bpm, default=None)

    conn = _connect()
    try:
        now = time.time()
        # High takes priority (the user's stated concern: "battre trop vite").
        for kind, hit in (("high", high), ("low", low)):
            if hit is None:
                continue
            if now - _last_alert_ts(conn, kind) < ALERT_COOLDOWN:
                log.info(f"Health {kind} alert suppressed (cooldown), bpm={hit.bpm}")
                continue
            _mark_alerted(conn, kind, now)
            return Alert(kind=kind, bpm=hit.bpm, text_by_lang=_alert_text(kind, hit.bpm))
    finally:
        conn.close()
    return None


def ingest(payload: dict) -> dict:
    """Parse → store → evaluate. Returns a summary dict (the endpoint serializes it)."""
    readings = parse_readings(payload)
    _store_readings(readings)
    alert = evaluate(readings)
    return {
        "received": len(readings),
        "live": sum(1 for r in readings if r.source in _LIVE_HR_NAMES),
        "alert": (
            {"kind": alert.kind, "bpm": alert.bpm, "text": alert.text()} if alert else None
        ),
        "_alert_obj": alert,  # consumed by the server, stripped before JSON response
    }


# ---------------------------------------------------------------------------
# Diagnostic CLI — `python -m health_access [< payload.json]`
# ---------------------------------------------------------------------------

def _diagnose() -> int:
    import sys

    print("JARVIS · Health — local check\n")
    print("Configuration:")
    print(f"  webhook token : {'set' if WEBHOOK_TOKEN else 'NOT set (endpoint open on localhost)'}")
    print(f"  high / low    : {HR_MAX} / {HR_MIN} bpm")
    print(f"  cooldown      : {ALERT_COOLDOWN}s")
    print(f"  alert lang    : {ALERT_LANG}\n")

    # If a JSON payload is piped in, dry-run the parser + thresholds.
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"Could not parse stdin as JSON: {e}")
                return 1
            readings = parse_readings(payload)
            print(f"Parsed {len(readings)} reading(s):")
            for r in readings[:10]:
                when = datetime.fromtimestamp(r.ts).strftime("%H:%M:%S")
                print(f"  - {when}  {r.bpm} bpm  [{r.source}]")
            alert = evaluate(readings)
            print("\n" + (f"⚠️  ALERT ({alert.kind}): {alert.text()}" if alert else "No alert triggered."))
            return 0

    status = latest_status()
    latest = status["latest"]
    if latest:
        age = int(latest["age_seconds"])
        print(f"Latest reading : {latest['bpm']} bpm  ({age}s ago, {latest['source']})")
    else:
        print("Latest reading : (none yet — point the iOS exporter at /api/health/ingest)")
    print(f"Total readings : {status['total_readings']}")
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(_diagnose())
