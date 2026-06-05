#!/usr/bin/env python3
"""
JARVIS WatchGuard — real-time intrusion watch for the home server (Phase 1).

Phase 1 = OBSERVE + ALERT (never blocks — that's Phase 2). It watches two
surfaces and tells you when something unauthorised happens:

  • SYSTEMS — every inbound network connection to the JARVIS ports. Each remote
    source is classified (this Mac / trusted LAN device / unknown LAN / external
    internet) and resolved (reverse-DNS). Unknown or external sources raise an
    alert with WHO connected and to WHICH port.
  • FILES   — the integrity of critical files (server.py, .env, the action
    modules…). Any change / addition / deletion raises an alert with WHAT moved.

Alerts go to: a log (.run/watchguard.log), a SQLite event store
(data/watchguard.db), a macOS notification, and — best-effort — Marion's voice
(she announces it out loud via the running backend).

It learns a baseline on first run (whatever is connected / on disk right now is
treated as legitimate), so it only cries about genuinely NEW things afterwards.

Run:   ./venv/bin/python watchguard.py
Allow: ./venv/bin/python watchguard.py --allow 192.168.1.50   (trust a device)
List:  ./venv/bin/python watchguard.py --list
Events:./venv/bin/python watchguard.py --status

Phase 1 is deliberately read-only/non-blocking. Set WATCHGUARD_BLOCK=1 later
(Phase 2) to add pf-based blocking — not enabled here.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_DATA = _REPO / "data"
_RUN = _REPO / ".run"
_DB = _DATA / "watchguard.db"
_LOG = _RUN / "watchguard.log"
_ALLOW_FILE = _DATA / "watchguard_allow.txt"
_FILES_BASELINE = _DATA / "watchguard_files.json"

# Ports we consider "ours" / sensitive (the JARVIS backend + the dev/avatar
# servers). Override with WATCHGUARD_PORTS="8340,5173".
_PORTS = {int(p) for p in os.getenv("WATCHGUARD_PORTS", "8340,5173,5174,5180,5191").split(",") if p.strip().isdigit()}

# Critical files whose contents must not change unexpectedly.
_CRITICAL = [
    "server.py", "spotify_access.py", "self_eval.py", "whisper_service.py",
    "did_avatar.py", "doorbird.py", "plejd_lights.py", "actions.py",
    "start_jarvis.sh", ".env",
]

_POLL_SECONDS = int(os.getenv("WATCHGUARD_POLL", "4"))
_BLOCK = os.getenv("WATCHGUARD_BLOCK", "0") == "1"  # Phase 2 (off here)
_ANNOUNCE_URL = "https://127.0.0.1:8340/api/watchguard/announce"
# Re-alert about the same source/file at most once per this window.
_COOLDOWN = 600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchguard] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("watchguard")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    _DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


def _init_db() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,"
            " severity TEXT, source TEXT, detail TEXT)"
        )


def _record(kind: str, severity: str, source: str, detail: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO events(ts, kind, severity, source, detail) VALUES(?,?,?,?,?)",
                (time.time(), kind, severity, source, detail),
            )
    except Exception as e:
        log.debug("record failed: %s", e)


# ---------------------------------------------------------------------------
# Allowlist + classification
# ---------------------------------------------------------------------------

def _own_lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _lan_network() -> ipaddress.IPv4Network | None:
    ip = _own_lan_ip()
    if not ip:
        return None
    try:
        return ipaddress.ip_network(ip + "/24", strict=False)
    except Exception:
        return None


_LAN_NET = _lan_network()


def load_allow() -> set[str]:
    try:
        if _ALLOW_FILE.exists():
            return {l.strip() for l in _ALLOW_FILE.read_text().splitlines()
                    if l.strip() and not l.startswith("#")}
    except Exception:
        pass
    return set()


def save_allow(entries: set[str]) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _ALLOW_FILE.write_text("# WatchGuard trusted sources (IP or CIDR), one per line\n"
                               + "\n".join(sorted(entries)) + "\n")
    except Exception as e:
        log.warning("could not save allowlist: %s", e)


def is_allowed(ip: str, allow: set[str]) -> bool:
    if ip in allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    if addr.is_loopback:
        return True
    for entry in allow:
        if "/" in entry:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except Exception:
                continue
    return False


def classify(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return "unknown"
    if addr.is_loopback:
        return "local"
    if _LAN_NET and addr.version == 4 and addr in _LAN_NET:
        return "lan"
    if addr.is_private:
        return "lan"
    return "external"


def _rdns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Network connection snapshot
# ---------------------------------------------------------------------------

_CONN_RE = re.compile(r"(\S+):(\d+)->(\S+):(\d+)")


def snapshot_inbound() -> list[tuple[str, int, int]]:
    """Return [(remote_ip, remote_port, local_port)] for ESTABLISHED connections
    whose LOCAL port is one of our monitored ports (i.e. inbound to us)."""
    out: list[tuple[str, int, int]] = []
    try:
        res = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.debug("lsof failed: %s", e)
        return out
    for line in res.stdout.splitlines():
        m = _CONN_RE.search(line)
        if not m:
            continue
        lhost, lport, rhost, rport = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        # Inbound = the LOCAL side is one of our listening ports.
        if lport in _PORTS:
            out.append((rhost, rport, lport))
        elif rport in _PORTS:
            # lsof sometimes orders the listening side second.
            out.append((lhost, lport, rport))
    return out


# ---------------------------------------------------------------------------
# File integrity
# ---------------------------------------------------------------------------

def _hash_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _load_baseline() -> dict:
    try:
        if _FILES_BASELINE.exists():
            return json.loads(_FILES_BASELINE.read_text())
    except Exception:
        pass
    return {}


def _save_baseline(b: dict) -> None:
    try:
        _FILES_BASELINE.write_text(json.dumps(b, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def _notify_macos(title: str, detail: str) -> None:
    try:
        safe_t = title.replace('"', "'")
        safe_d = detail.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_d}" with title "{safe_t}"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _announce_via_marion(text: str) -> None:
    """Best-effort: ask the running backend to say the alert in Marion's voice."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        data = json.dumps({"text": text, "lang": "fr"}).encode()
        req = urllib.request.Request(_ANNOUNCE_URL, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5, context=ctx)
    except Exception:
        pass  # backend down / endpoint absent — log + notification still happened


