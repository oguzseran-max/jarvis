"""
JARVIS Self-Formation — Phase 3 of Marion's self-improvement program.

Phases 0-2 react to individual turns and bugs. This phase steps back: on a slow
cadence it MINES the accumulated quality data (perf_monitor latency + flags,
self_eval critic scores, the autonomous bug-fixer's history) and asks Haiku to
name Marion's STRUCTURAL weaknesses — the patterns no single turn reveals — and
to propose short, actionable guidance she can follow to get better over time.

Two outputs:
  • an ASSESSMENT (stored with history, so you can watch the trend) + a once-a-day
    spoken digest line.
  • GUIDANCE rules — one-sentence behavioural nudges injected into Marion's system
    prompt (same mechanism as self_eval's learned preferences), bounded and
    reversible.

SAFE BY DEFAULT: guidance is only PROPOSED, not applied — it does not change
Marion's behaviour until you activate it (POST /api/formation/guidance/{id}/
activate), or unless FORMATION_AUTOAPPLY=true. Assessment/reporting always runs.
Active guidance is capped and every rule is reversible. Crash-safe throughout.

Storage: data/self_formation.db (its own SQLite DB).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import perf_monitor
import self_eval

try:
    import bug_fixer
except Exception:  # keep Phase 3 independent if Phase 2 is absent
    bug_fixer = None

log = logging.getLogger("jarvis.formation")

_DB = Path(__file__).resolve().parent / "data" / "self_formation.db"

_INTERVAL = int(os.getenv("FORMATION_INTERVAL", "21600"))      # 6h default
_MIN_TURNS = int(os.getenv("FORMATION_MIN_TURNS", "15"))       # need data first
_AUTOAPPLY = os.getenv("FORMATION_AUTOAPPLY", "false").lower() == "true"
_MAX_ACTIVE = int(os.getenv("FORMATION_MAX_GUIDANCE", "5"))
_MODEL = "claude-haiku-4-5-20251001"


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB), timeout=5)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    try:
        c = _conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                summary TEXT DEFAULT '',
                weaknesses TEXT DEFAULT '[]',
                signals TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS guidance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                lang TEXT DEFAULT 'all',
                text TEXT NOT NULL,
                area TEXT DEFAULT '',
                status TEXT DEFAULT 'proposed'   -- proposed | active | removed
            );
        """)
        c.commit()
        c.close()
        log.info("self-formation ready (autoapply=%s)", _AUTOAPPLY)
    except Exception as e:
        log.warning("formation init failed: %s", e)


# ---------------------------------------------------------------------------
# Signal gathering
# ---------------------------------------------------------------------------

