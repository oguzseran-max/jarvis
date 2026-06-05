"""
JARVIS Performance & Bug Monitor — real-time observability for the voice loop.

Phase 0 of Marion's self-improvement program: make bugs VISIBLE. Every voice
turn is traced (per-stage latency + errors) into its own SQLite DB
(data/perf.db), and each finished turn is evaluated LIVE against latency and
quality heuristics. Anything slow, broken, or off-target is flagged the moment
it happens — logged, surfaced via /api/perf/* (the on-screen HUD), and pushed
as a macOS notification.

Design rules (same contract as self_eval.py):
  • NEVER blocks or breaks the voice loop — every public call is wrapped so a
    failure is logged and swallowed; the assistant keeps working if perf is down.
  • Adds no awaited latency: persistence + evaluation happen on cheap local work.
  • By design it does NOT make Marion speak about her own slowness (that would
    annoy, and the user already is). Awareness lives in the DB + HUD + notice;
    Phase 2 turns recurring flags into autonomous background fixes.

Storage: data/perf.db (its own SQLite DB — not shared with memory/self_eval).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.perf")

_DB = Path(__file__).resolve().parent / "data" / "perf.db"

# Latency budgets (ms) — override via env. "reason" = classify+action+LLM reply.
_SLOW_TOTAL_MS = int(os.getenv("PERF_SLOW_TOTAL_MS", "6000"))
_SLOW_REASON_MS = int(os.getenv("PERF_SLOW_REASON_MS", "4000"))
_SLOW_TTS_MS = int(os.getenv("PERF_SLOW_TTS_MS", "3500"))
_SLOW_STT_MS = int(os.getenv("PERF_SLOW_STT_MS", "3000"))

# Off-target / quality heuristics (ported from monitor.py, kept tiny + live).
_BAD_PATTERNS = [
    (r"\bas an ai\b", "broke the butler persona ('as an AI')"),
    (r"\bi (?:cannot|can't|am unable to) (?:help|assist|do)\b", "flat refusal"),
    (r"\bsamantha\b", "referenced 'Samantha' — Marion IS the assistant"),
    (r"\b(language model|i don'?t have access)\b", "leaked model/disclaimer text"),
]
# Signals that memory failed the user (they had to repeat themselves).
_MEMORY_MISS = [
    r"\b(?:remind me|tell me again|comme (?:je (?:t'?ai|te l'?ai) (?:dit|déjà dit)))",
    r"\b(?:i (?:don'?t|do not) (?:recall|remember)|je ne (?:m'?en souviens|sais) (?:pas|plus))",
    r"\b(?:you (?:didn'?t|never) (?:tell|told) me|tu ne m'?as (?:pas|jamais) dit)",
    r"\b(?:as i (?:said|told you)|je (?:te )?(?:l'?ai déjà|répète)|encore une fois)",
]
# User explicitly complaining → strongest off-target signal.
_USER_COMPLAINT = [
    r"\b(?:à côté de la plaque|n'?importe quoi|tu (?:m'?écoutes pas|comprends pas)|"
    r"c'?est pas (?:ça|ce que))",
    r"\b(?:that'?s (?:wrong|not what)|you'?re not listening|wrong answer|not what i (?:said|asked))",
    r"\b(?:trop lent|too slow|t'?es lente?)",
]


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB), timeout=5)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    """Create the perf DB. Safe to call repeatedly."""
    try:
        c = _conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                lang TEXT DEFAULT '',
                user_text TEXT DEFAULT '',
                reply_text TEXT DEFAULT '',
                total_ms INTEGER DEFAULT 0,
                stt_ms INTEGER DEFAULT 0,
                reason_ms INTEGER DEFAULT 0,
                tts_ms INTEGER DEFAULT 0,
                stages TEXT DEFAULT '{}',
                flags TEXT DEFAULT '[]',
                severity TEXT DEFAULT 'ok',     -- ok | warn | bad
                error TEXT DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts)")
        c.commit()
        c.close()
        log.info("perf monitor DB ready at %s", _DB)
    except Exception as e:
        log.warning("perf init failed: %s", e)


# ---------------------------------------------------------------------------
# Per-turn trace
# ---------------------------------------------------------------------------

class TurnTrace:
    """Times one voice turn. Create at turn start, mark() at each stage
    boundary, finish() once the reply is sent. All methods are crash-safe."""

    def __init__(self, lang: str = ""):
        self.lang = lang or ""
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.stages: dict[str, int] = {}
        self.flags: list[str] = []
        self.error: str = ""

    def mark(self, stage: str) -> None:
        try:
            now = time.perf_counter()
            self.stages[stage] = int((now - self._last) * 1000)
            self._last = now
        except Exception:
            pass

    def flag(self, msg: str) -> None:
        if msg and msg not in self.flags:
            self.flags.append(msg)

    def finish(self, user_text: str = "", reply_text: str = "",
               error: Optional[str] = None) -> None:
        try:
            total = int((time.perf_counter() - self._t0) * 1000)
            if error:
                self.error = str(error)[:500]
                self.flag(f"turn errored: {self.error[:120]}")
            self._evaluate(total, user_text or "", reply_text or "")
            loud = any(("errored" in f.lower() or "complaint" in f.lower()
                        or "off-target reply" in f.lower()) for f in self.flags)
            severity = "bad" if (self.error or loud) else ("warn" if self.flags else "ok")
            self._persist(total, user_text, reply_text, severity)
            if self.flags:
                self._notify(total, severity, user_text)
        except Exception as e:
            log.debug("perf finish failed: %s", e)

    # -- internals ----------------------------------------------------------

    def _evaluate(self, total: int, user_text: str, reply_text: str) -> None:
        st = self.stages
        if total > _SLOW_TOTAL_MS:
            self.flag(f"slow turn: {total}ms (budget {_SLOW_TOTAL_MS})")
        if st.get("reason", 0) > _SLOW_REASON_MS:
            self.flag(f"slow reasoning: {st['reason']}ms")
        if st.get("tts", 0) > _SLOW_TTS_MS:
            self.flag(f"slow TTS: {st['tts']}ms")
        if st.get("stt", 0) > _SLOW_STT_MS:
            self.flag(f"slow transcription: {st['stt']}ms")
        low_reply = (reply_text or "").lower()
        low_user = (user_text or "").lower()
        if reply_text and len(re.split(r"[.!?]+", reply_text.strip())) > 4:
            self.flag("reply too long for voice (>3 sentences)")
        for pat, why in _BAD_PATTERNS:
            if re.search(pat, low_reply):
                self.flag(f"off-target reply: {why}")
        for pat in _MEMORY_MISS:
            if re.search(pat, low_reply) or re.search(pat, low_user):
                self.flag("possible memory miss (user/Marion referenced repeating)")
                break
        for pat in _USER_COMPLAINT:
            if re.search(pat, low_user):
                self.flag("USER COMPLAINT — reply judged off-target by the user")
                break

    def _persist(self, total: int, user_text: str, reply_text: str, severity: str) -> None:
        try:
            c = _conn()
            c.execute(
                "INSERT INTO turns (ts, lang, user_text, reply_text, total_ms, "
                "stt_ms, reason_ms, tts_ms, stages, flags, severity, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), self.lang, (user_text or "")[:500], (reply_text or "")[:500],
                 total, self.stages.get("stt", 0), self.stages.get("reason", 0),
                 self.stages.get("tts", 0), json.dumps(self.stages),
                 json.dumps(self.flags), severity, self.error),
            )
            c.commit()
            c.close()
        except Exception as e:
            log.debug("perf persist failed: %s", e)

    def _notify(self, total: int, severity: str, user_text: str) -> None:
        head = "; ".join(self.flags)
        log.warning("[perf %s] %dms :: %s", severity, total, head)
        if severity == "bad":  # only surface the loud ones to the desktop
            try:
                d = head.replace('"', "'")[:200]
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{d}" with title "Marion — issue détecté"'],
                    capture_output=True, timeout=4,
                )
            except Exception:
                pass


def start(lang: str = "") -> TurnTrace:
    """Begin tracing a turn. Returns a TurnTrace (never raises)."""
    try:
        return TurnTrace(lang)
    except Exception:
        return TurnTrace("")


# ---------------------------------------------------------------------------
# Read-side: HUD / API
# ---------------------------------------------------------------------------

def stats(window_hours: int = 24) -> dict:
    """Aggregate telemetry for the on-screen HUD and /api/perf/stats."""
    out = {"turns": 0, "warn": 0, "bad": 0, "p50_ms": 0, "p95_ms": 0,
           "avg_stt_ms": 0, "avg_reason_ms": 0, "avg_tts_ms": 0, "recent": []}
    try:
        since = time.time() - window_hours * 3600
        c = _conn()
        rows = c.execute("SELECT total_ms, stt_ms, reason_ms, tts_ms, severity "
                         "FROM turns WHERE ts >= ?", (since,)).fetchall()
        if rows:
            tot = sorted(r["total_ms"] for r in rows)
            n = len(tot)
            out["turns"] = n
            out["warn"] = sum(1 for r in rows if r["severity"] == "warn")
            out["bad"] = sum(1 for r in rows if r["severity"] == "bad")
            out["p50_ms"] = tot[n // 2]
            out["p95_ms"] = tot[min(n - 1, int(n * 0.95))]
            out["avg_stt_ms"] = round(sum(r["stt_ms"] for r in rows) / n)
            out["avg_reason_ms"] = round(sum(r["reason_ms"] for r in rows) / n)
            out["avg_tts_ms"] = round(sum(r["tts_ms"] for r in rows) / n)
        out["recent"] = [
            {"ts": r["ts"], "lang": r["lang"], "total_ms": r["total_ms"],
             "severity": r["severity"], "flags": json.loads(r["flags"] or "[]"),
             "user_text": r["user_text"], "reply_text": r["reply_text"][:120],
             "stages": json.loads(r["stages"] or "{}")}
            for r in c.execute("SELECT * FROM turns ORDER BY id DESC LIMIT 15")
        ]
        c.close()
    except Exception as e:
        log.debug("perf stats failed: %s", e)
    return out


def recent_flags(window_hours: int = 24, limit: int = 50) -> list[dict]:
    """Flagged turns only — the raw material Phase 2's auto-fix loop consumes."""
    out: list[dict] = []
    try:
        since = time.time() - window_hours * 3600
        c = _conn()
        for r in c.execute("SELECT ts, lang, total_ms, severity, flags, user_text, "
                           "reply_text, error FROM turns WHERE ts >= ? AND severity != 'ok' "
                           "ORDER BY id DESC LIMIT ?", (since, limit)):
            out.append({"ts": r["ts"], "lang": r["lang"], "total_ms": r["total_ms"],
                        "severity": r["severity"], "flags": json.loads(r["flags"] or "[]"),
                        "user_text": r["user_text"], "reply_text": r["reply_text"],
                        "error": r["error"]})
        c.close()
    except Exception as e:
        log.debug("perf recent_flags failed: %s", e)
    return out