def alert(kind: str, severity: str, source: str, detail: str, spoken: str) -> None:
    line = f"{severity.upper()} [{kind}] source={source} :: {detail}"
    log.warning(line)
    _record(kind, severity, source, detail)
    _notify_macos(f"WatchGuard — {severity.upper()}", detail)
    _announce_via_marion(spoken)


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

def _run_monitor() -> None:
    _RUN.mkdir(parents=True, exist_ok=True)
    _init_db()
    allow = load_allow()

    # Seed the allowlist on first run: trust this Mac + DoorBird + whatever is
    # already connected (baseline = legitimate). After this, NEW sources alert.
    first_run = not _ALLOW_FILE.exists()
    if first_run:
        allow |= {"127.0.0.1", "::1"}
        own = _own_lan_ip()
        if own:
            allow.add(own)
        allow.add("192.168.1.26")  # DoorBird intercom (known infra)
        learned = 0
        for rip, _rport, _lport in snapshot_inbound():
            if rip not in allow:
                allow.add(rip)
                learned += 1
        save_allow(allow)
        log.info("baseline learned: %d current device(s) trusted. Edit %s to curate.",
                 learned, _ALLOW_FILE)

    # File integrity baseline.
    baseline = _load_baseline()
    if not baseline:
        baseline = {f: _hash_file(_REPO / f) for f in _CRITICAL if (_REPO / f).exists()}
        _save_baseline(baseline)
        log.info("file-integrity baseline set for %d file(s).", len(baseline))

    log.info("WatchGuard active — ports=%s lan=%s block=%s. Watching connections + %d files.",
             sorted(_PORTS), _LAN_NET, _BLOCK, len(baseline))

    last_alert: dict[str, float] = {}

    def _cool(key: str) -> bool:
        now = time.time()
        if now - last_alert.get(key, 0) < _COOLDOWN:
            return False
        last_alert[key] = now
        return True

    while True:
        try:
            allow = load_allow()  # re-read so --allow takes effect live
            # --- systems: inbound connections ---
            for rip, rport, lport in snapshot_inbound():
                if is_allowed(rip, allow):
                    continue
                kind_of = classify(rip)
                if kind_of == "local":
                    continue
                key = f"conn:{rip}:{lport}"
                if not _cool(key):
                    continue
                host = _rdns(rip)
                where = f"{host} ({rip})" if host else rip
                sev = "critical" if kind_of == "external" else "warning"
                detail = (f"{kind_of.upper()} source {where} connected to port {lport}"
                          f"{' (JARVIS)' if lport == 8340 else ''}")
                spoken = ("Attention mon amour, une connexion "
                          + ("externe" if kind_of == "external" else "inconnue")
                          + f" vient de toucher le port {lport}.")
                alert("connection", sev, where, detail, spoken)

            # --- files: integrity ---
            current = {f: _hash_file(_REPO / f) for f in _CRITICAL}
            for f, h in current.items():
                old = baseline.get(f)
                if old is None and h is not None:
                    if _cool(f"file:new:{f}"):
                        alert("file", "warning", "local",
                              f"new critical file appeared: {f}",
                              f"Un nouveau fichier critique est apparu : {f}.")
                elif old is not None and h is None:
                    if _cool(f"file:del:{f}"):
                        alert("file", "critical", "local",
                              f"critical file DELETED: {f}",
                              f"Alerte : le fichier {f} a été supprimé.")
                elif old and h and old != h:
                    if _cool(f"file:mod:{f}"):
                        alert("file", "warning", "local",
                              f"critical file MODIFIED: {f}",
                              f"Le fichier {f} vient d'être modifié.")
                baseline[f] = h
            _save_baseline(baseline)

        except Exception as e:
            log.debug("monitor tick error: %s", e)
        time.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_list() -> None:
    allow = load_allow()
    print("Trusted sources (allowlist):")
    for a in sorted(allow):
        print(f"  {a}")
    print(f"\nMonitored ports: {sorted(_PORTS)}   LAN: {_LAN_NET}")


def _cli_status(n: int = 20) -> None:
    try:
        with _conn() as c:
            rows = c.execute("SELECT ts, severity, kind, source, detail FROM events "
                             "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    except Exception:
        rows = []
    if not rows:
        print("No events recorded yet.")
        return
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        print(f"  {when}  {r['severity'].upper():8s} {r['kind']:10s} {r['source']:24s} {r['detail']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS WatchGuard")
    ap.add_argument("--allow", metavar="IP", help="trust a source (IP or CIDR) and exit")
    ap.add_argument("--remove", metavar="IP", help="untrust a source and exit")
    ap.add_argument("--list", action="store_true", help="show the allowlist and exit")
    ap.add_argument("--status", action="store_true", help="show recent events and exit")
    args = ap.parse_args()

    if args.allow:
        a = load_allow(); a.add(args.allow.strip()); save_allow(a)
        print(f"Trusted: {args.allow}"); return 0
    if args.remove:
        a = load_allow(); a.discard(args.remove.strip()); save_allow(a)
        print(f"Untrusted: {args.remove}"); return 0
    if args.list:
        _cli_list(); return 0
    if args.status:
        _cli_status(); return 0

    try:
        _run_monitor()
    except KeyboardInterrupt:
        print("\nWatchGuard stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
