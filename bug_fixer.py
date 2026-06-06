"""
JARVIS Autonomous Bug-Fixer — Phase 2 of Marion's self-improvement program.

Closes the loop opened by perf_monitor (Phase 0): when a bug RECURS in the live
voice loop, Marion diagnoses it and drafts a fix herself — autonomously, but
behind hard guardrails — then waits for your one-click approval to land it.

Flow:
  perf_monitor flags turns  →  cluster into recurring "signatures"  →  for an
  untreated, recurring signature, spawn a headless `claude -p` IN AN ISOLATED
  GIT WORKTREE on a dedicated `autofix/*` branch  →  commit + verify (compile /
  diff)  →  mark READY and notify you  →  you approve (merge) or dismiss.

GUARDRAILS (non-negotiable — this writes code):
  • NEVER touches the running checkout: all work happens in a throwaway git
    worktree under a temp dir, on a fresh branch off HEAD.
  • NEVER pushes, NEVER merges on its own. Landing a fix requires an explicit
    approve() call (a human click). dismiss() throws the branch away.
  • Single fix in flight at a time; per-signature cooldown; recurrence gate
    (a bug must happen >= N times before any code is written).
  • Kill switch: BUGFIX_ENABLED=false disables all code-writing (detection
    still records, so you keep visibility).
  • Crash-safe: every public call is wrapped; a failure never touches the loop.

Storage: data/bugfix.db (its own SQLite DB).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import perf_monitor

log = logging.getLogger("jarvis.bugfix")

_REPO = Path(__file__).resolve().parent
_DB = _REPO / "data" / "bugfix.db"
_WORKTREE_ROOT = Path(tempfile.gettempdir()) / "jarvis-bugfix"

# Tunables (env-overridable).
# OFF by default: the unattended timer writes code (via skip-permissions claude),
# so it stays opt-in. With it off, detection still records recurring bugs and you
# can draft a fix on demand via POST /api/bugfix/scan (an explicit human action).
# Set BUGFIX_ENABLED=true to allow the autonomous timer.
_ENABLED = os.getenv("BUGFIX_ENABLED", "false").lower() == "true"
_INTERVAL = int(os.getenv("BUGFIX_INTERVAL", "300"))            # detector tick (s)
_MIN_OCCURRENCES = int(os.getenv("BUGFIX_MIN_OCCURRENCES", "3"))
_COOLDOWN_H = float(os.getenv("BUGFIX_COOLDOWN_HOURS", "6"))
_CLAUDE_TIMEOUT = int(os.getenv("BUGFIX_CLAUDE_TIMEOUT", "900"))
_SKIP_PERMS = os.getenv("JARVIS_SKIP_PERMISSIONS", "true").lower() != "false"

_active = False                       # single-flight guard
_speak: Optional[Callable[[str, str], object]] = None  # optional voice sink


def set_speak_sink(fn: Callable[[str, str], object]) -> None:
    """Let server.py wire Marion's voice in without a circular import."""
    global _speak
    _speak = fn


def is_enabled() -> bool:
    """True only when the unattended code-writing timer is explicitly allowed."""
    return _ENABLED


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB), timeout=5)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    try:
        c = _conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS fixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL,
                status TEXT NOT NULL,        -- analyzing|ready|nofix|error|approved|dismissed
                branch TEXT DEFAULT '',
                worktree TEXT DEFAULT '',
                occurrences INTEGER DEFAULT 0,
                examples TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                diffstat TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        c.commit()
        c.close()
        _WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
        log.info("bug-fixer ready (enabled=%s)", _ENABLED)
    except Exception as e:
        log.warning("bugfix init failed: %s", e)


# ---------------------------------------------------------------------------
# Signatures — turn raw perf flags into a stable bug identity
# ---------------------------------------------------------------------------

def signature(flag: str) -> str:
    """Normalize a flag to a recurring category (drop numbers/specifics)."""
    f = (flag or "").lower().split(":", 1)[0].strip()
    f = re.sub(r"\d+", "", f)
    return re.sub(r"\s+", " ", f).strip() or "unknown"