def _cluster_flags(flagged: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in flagged:
        for f in (t.get("flags") or [])[:1]:
            sig = re.sub(r"\d+", "", f.lower().split(":", 1)[0]).strip()
            out[sig] = out.get(sig, 0) + 1
    return out


def gather_signals(window_h: int = 168) -> dict:
    """Aggregate the week's quality telemetry from the other phases."""
    sig: dict = {}
    try:
        sig["perf"] = perf_monitor.stats(window_h)
    except Exception:
        sig["perf"] = {}
    try:
        sig["flag_clusters"] = _cluster_flags(perf_monitor.recent_flags(window_h, 300))
    except Exception:
        sig["flag_clusters"] = {}
    try:
        sig["self_eval"] = self_eval.stats(window_h)
    except Exception:
        sig["self_eval"] = {}
    if bug_fixer:
        try:
            fixes = bug_fixer.list_fixes(50)
            by_status: dict[str, int] = {}
            for f in fixes:
                by_status[f["status"]] = by_status.get(f["status"], 0) + 1
            sig["bugfix"] = {"total": len(fixes), "by_status": by_status}
        except Exception:
            sig["bugfix"] = {}
    return sig


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------

_SYS = (
    "You are the self-improvement analyst for Marion, a British-butler voice "
    "assistant (dry wit, 1-2 sentence replies, English/French/Turkish). You are "
    "given a WEEK of her own quality telemetry. Identify her top STRUCTURAL "
    "weaknesses — recurring patterns, not one-offs — and for each propose ONE "
    "short actionable guidance rule she can follow to improve (max 18 words, "
    "imperative, e.g. 'When the user asks about the calendar, confirm the date "
    "before answering'). Focus on what the data shows: latency by stage, "
    "off-target/ memory-miss/ complaint flags, critic adjustments, recurring "
    "bugs. Be concrete and few (1-3 items). "
    "Return ONLY JSON: {\"summary\":\"<=20 words\", \"weaknesses\":[{\"area\":\"latency|"
    "memory|accuracy|persona|coverage\", \"evidence\":\"...\", \"guidance\":\"...\", "
    "\"lang\":\"all|en|fr|tr\"}]}. If the data is too thin or healthy, return an "
    "empty weaknesses list."
)


async def run_assessment(client, force: bool = False) -> dict:
    """Mine the signals, synthesize an assessment, store it, and (if allowed)
    propose/apply guidance. Returns a small report dict."""
    if client is None:
        return {"ok": False, "reason": "no LLM client"}
    signals = gather_signals()
    turns = (signals.get("perf") or {}).get("turns", 0)
    if turns < _MIN_TURNS and not force:
        return {"ok": False, "reason": f"insufficient data ({turns}/{_MIN_TURNS} turns)"}

    try:
        resp = await client.messages.create(
            model=_MODEL, max_tokens=600, system=_SYS,
            messages=[{"role": "user", "content": json.dumps(signals, ensure_ascii=False)[:6000]}],
        )
        data = _parse_json(resp.content[0].text)
    except Exception as e:
        log.warning("assessment LLM failed: %s", e)
        return {"ok": False, "reason": str(e)}
    if not data:
        return {"ok": False, "reason": "unparseable assessment"}

    summary = (data.get("summary") or "").strip()
    weaknesses = data.get("weaknesses") or []
    _store_assessment(summary, weaknesses, signals)

    proposed, applied = [], []
    for w in weaknesses:
        g = (w.get("guidance") or "").strip()
        if not g or len(g) > 160:
            continue
        lang = w.get("lang", "all")
        area = w.get("area", "")
        status = "active" if _AUTOAPPLY else "proposed"
        if _add_guidance(g, lang, area, status):
            (applied if status == "active" else proposed).append(g)
    _enforce_active_cap()
    return {"ok": True, "summary": summary, "weaknesses": weaknesses,
            "proposed": proposed, "applied": applied, "autoapply": _AUTOAPPLY}


def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    s = raw.strip()
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        s = m.group(0)
    try:
        return json.loads(s)
    except Exception:
        return None


def _store_assessment(summary: str, weaknesses: list, signals: dict) -> None:
    try:
        c = _conn()
        c.execute("INSERT INTO assessments (ts, summary, weaknesses, signals) VALUES (?,?,?,?)",
                  (time.time(), summary, json.dumps(weaknesses, ensure_ascii=False),
                   json.dumps(signals, ensure_ascii=False)[:8000]))
        c.commit()
        c.close()
    except Exception as e:
        log.debug("store assessment failed: %s", e)


# ---------------------------------------------------------------------------
# Guidance store + prompt injection
# ---------------------------------------------------------------------------

def _add_guidance(text: str, lang: str, area: str, status: str) -> bool:
    """Insert a guidance rule unless a near-identical one already exists."""
    try:
        c = _conn()
        existing = c.execute("SELECT text FROM guidance WHERE status!='removed'").fetchall()
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        for r in existing:
            if re.sub(r"\W+", " ", r["text"].lower()).strip() == norm:
                c.close()
                return False
        c.execute("INSERT INTO guidance (ts, lang, text, area, status) VALUES (?,?,?,?,?)",
                  (time.time(), lang or "all", text, area or "", status))
        c.commit()
        c.close()
        log.info("guidance %s: %s", status, text)
        return True
    except Exception as e:
        log.debug("add guidance failed: %s", e)
        return False


def _enforce_active_cap() -> None:
    """Keep only the newest _MAX_ACTIVE active rules (oldest demoted to proposed)."""
    try:
        c = _conn()
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM guidance WHERE status='active' ORDER BY id DESC").fetchall()]
        for old in ids[_MAX_ACTIVE:]:
            c.execute("UPDATE guidance SET status='proposed' WHERE id=?", (old,))
        c.commit()
        c.close()
    except Exception:
        pass


def get_guidance_text(lang: str) -> str:
    """Active guidance for the system prompt — mirrors self_eval.get_preferences_text."""
    try:
        c = _conn()
        rows = c.execute(
            "SELECT text FROM guidance WHERE status='active' AND lang IN ('all', ?) "
            "ORDER BY id DESC LIMIT ?", (lang or "all", _MAX_ACTIVE)).fetchall()
        c.close()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"- {r['text']}" for r in rows)
    return ("\n\nLEARNED GUIDANCE (from your own performance review — follow these):\n"
            + lines)


