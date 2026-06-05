"""
JARVIS Self-Evaluation — Marion learns from her own conversations.

Phase 1 of the continuous-improvement loop. After every voice turn a background
"critic" (Haiku) scores the exchange and the module AUTO-APPLIES small, safe
adjustments, then reports them to the user once a day. Nothing here ever blocks
or breaks the voice loop: every public call is wrapped so a failure is logged
and swallowed (the assistant keeps working even if self-eval is down).

What it auto-applies (all bounded / reversible):
  • Vocabulary  — proper nouns the user says are added to the Whisper decoder
                  primer (written to data/whisper_vocab_<lang>.txt, which the
                  whisper service merges in live) so they stop being mis-heard.
  • Preferences — stable, explicit preferences ("answer shorter", "call me X")
                  are stored and re-injected into Marion's system prompt.
  • Brevity     — if replies are consistently judged too long, the reply token
                  cap is nudged down within a safe [90, 160] band.

Everything applied today is collected and surfaced as a one-line spoken digest
the first time the user talks to Marion on a new day (`pop_due_digest`).

Storage: data/self_eval.db (its own SQLite DB — not shared with memory/tracking).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.self_eval")

_REPO = Path(__file__).resolve().parent
_DATA = _REPO / "data"
_DB = _DATA / "self_eval.db"

# Reply-length auto-tuning band (matches server.py's default of 140).
_TOKENS_DEFAULT = 140
_TOKENS_MIN = 90
_TOKENS_MAX = 160

# How many recent turns the brevity auto-tune looks at.
_BREVITY_WINDOW = 8

_ALLOWED_LANGS = ("fr", "tr", "en")

# Fast, local "the user is correcting / frustrated" detector. These are strong
# signals that the PREVIOUS turn went wrong — logged for the daily digest even
# if the critic is unavailable.
_CORRECTION_PATTERNS = {
    "fr": (
        "à côté de la plaque", "a cote de la plaque", "tu comprends mal",
        "c'est pas ça", "c'est pas ce que", "non je voulais dire", "je voulais dire",
        "non pas ça", "t'as pas compris", "tu n'as pas compris", "répète", "repete",
        "j'ai pas dit", "je n'ai pas dit", "n'importe quoi", "ça n'a rien à voir",
    ),
    "tr": ("yanlış anladın", "onu demek istemedim", "öyle demedim", "tekrar et"),
    "en": (
        "that's not what", "you misunderstood", "i didn't say", "not what i meant",
        "wrong", "say again", "repeat that", "no i meant",
    ),
}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    _DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    """Create tables. Safe to call on every startup."""
    try:
        with _conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, lang TEXT, user_text TEXT, reply_text TEXT,
                    latency_ms INTEGER, score INTEGER, too_long INTEGER,
                    note TEXT, correction INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS vocab (
                    lang TEXT, word TEXT, count INTEGER DEFAULT 1,
                    first_ts REAL, UNIQUE(lang, word)
                );
                CREATE TABLE IF NOT EXISTS prefs (
                    lang TEXT, text TEXT, count INTEGER DEFAULT 1,
                    ts REAL, UNIQUE(lang, text)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT, ts REAL, kind TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
        # Seed the vocab files so the whisper service has something to read.
        for lang in ("fr", "tr"):
            _rewrite_vocab_file(lang)
        log.info("self_eval ready (db=%s)", _DB)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("self_eval init failed: %s", e)


def _meta_get(key: str) -> Optional[str]:
    try:
        with _conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
    except Exception:
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reads consumed by the rest of JARVIS (never raise)
# ---------------------------------------------------------------------------

def get_max_tokens() -> int:
    """Current reply-length cap (auto-tuned). server.py reads this."""
    try:
        v = _meta_get("max_tokens")
        n = int(v) if v else _TOKENS_DEFAULT
        return max(_TOKENS_MIN, min(_TOKENS_MAX, n))
    except Exception:
        return _TOKENS_DEFAULT


def get_preferences_text(lang: str) -> str:
    """Learned preferences to append to Marion's system prompt (empty if none)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT text FROM prefs WHERE lang=? ORDER BY count DESC, ts DESC LIMIT 8",
                (lang,),
            ).fetchall()
        items = [r["text"] for r in rows if r["text"]]
        if not items:
            return ""
        bullets = "\n".join(f"- {t}" for t in items)
        return ("\n\nLEARNED PREFERENCES (things this user has asked for — honour them):\n"
                + bullets)
    except Exception:
        return ""


def get_vocab(lang: str) -> list[str]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT word FROM vocab WHERE lang=? ORDER BY count DESC, first_ts DESC LIMIT 60",
                (lang,),
            ).fetchall()
        return [r["word"] for r in rows if r["word"]]
    except Exception:
        return []