def _cluster(flagged: list[dict]) -> dict[str, list[dict]]:
    """Group flagged turns by the signature of their first flag."""
    out: dict[str, list[dict]] = {}
    for t in flagged:
        flags = t.get("flags") or []
        if not flags:
            continue
        sig = signature(flags[0])
        out.setdefault(sig, []).append(t)
    return out


# ---------------------------------------------------------------------------
# git / subprocess helpers
# ---------------------------------------------------------------------------

async def _run(cmd: list[str], cwd: Optional[Path] = None,
               timeout: int = 60, stdin: Optional[str] = None) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin.encode() if stdin is not None else None),
            timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def _slug(sig: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sig.lower()).strip("-")[:32] or "bug"


# ---------------------------------------------------------------------------
# The fix run
# ---------------------------------------------------------------------------

_FIX_PROMPT = """\
You are fixing a RECURRING bug in JARVIS/Marion, a voice assistant. You are in
an isolated git worktree (a throwaway branch) — work only here.

The live performance monitor has flagged this issue repeatedly:

  SIGNATURE: {sig}
  OCCURRENCES: {n}
  EXAMPLES (real turns):
{examples}

The backend is a ~2700-line FastAPI monolith (server.py) plus modules:
perf_monitor.py (the telemetry), memory.py, self_eval.py, actions.py,
whisper_service.py. The flag text tells you which subsystem is implicated
(e.g. "slow reasoning" = the classify/LLM reply path; "possible memory miss" =
memory recall/injection; "slow transcription" = whisper; "off-target reply" =
the system prompt / classifier).

Your job:
  1. Investigate the root cause (read the relevant code).
  2. Implement the MINIMAL, SAFE fix. Do not refactor unrelated code. Do not
     change public behaviour beyond fixing this bug. Keep the voice persona.
  3. If you genuinely cannot find a safe fix, change nothing and say so.
  4. Briefly summarize: root cause, what you changed, and how to verify.

Do NOT commit, push, or run git — just edit files. Be surgical.
"""


async def _run_claude_fix(worktree: Path, prompt: str) -> tuple[bool, str]:
    """Spawn headless claude in the worktree. Returns (ran_ok, summary_text).
    Isolated so the rest of the pipeline is testable without an LLM call."""
    claude = shutil.which("claude")
    if not claude:
        return False, "claude CLI not found"
    cmd = [claude, "-p", "--output-format", "text"]
    if _SKIP_PERMS:
        cmd.append("--dangerously-skip-permissions")
    rc, out, err = await _run(cmd, cwd=worktree, timeout=_CLAUDE_TIMEOUT, stdin=prompt)
    if rc != 0:
        return False, (err or out or "claude failed")[:1000]
    return True, out.strip()[:4000]


