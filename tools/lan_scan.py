#!/usr/bin/env python3
"""
JARVIS LAN Scanner — discover every device on the internal network, enrich it
(vendor / hostname / open ports / known-device label / trust status), score the
risk, and render a self-contained interactive JARVIS-style dashboard.

It reuses WatchGuard's data:
  • data/watchguard_devices.json  — friendly device labels (IP -> name)
  • data/watchguard_allow.txt     — which IPs are trusted vs under surveillance

Run:   ./venv/bin/python tools/lan_scan.py            (scan + build dashboard)
       ./venv/bin/python tools/lan_scan.py --open     (… and open it in Chrome)
       ./venv/bin/python tools/lan_scan.py --no-scan  (rebuild HTML from cache)

Outputs:
  • data/lan_inventory.json   — machine-readable inventory (cached vendors)
  • data/network_map.html     — the interactive dashboard (open in a browser)

No sudo, no nmap: ARP + ping-sweep + plain TCP connect probes.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DATA = _REPO / "data"
_DEVICES = _DATA / "watchguard_devices.json"
_ALLOW = _DATA / "watchguard_allow.txt"
_INVENTORY = _DATA / "lan_inventory.json"
_HTML = _DATA / "network_map.html"
# Single source of truth: the live dashboard served by the frontend at
# :5173/network.html. lan_scan.py reuses it as the template for the standalone
# (data-embedded) copy written to data/network_map.html.
_TEMPLATE = _REPO / "frontend" / "public" / "network.html"

_SUBNET = "192.168.1.0/24"

# Common ports worth probing (service name for display).
_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 53: "DNS", 80: "HTTP", 139: "NetBIOS",
    443: "HTTPS", 445: "SMB", 554: "RTSP", 1900: "UPnP", 3389: "RDP",
    5555: "ADB", 5900: "VNC", 8008: "Cast", 8080: "HTTP-alt", 8443: "HTTPS-alt",
    9100: "Printer", 32400: "Plex",
}

# Per-IP curated knowledge (type + risk + note), from the manual analysis. New
# devices not listed here get heuristics from vendor/host/ports.
_KNOWN: dict[str, dict] = {
    "192.168.1.1":  {"type": "gateway", "risk": "medium",
                     "note": "Box / passerelle Internet. Admin en HTTP clair — mot de passe fort + pas d'admin distante."},
    "192.168.1.10": {"type": "iot",     "risk": "low",
                     "note": "Hub domotique Homematic IP. Pas de port exposé."},
    "192.168.1.11": {"type": "mobile",  "risk": "watch",
                     "note": "Non confirmé. Profil mobile/tablette (module FN-Link, aucun service). Sous surveillance."},
    "192.168.1.12": {"type": "mobile",  "risk": "medium",
                     "note": "Appareil Xiaomi. Télémétrie vers cloud étranger — idéalement réseau invité."},
    "192.168.1.13": {"type": "iot",     "risk": "medium",
                     "note": "IoT Tuya (cloud chinois). Pas de surface locale, mais dépendance/ télémétrie cloud."},
    "192.168.1.14": {"type": "tv",      "risk": "medium",
                     "note": "TV LG webOS. Historique de vulnérabilités + micro. Firmware à jour."},
    "192.168.1.15": {"type": "audio",   "risk": "low",
                     "note": "Enceinte HEDDON (Spotify Connect)."},
    "192.168.1.18": {"type": "mobile",  "risk": "watch",
                     "note": "Auto-déclaré « android ». Téléphone/tablette probable. Sous surveillance."},
    "192.168.1.26": {"type": "camera",  "risk": "high",
                     "note": "Interphone/caméra DoorBird. RTSP 554 exposé — mot de passe fort, firmware à jour."},
    "192.168.1.27": {"type": "iot",     "risk": "low",
                     "note": "Passerelle Plejd (éclairage)."},
    "192.168.1.28": {"type": "camera",  "risk": "high",
                     "note": "Seconde caméra/interphone DoorBird."},
    "192.168.1.36": {"type": "host",    "risk": "low",
                     "note": "Ce Mac — serveur JARVIS."},
    "192.168.1.40": {"type": "mobile",  "risk": "low",
                     "note": "iPhone d'Oz."},
    "192.168.1.60": {"type": "camera",  "risk": "high",
                     "note": "Caméra extérieure Netatmo Presence."},
}

_TYPE_ICON = {"gateway": "🌐", "camera": "📷", "tv": "📺", "audio": "🔊",
              "iot": "🏠", "mobile": "📱", "host": "💻", "unknown": "❓"}


def load_labels() -> dict[str, str]:
    try:
        return {str(k): str(v) for k, v in json.loads(_DEVICES.read_text()).items()}
    except Exception:
        return {}


def load_allow() -> set[str]:
    try:
        return {l.strip() for l in _ALLOW.read_text().splitlines()
                if l.strip() and not l.startswith("#")}
    except Exception:
        return set()


def ping_sweep(subnet: str) -> None:
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]

    def _ping(ip: str) -> None:
        subprocess.run(["ping", "-c1", "-W1", ip],
                       capture_output=True, timeout=3)
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(lambda ip: _safe(_ping, ip), hosts))


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def arp_table(subnet: str) -> dict[str, str]:
    net = ipaddress.ip_network(subnet, strict=False)
    out = subprocess.run(["arp", "-a", "-n"], capture_output=True, text=True).stdout
    devs: dict[str, str] = {}
    for line in out.splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)", line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2)
        if "incomplete" in line or ip.endswith(".255"):
            continue
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except Exception:
            continue
        devs[ip] = ":".join(p.zfill(2) for p in mac.split(":")).lower()
    return devs


def vendor(mac: str) -> str:
    try:
        req = urllib.request.Request(f"https://api.macvendors.com/{mac}",
                                     headers={"User-Agent": "JARVIS-LAN"})
        return urllib.request.urlopen(req, timeout=5).read().decode().strip()
    except Exception:
        return ""


def rdns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def scan_ports(ip: str) -> list[str]:
    open_: list[str] = []
    for p, name in _PORTS.items():
        s = socket.socket()
        s.settimeout(0.6)
        try:
            if s.connect_ex((ip, p)) == 0:
                open_.append(f"{p}/{name}")
        except Exception:
            pass
        finally:
            s.close()
    return open_


def classify(ip: str, host: str, vend: str, ports: list[str]) -> tuple[str, str, str]:
    """(type, risk, note) for an UNKNOWN device, from heuristics."""
    h, v = host.lower(), vend.lower()
    pnums = " ".join(ports)
    if "554/RTSP" in pnums or "camera" in h or "doorbird" in h:
        return "camera", "high", "Caméra/flux vidéo détecté (RTSP) — vérifier les identifiants."
    if "android" in h:
        return "mobile", "watch", "Appareil Android auto-déclaré — sous surveillance."
    if "5555/ADB" in pnums:
        return "mobile", "high", "ADB (5555) ouvert — accès debug Android exposé !"
    if any(x in v for x in ("tuya", "xiaomi", "espressif", "tp-link", "sonoff")):
        return "iot", "medium", "Objet connecté (cloud). Isoler sur réseau invité de préférence."
    if "tv" in h or "webos" in h or "8008/Cast" in pnums:
        return "tv", "medium", "Téléviseur connecté."
    return "unknown", "watch", "Appareil inconnu non répertorié — sous surveillance."


def build_inventory(do_scan: bool) -> dict:
    labels = load_labels()
    allow = load_allow()
    cache = {}
    if _INVENTORY.exists():
        try:
            cache = {d["ip"]: d for d in json.loads(_INVENTORY.read_text())["devices"]}
        except Exception:
            cache = {}

    if do_scan:
        print("• Balayage ping du réseau…", file=sys.stderr)
        ping_sweep(_SUBNET)
    macs = arp_table(_SUBNET) if do_scan else {ip: cache[ip].get("mac", "") for ip in cache}

    devices = []
    items = list(macs.items())
    print(f"• {len(items)} appareils — résolution constructeur/ports…", file=sys.stderr)
    for ip, mac in sorted(items, key=lambda kv: ipaddress.ip_address(kv[0])):
        prev = cache.get(ip, {})
        vend = prev.get("vendor") or (vendor(mac) if do_scan else "")
        if do_scan and not prev.get("vendor"):
            time.sleep(1.2)  # macvendors rate limit
        host = (rdns(ip) if do_scan else prev.get("host", "")) or prev.get("host", "")
        ports = scan_ports(ip) if do_scan else prev.get("ports", [])
        meta = _KNOWN.get(ip)
        if meta:
            dtype, risk, note = meta["type"], meta["risk"], meta["note"]
        else:
            dtype, risk, note = classify(ip, host, vend, ports)
        trusted = ip in allow
        if not trusted and risk == "low":
            risk = "watch"
        devices.append({
            "ip": ip, "mac": mac, "vendor": vend, "host": host,
            "label": labels.get(ip, ""), "ports": ports,
            "type": dtype, "icon": _TYPE_ICON.get(dtype, "❓"),
            "risk": risk, "note": note, "trusted": trusted,
        })

    inv = {"scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "subnet": _SUBNET, "count": len(devices), "devices": devices}
    _INVENTORY.write_text(json.dumps(inv, indent=1, ensure_ascii=False))
    return inv


def render_html(inv: dict) -> None:
    tpl = _TEMPLATE.read_text()
    html = tpl.replace("/*__DATA__*/null", json.dumps(inv, ensure_ascii=False))
    _HTML.write_text(html)
    print(f"✓ Dashboard : {_HTML}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS LAN scanner + dashboard")
    ap.add_argument("--no-scan", action="store_true", help="rebuild HTML from cache only")
    ap.add_argument("--open", action="store_true", help="open the dashboard after building")
    args = ap.parse_args()

    inv = build_inventory(do_scan=not args.no_scan)
    render_html(inv)
    print(f"\n{inv['count']} appareils — {inv['scanned_at']}")
    for d in inv["devices"]:
        flag = "✓trust" if d["trusted"] else "•watch"
        name = d["label"] or d["host"] or d["vendor"] or "?"
        print(f"  [{d['risk']:6s}] {flag} {d['ip']:14s} {d['icon']} {name}")
    if args.open:
        subprocess.run(["open", "-a", "Google Chrome", str(_HTML)],
                       capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