def list_guidance(limit: int = 30) -> list[dict]:
    out = []
    try:
        c = _conn()
        for r in c.execute("SELECT id,ts,lang,text,area,status FROM guidance "
                           "WHERE status!='removed' ORDER BY id DESC LIMIT ?", (limit,)):
            out.append(dict(r))
        c.close()
    except Exception:
        pass
    return out


def set_guidance_status(gid: int, status: str) -> dict:
    if status not in ("active", "proposed", "removed"):
        return {"ok": False, "error": "bad status"}
    try:
        c = _conn()
        c.execute("UPDATE guidance SET status=? WHERE id=?", (status, gid))
        c.commit()
        c.close()
        if status == "active":
            _enforce_active_cap()
        return {"ok": True, "id": gid, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Reporting + daily digest + loop
# ---------------------------------------------------------------------------

def latest() -> dict:
    out = {"assessment": None, "guidance": list_guidance()}
    try:
        c = _conn()
        r = c.execute("SELECT ts, summary, weaknesses FROM assessments "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        if r:
            out["assessment"] = {"ts": r["ts"], "summary": r["summary"],
                                 "weaknesses": json.loads(r["weaknesses"] or "[]")}
    except Exception:
        pass
    return out


def history(limit: int = 20) -> list[dict]:
    out = []
    try:
        c = _conn()
        for r in c.execute("SELECT ts, summary FROM assessments ORDER BY id DESC LIMIT ?",
                           (limit,)):
            out.append({"ts": r["ts"], "summary": r["summary"]})
        c.close()
    except Exception:
        pass
    return out


def pop_due_digest(lang: str) -> Optional[str]:
    """Once per day, a one-line spoken summary of the latest self-assessment."""
    try:
        c = _conn()
        last = c.execute("SELECT value FROM meta WHERE key='digest_day'").fetchone() \
            if _has_meta(c) else None
    except Exception:
        last = None
    today = date.today().isoformat()
    try:
        c = _conn()
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM meta WHERE key='digest_day'").fetchone()
        if row and row["value"] == today:
            c.close()
            return None
        a = c.execute("SELECT summary FROM assessments ORDER BY id DESC LIMIT 1").fetchone()
        c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('digest_day',?)", (today,))
        c.commit()
        c.close()
        if a and a["summary"]:
            return a["summary"]
    except Exception:
        pass
    return None


def _has_meta(c) -> bool:
    try:
        return bool(c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name='meta'").fetchone())
    except Exception:
        return False


async def loop(get_client: Callable[[], object], interval: Optional[int] = None) -> None:
    """Periodic self-assessment. Start once at server boot via create_task."""
    iv = interval or _INTERVAL
    log.info("self-formation loop every %ss (autoapply=%s, min_turns=%s)",
             iv, _AUTOAPPLY, _MIN_TURNS)
    await asyncio.sleep(120)  # let the server settle + accumulate a little data
    while True:
        try:
            await run_assessment(get_client())
        except Exception as e:
            log.debug("formation tick error: %s", e)
        await asyncio.sleep(iv)