async def _spawn_fix(sig: str, examples: list[dict]) -> Optional[int]:
    """Create an isolated worktree, let claude draft a fix, commit + verify it,
    and record the result as READY (awaiting approval) or nofix/error."""
    global _active
    branch = f"autofix/{_slug(sig)}-{datetime.now():%Y%m%d-%H%M%S}"
    wt = _WORKTREE_ROOT / branch.replace("/", "_")
    fix_id = _insert(sig, "analyzing", branch, str(wt), len(examples), examples)

    try:
        # Fresh worktree on a new branch off current HEAD — never the live tree.
        rc, _, err = await _run(["git", "worktree", "add", "-b", branch, str(wt), "HEAD"],
                                cwd=_REPO, timeout=120)
        if rc != 0:
            return _finish(fix_id, "error", error=f"worktree: {err[:300]}")

        ex_txt = "\n".join(
            f"    - [{e.get('total_ms','?')}ms] user={e.get('user_text','')[:80]!r} "
            f"flags={e.get('flags')}" for e in examples[:6])
        prompt = _FIX_PROMPT.format(sig=sig, n=len(examples), examples=ex_txt)

        ran, summary = await _run_claude_fix(wt, prompt)
        if not ran:
            return _finish(fix_id, "error", summary=summary, error="claude run failed")

        # Capture whatever claude changed (we commit, not claude).
        await _run(["git", "add", "-A"], cwd=wt, timeout=30)
        rc, diffstat, _ = await _run(["git", "diff", "--cached", "--stat"], cwd=wt, timeout=30)
        if not diffstat.strip():
            return _finish(fix_id, "nofix", summary=summary or "No change proposed.")

        await _run(["git", "commit", "-m", f"autofix: {sig}\n\n{summary[:400]}"],
                   cwd=wt, timeout=30)

        # Lightweight verification: changed .py files must still compile.
        rc, names, _ = await _run(["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                                  cwd=wt, timeout=30)
        bad = []
        for f in [n for n in names.split() if n.endswith(".py")]:
            crc, _, cerr = await _run([sys.executable, "-m", "py_compile", f], cwd=wt, timeout=30)
            if crc != 0:
                bad.append(f"{f}: {cerr.strip()[:120]}")
        if bad:
            return _finish(fix_id, "error", summary=summary, diffstat=diffstat,
                           error="fix does not compile: " + "; ".join(bad))

        _finish(fix_id, "ready", summary=summary, diffstat=diffstat)
        _notify(sig, branch, diffstat)
        return fix_id
    except Exception as e:
        log.error("spawn_fix failed: %s", e, exc_info=True)
        return _finish(fix_id, "error", error=str(e)[:300])
    finally:
        _active = False


# ---------------------------------------------------------------------------
# Detection tick + loop
# ---------------------------------------------------------------------------

async def detect_and_fix(force: bool = False) -> dict:
    """One detection pass. Returns a small report dict (also used by the API)."""
    global _active
    if not _ENABLED and not force:
        return {"enabled": False, "acted": False, "reason": "disabled"}
    if _active:
        return {"enabled": True, "acted": False, "reason": "a fix is already in flight"}

    flagged = perf_monitor.recent_flags(window_hours=24, limit=200)
    clusters = _cluster(flagged)
    recurring = {s: ex for s, ex in clusters.items() if len(ex) >= _MIN_OCCURRENCES}
    candidates = {s: ex for s, ex in recurring.items() if _eligible(s)}
    report = {"enabled": True, "acted": False,
              "recurring": {s: len(e) for s, e in recurring.items()},
              "candidates": list(candidates)}
    if not candidates:
        report["reason"] = "no eligible recurring bug"
        return report

    # Tackle the most frequent eligible bug first.
    sig, examples = max(candidates.items(), key=lambda kv: len(kv[1]))
    _active = True
    report["acted"] = True
    report["signature"] = sig
    asyncio.create_task(_spawn_fix(sig, examples))  # runs in background
    return report


async def loop(interval: Optional[int] = None) -> None:
    """Background detector — call once at startup via asyncio.create_task."""
    iv = interval or _INTERVAL
    log.info("bug-fixer loop every %ss (enabled=%s, min_occ=%s)", iv, _ENABLED, _MIN_OCCURRENCES)
    while True:
        try:
            await detect_and_fix()
        except Exception as e:
            log.debug("bugfix tick error: %s", e)
        await asyncio.sleep(iv)


def _eligible(sig: str) -> bool:
    """Skip signatures already in flight/ready or within the retry cooldown."""
    try:
        c = _conn()
        row = c.execute("SELECT status, updated_at FROM fixes WHERE signature=? "
                        "ORDER BY id DESC LIMIT 1", (sig,)).fetchone()
        c.close()
        if not row:
            return True
        if row["status"] in ("analyzing", "ready"):
            return False  # don't pile up on the same bug
        if row["status"] in ("error", "nofix", "dismissed"):
            return (time.time() - row["updated_at"]) > _COOLDOWN_H * 3600
        if row["status"] == "approved":
            return True   # it came back → allow another attempt
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Persistence + review API
# ---------------------------------------------------------------------------

def _insert(sig, status, branch, worktree, occ, examples) -> int:
    try:
        now = time.time()
        c = _conn()
        cur = c.execute(
            "INSERT INTO fixes (signature,status,branch,worktree,occurrences,examples,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (sig, status, branch, worktree, occ, json.dumps(examples[:6]), now, now))
        c.commit(); rid = cur.lastrowid; c.close()
        return rid
    except Exception as e:
        log.warning("bugfix insert failed: %s", e)
        return -1


def _finish(fix_id, status, summary="", diffstat="", error="") -> int:
    try:
        c = _conn()
        c.execute("UPDATE fixes SET status=?, summary=?, diffstat=?, error=?, updated_at=? "
                  "WHERE id=?", (status, summary, diffstat, error, time.time(), fix_id))
        c.commit(); c.close()
    except Exception as e:
        log.warning("bugfix finish failed: %s", e)
    log.info("bugfix #%s -> %s", fix_id, status)
    return fix_id


def _notify(sig: str, branch: str, diffstat: str) -> None:
    msg = f"Correctif prêt pour « {sig} » sur {branch}. À valider."
    log.warning("[bugfix] %s\n%s", msg, diffstat)
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{sig} — à valider" with title "Marion — correctif prêt"'],
                       capture_output=True, timeout=4)
    except Exception:
        pass
    if _speak:
        try:
            _speak(f"Mon amour, j'ai préparé un correctif pour un bug récurrent. Il attend ton accord.", "fr")
        except Exception:
            pass


def list_fixes(limit: int = 20) -> list[dict]:
    out = []
    try:
        c = _conn()
        for r in c.execute("SELECT id,signature,status,branch,occurrences,summary,"
                           "diffstat,error,created_at,updated_at FROM fixes "
                           "ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            out.append(d)
        c.close()
    except Exception as e:
        log.debug("list_fixes failed: %s", e)
    return out


def _get(fix_id: int) -> Optional[dict]:
    try:
        c = _conn()
        r = c.execute("SELECT * FROM fixes WHERE id=?", (fix_id,)).fetchone()
        c.close()
        return dict(r) if r else None
    except Exception:
        return None


async def approve(fix_id: int) -> dict:
    """Land a READY fix: merge its branch into the current branch, then clean up.
    The only path that touches the live tree — and only on an explicit click."""
    fx = _get(fix_id)
    if not fx:
        return {"ok": False, "error": "unknown fix"}
    if fx["status"] != "ready":
        return {"ok": False, "error": f"fix is '{fx['status']}', not ready"}
    branch = fx["branch"]
    rc, out, err = await _run(["git", "merge", "--no-ff", "--no-edit", branch],
                              cwd=_REPO, timeout=120)
    if rc != 0:
        await _run(["git", "merge", "--abort"], cwd=_REPO, timeout=30)
        return {"ok": False, "error": f"merge conflict — review {branch} manually: {err[:200]}"}
    await _cleanup_worktree(fx)
    _finish(fix_id, "approved", summary=fx["summary"], diffstat=fx["diffstat"])
    return {"ok": True, "merged": branch}


async def dismiss(fix_id: int) -> dict:
    """Throw a fix away: remove the worktree and delete the branch."""
    fx = _get(fix_id)
    if not fx:
        return {"ok": False, "error": "unknown fix"}
    await _cleanup_worktree(fx)
    await _run(["git", "branch", "-D", fx["branch"]], cwd=_REPO, timeout=30)
    _finish(fix_id, "dismissed", summary=fx["summary"])
    return {"ok": True, "dismissed": fx["branch"]}


async def _cleanup_worktree(fx: dict) -> None:
    wt = fx.get("worktree")
    if wt and Path(wt).exists():
        await _run(["git", "worktree", "remove", "--force", wt], cwd=_REPO, timeout=30)


async def diff(fix_id: int) -> str:
    """Full patch of a fix's branch, for the review panel."""
    fx = _get(fix_id)
    if not fx:
        return ""
    rc, out, _ = await _run(["git", "diff", f"HEAD...{fx['branch']}"], cwd=_REPO, timeout=30)
    return out