def _rewrite_vocab_file(lang: str) -> None:
    """Persist the learned vocab to data/whisper_vocab_<lang>.txt (read by the
    whisper service to extend its decoder primer live, no restart needed)."""
    try:
        words = get_vocab(lang)
        path = _DATA / f"whisper_vocab_{lang}.txt"
        _DATA.mkdir(parents=True, exist_ok=True)
        path.write_text(", ".join(words))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Writes (auto-applied adjustments) + change log for the daily digest
# ---------------------------------------------------------------------------

def _log_change(kind: str, detail: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO changes(day, ts, kind, detail) VALUES(?,?,?,?)",
                (date.today().isoformat(), time.time(), kind, detail),
            )
    except Exception:
        pass


def _add_vocab(lang: str, word: str) -> bool:
    word = (word or "").strip().strip(".,!?;:\"'").strip()
    if not word or len(word) < 2 or len(word) > 40:
        return False
    if lang not in _ALLOWED_LANGS:
        return False
    try:
        with _conn() as c:
            cur = c.execute("SELECT count FROM vocab WHERE lang=? AND word=?", (lang, word))
            row = cur.fetchone()
            if row:
                c.execute("UPDATE vocab SET count=count+1 WHERE lang=? AND word=?", (lang, word))
                return False  # already known — not a new change
            c.execute(
                "INSERT INTO vocab(lang, word, count, first_ts) VALUES(?,?,1,?)",
                (lang, word, time.time()),
            )
        _rewrite_vocab_file(lang)
        return True
    except Exception:
        return False


def _add_pref(lang: str, text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) < 4 or len(text) > 120:
        return False
    try:
        with _conn() as c:
            row = c.execute("SELECT count FROM prefs WHERE lang=? AND text=?", (lang, text)).fetchone()
            if row:
                c.execute("UPDATE prefs SET count=count+1 WHERE lang=? AND text=?", (lang, text))
                return False
            c.execute(
                "INSERT INTO prefs(lang, text, count, ts) VALUES(?,?,1,?)",
                (lang, text, time.time()),
            )
        return True
    except Exception:
        return False


def _maybe_tune_brevity() -> None:
    """If recent replies are mostly judged too long, nudge the cap down."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT too_long FROM turns WHERE too_long IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (_BREVITY_WINDOW,),
            ).fetchall()
        if len(rows) < _BREVITY_WINDOW:
            return
        long_ratio = sum(r["too_long"] for r in rows) / len(rows)
        cur = get_max_tokens()
        if long_ratio >= 0.6 and cur > _TOKENS_MIN:
            new = max(_TOKENS_MIN, cur - 15)
            if new != cur:
                _meta_set("max_tokens", str(new))
                _log_change("brevity", f"réponses raccourcies ({cur}→{new} jetons)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Correction / frustration detection (fast, local, synchronous)
# ---------------------------------------------------------------------------

def looks_like_correction(text: str, lang: str) -> bool:
    t = (text or "").lower()
    for p in _CORRECTION_PATTERNS.get(lang, ()):  # type: ignore[arg-type]
        if p in t:
            return True
    return False


# ---------------------------------------------------------------------------
# Per-turn recording + background critic
# ---------------------------------------------------------------------------

_CRITIC_SYSTEM = (
    "You are a terse QA critic for a voice assistant named Marion. You are given "
    "the PREVIOUS turn and the CURRENT turn of a spoken conversation. Judge ONLY "
    "the CURRENT assistant reply and extract durable signals. Reply with STRICT "
    "JSON, no prose:\n"
    '{"score": 1-5, "too_long": true/false, '
    '"vocab": [proper nouns / names / places / song or artist titles the USER said '
    'that a speech-recogniser might mis-hear — verbatim, max 4, [] if none], '
    '"preference": "a STABLE explicit preference the user expressed about how Marion '
    'should behave (e.g. answer shorter, call me X), else empty string", '
    '"note": "<=8 word issue summary, empty if fine"}\n'
    "Be conservative: empty arrays/strings unless clearly warranted. A reply is "
    "too_long if it exceeds ~2 sentences for a casual exchange."
)


async def record_turn(client, *, user_text: str, reply_text: str, lang: str,
                      latency_ms: int = 0, prev_user: str = "", prev_reply: str = "") -> None:
    """Store a turn and kick off the background critic. Never blocks the reply."""
    if lang not in _ALLOWED_LANGS:
        lang = "en"
    correction = 1 if looks_like_correction(user_text, lang) else 0
    turn_id = None
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO turns(ts, lang, user_text, reply_text, latency_ms, correction) "
                "VALUES(?,?,?,?,?,?)",
                (time.time(), lang, user_text, reply_text, latency_ms, correction),
            )
            turn_id = cur.lastrowid
        if correction:
            _log_change("frustration", f"correction détectée : « {user_text[:60]} »")
    except Exception as e:
        log.debug("record_turn store failed: %s", e)

    # Background evaluation — fire and forget so it adds ZERO latency.
    try:
        asyncio.create_task(_evaluate(client, turn_id, lang, user_text, reply_text,
                                      prev_user, prev_reply))
    except Exception:
        pass


async def _evaluate(client, turn_id, lang, user_text, reply_text, prev_user, prev_reply) -> None:
    try:
        payload = (
            f"PREVIOUS user: {prev_user}\nPREVIOUS Marion: {prev_reply}\n\n"
            f"CURRENT user: {user_text}\nCURRENT Marion: {reply_text}\n\nlang={lang}"
        )
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=_CRITIC_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        raw = resp.content[0].text if resp and resp.content else ""
        data = _parse_json(raw)
        if not data:
            return

        score = _as_int(data.get("score"))
        too_long = 1 if data.get("too_long") else 0
        note = str(data.get("note") or "")[:120]
        if turn_id is not None:
            try:
                with _conn() as c:
                    c.execute(
                        "UPDATE turns SET score=?, too_long=?, note=? WHERE id=?",
                        (score, too_long, note, turn_id),
                    )
            except Exception:
                pass

        # Auto-apply: vocabulary
        for w in (data.get("vocab") or [])[:4]:
            if _add_vocab(lang, str(w)):
                _log_change("vocab", f"nouveau mot appris : « {str(w).strip()} »")

        # Auto-apply: preference
        pref = str(data.get("preference") or "").strip()
        if pref and _add_pref(lang, pref):
            _log_change("preference", f"préférence retenue : « {pref} »")

        # Auto-apply: brevity tuning
        _maybe_tune_brevity()
    except Exception as e:
        log.debug("self_eval critic failed: %s", e)


def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stats for the in-app "Self-Evolution" HUD
# ---------------------------------------------------------------------------

def stats(window_hours: int = 24) -> dict:
    """Improvement telemetry for the dashboard: how many adjustments were
    auto-applied in the window, by kind, plus the current tuning state."""
    out = {
        "window_h": window_hours, "total": 0, "by_kind": {}, "recent": [],
        "vocab_total": 0, "prefs_total": 0, "max_tokens": get_max_tokens(),
    }
    try:
        since = time.time() - window_hours * 3600
        with _conn() as c:
            for r in c.execute(
                "SELECT kind, COUNT(*) n FROM changes WHERE ts>=? GROUP BY kind", (since,)
            ):
                out["by_kind"][r["kind"]] = r["n"]
                out["total"] += r["n"]
            out["recent"] = [
                {"kind": r["kind"], "detail": r["detail"], "ts": r["ts"]}
                for r in c.execute(
                    "SELECT kind, detail, ts FROM changes ORDER BY id DESC LIMIT 12")
            ]
            out["vocab_total"] = c.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
            out["prefs_total"] = c.execute("SELECT COUNT(*) FROM prefs").fetchone()[0]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Daily digest — Marion reports what she changed, once per day
# ---------------------------------------------------------------------------

def pop_due_digest(lang: str) -> Optional[str]:
    """Return a one-line spoken digest of yesterday/today's auto-changes, at most
    once per calendar day. Returns None if already delivered today or nothing to
    report."""
    try:
        today = date.today().isoformat()
        if _meta_get("digest_day") == today:
            return None
        # Summarise changes since the last delivery (everything not yet reported).
        with _conn() as c:
            rows = c.execute(
                "SELECT kind, detail FROM changes WHERE day < ? AND id > "
                "COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='digest_last_id'), 0) "
                "ORDER BY id", (today,),
            ).fetchall()
            last_id_row = c.execute("SELECT MAX(id) AS m FROM changes WHERE day < ?", (today,)).fetchone()

        # Mark delivered for today regardless, so we never nag twice a day.
        _meta_set("digest_day", today)
        if last_id_row and last_id_row["m"]:
            _meta_set("digest_last_id", str(last_id_row["m"]))

        if not rows:
            return None

        vocab = [r["detail"] for r in rows if r["kind"] == "vocab"]
        prefs = [r["detail"] for r in rows if r["kind"] == "preference"]
        brev = [r["detail"] for r in rows if r["kind"] == "brevity"]
        frus = [r for r in rows if r["kind"] == "frustration"]

        parts = []
        if vocab:
            parts.append(f"{len(vocab)} mot(s) appris")
        if prefs:
            parts.append(f"{len(prefs)} préférence(s) retenue(s)")
        if brev:
            parts.append("j'ai raccourci mes réponses")
        if frus:
            parts.append(f"{len(frus)} fois où je t'ai mal compris")
        if not parts:
            return None

        if lang == "fr":
            return ("Petit bilan d'hier, mon amour : " + ", ".join(parts) +
                    ". Je m'améliore.")
        if lang == "tr":
            return "Dünün özeti canım: " + ", ".join(parts) + "."
        return "Yesterday's learning, sir: " + ", ".join(parts) + "."
    except Exception:
        return None
