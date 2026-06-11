"""
JARVIS Morning Briefing — gather the facts for the post-startup briefing.

Pulls together the data sources that aren't already in server.py:
  * traffic   — Google Directions API (live, traffic-aware ETA)
  * weather   — Open-Meteo daily forecast (no key) for clothing advice
  * portfolio — runs the user's track.py to refresh prices, parses the totals

Mail, calendar and crypto-sentiment reuse the existing server.py helpers.
Each function returns plain facts; server.py composes them into a spoken,
language-appropriate briefing via the LLM.
"""

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("jarvis.briefing")

# Home → office, fixed for the user.
HOME_ADDRESS = os.getenv("BRIEFING_HOME", "1146G Route des Mermes, 74140 Veigy-Foncenex, France")
OFFICE_ADDRESS = os.getenv("BRIEFING_OFFICE", "Barclays Bank, 28-20 Chemin Grange-Canal, 1204 Geneva, Switzerland")

# Veigy-Foncenex coordinates for the weather forecast.
WEATHER_LAT = float(os.getenv("BRIEFING_LAT", "46.2755"))
WEATHER_LON = float(os.getenv("BRIEFING_LON", "6.2925"))

PORTFOLIO_DIR = Path(os.getenv(
    "BRIEFING_PORTFOLIO_DIR",
    str(Path.home() / "Desktop" / "research-balanced-investment-opportunities"),
))


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


# ---- Traffic -------------------------------------------------------------

async def _google_routes_traffic() -> dict:
    """Live traffic-aware ETA home → office via the Google Routes API.

    The legacy Directions API is deprecated for new keys; Routes API
    (routes.googleapis.com/.../v2:computeRoutes) is the current endpoint. Enable
    "Routes API" for the key in Google Cloud Console.
    """
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        return {"ok": False, "reason": "no_key"}

    def _call():
        body = json.dumps({
            "origin": {"address": HOME_ADDRESS},
            "destination": {"address": OFFICE_ADDRESS},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
        }).encode()
        req = urllib.request.Request(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters",
            },
        )
        return json.loads(urllib.request.urlopen(req, timeout=15).read())

    try:
        data = await asyncio.to_thread(_call)
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = e.read().decode(errors="ignore")[:300]
        except Exception:
            pass
        log.warning(f"traffic fetch failed: {e} {msg}")
        return {"ok": False, "reason": msg or str(e)}
    except Exception as e:
        log.warning(f"traffic fetch failed: {e}")
        return {"ok": False, "reason": str(e)}

    routes = data.get("routes") or []
    if not routes:
        return {"ok": False, "reason": "no_route"}
    r0 = routes[0]

    def _secs(v) -> int:
        try:
            return int(str(v).rstrip("s"))
        except Exception:
            return 0

    traffic_min = _secs(r0.get("duration")) // 60
    normal_min = _secs(r0.get("staticDuration") or r0.get("duration")) // 60
    delay = traffic_min - normal_min
    meters = r0.get("distanceMeters", 0)
    if delay >= 8:
        condition = "heavy traffic"
    elif delay >= 3:
        condition = "moderate traffic"
    else:
        condition = "clear roads"
    return {
        "ok": True,
        "distance": f"{meters / 1000:.1f} km",
        "eta_min": traffic_min,
        "normal_min": normal_min,
        "delay_min": delay,
        "condition": condition,
        "route": "the usual route",
        "warnings": [],
    }


# Keyless fallback: OpenStreetMap (Nominatim geocoding) + OSRM routing. No live
# traffic, but a working ETA when the Google key/Routes-API isn't available.
_GEO_CACHE: dict[str, tuple] = {}


def _geocode_sync(addr: str):
    if addr in _GEO_CACHE:
        return _GEO_CACHE[addr]
    # Nominatim resolves streets better than business names. Try the full string,
    # then drop a leading business-name segment, then just the street tail.
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    variants = [addr]
    if len(parts) > 2:
        variants.append(", ".join(parts[1:]))    # drop leading business name
        variants.append(", ".join(parts[-3:]))   # street + city + country
    for q in variants:
        try:
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": q, "format": "json", "limit": 1})
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-briefing/1.0 (personal use)"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if data:
                coords = (float(data[0]["lat"]), float(data[0]["lon"]))
                _GEO_CACHE[addr] = coords
                return coords
        except Exception:
            continue
    return None


def _osrm_sync(o: tuple, d: tuple):
    # OSRM expects lon,lat order
    url = (f"https://router.project-osrm.org/route/v1/driving/"
           f"{o[1]},{o[0]};{d[1]},{d[0]}?overview=false")
    data = json.loads(_get(url))
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    return r["duration"], r["distance"]  # seconds, meters


async def _osrm_fallback() -> dict:
    loop = asyncio.get_event_loop()
    try:
        home = await loop.run_in_executor(None, _geocode_sync, HOME_ADDRESS)
        office = await loop.run_in_executor(None, _geocode_sync, OFFICE_ADDRESS)
        if not home or not office:
            return {"ok": False, "reason": "geocode_failed"}
        res = await loop.run_in_executor(None, _osrm_sync, home, office)
        if not res:
            return {"ok": False, "reason": "osrm_failed"}
        secs, meters = res
        mins = int(secs // 60)
        return {
            "ok": True,
            "distance": f"{meters / 1000:.1f} km",
            "eta_min": mins,
            "normal_min": mins,
            "delay_min": 0,
            "condition": "free-flow estimate (no live traffic)",
            "route": "the usual route",
            "warnings": [],
            "live_traffic": False,
        }
    except Exception as e:
        log.warning(f"osrm fallback failed: {e}")
        return {"ok": False, "reason": str(e)}


async def get_traffic() -> dict:
    """Home → office ETA. Prefer Google Routes (live traffic); if the key/API
    isn't available, fall back to a keyless OSRM estimate so the commute still
    works."""
    res = await _google_routes_traffic()
    if res.get("ok"):
        return res
    log.info(f"Google traffic unavailable ({str(res.get('reason'))[:80]}) — using OSRM fallback")
    fb = await _osrm_fallback()
    return fb if fb.get("ok") else res


# ---- Weather -------------------------------------------------------------

_WCODE = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


async def get_weather() -> dict:
    """Today's forecast (high/low, conditions, rain chance) for the home area."""
    def _call():
        params = {
            "latitude": WEATHER_LAT, "longitude": WEATHER_LON,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
            "current": "temperature_2m,weathercode",
            "timezone": "auto", "forecast_days": 1,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        return json.loads(_get(url))

    try:
        d = await asyncio.to_thread(_call)
        daily = d["daily"]
        code = daily["weathercode"][0]
        return {
            "ok": True,
            "high_c": round(daily["temperature_2m_max"][0]),
            "low_c": round(daily["temperature_2m_min"][0]),
            "current_c": round(d.get("current", {}).get("temperature_2m", daily["temperature_2m_max"][0])),
            "rain_chance": daily["precipitation_probability_max"][0],
            "conditions": _WCODE.get(code, "mixed conditions"),
        }
    except Exception as e:
        log.warning(f"weather fetch failed: {e}")
        return {"ok": False, "reason": str(e)}


# ---- Portfolio -----------------------------------------------------------

async def get_portfolio() -> dict:
    """Refresh prices via the user's track.py, parse totals + movers."""
    script = PORTFOLIO_DIR / "track.py"
    if not script.exists():
        return {"ok": False, "reason": "no_script"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(script),
            cwd=str(PORTFOLIO_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as e:
        log.warning(f"portfolio refresh failed: {e}")
        return {"ok": False, "reason": str(e)}

    text = out.decode(errors="replace")
    positions = []
    total_value = total_gain_pct = None
    for line in text.splitlines():
        # e.g. "SPCE  162.25  $7.83  $1,269.90  $267.16  +26.6%"
        m = re.match(r"\s*([A-Z]{2,6})\s+[\d.]+\s+\$[\d,]+\.\d+\s+\$[\d,\-]+\.\d+\s+\$[\d,\-]+\.\d+\s+([+\-][\d.]+)%", line)
        if m:
            positions.append({"ticker": m.group(1), "gain_pct": float(m.group(2))})
        t = re.search(r"TOTAL\s+\$([\d,]+\.\d+)\s+\$[\d,\-]+\.\d+\s+([+\-][\d.]+)%", line)
        if t:
            total_value = t.group(1)
            total_gain_pct = float(t.group(2))

    movers = sorted(positions, key=lambda p: p["gain_pct"], reverse=True)
    return {
        "ok": total_value is not None,
        "total_value": total_value,
        "total_gain_pct": total_gain_pct,
        "best": movers[0] if movers else None,
        "worst": movers[-1] if movers else None,
        "dashboard": str(PORTFOLIO_DIR / "dashboard.html"),
    }


# ---- Crypto sentiment (fast, concurrent) ---------------------------------

_POS = ["adoption", "launch", "partnership", "etf", "rally", "breakthrough",
        "growth", "approval", "bullish", "surge", "adopts", "soar", "gains"]
_NEG = ["crash", "exploit", "hack", "delay", "liquidation", "depeg", "bearish",
        "decline", "setback", "breach", "drop", "plunge", "selloff", "lawsuit"]
_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
    "https://bitcoinist.com/feed/",
    "https://www.newsbtc.com/feed/",
    "https://cryptonews.com/news/feed/",
]


def _fetch_feed(url: str) -> list[str]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(_get(url, timeout=8))
        out = []
        for it in root.findall(".//item"):
            title = it.findtext("title") or ""
            desc = it.findtext("description") or ""
            out.append((title + " " + desc).lower())
        return out
    except Exception:
        return []


async def get_sentiment() -> dict:
    """Crypto news sentiment — fetches all feeds concurrently (~4s vs ~20s)."""
    results = await asyncio.gather(*[asyncio.to_thread(_fetch_feed, u) for u in _FEEDS])
    texts = [t for sub in results for t in sub]
    if not texts:
        return {"ok": False}
    total = pos = neg = 0
    for txt in texts:
        p = sum(1 for w in _POS if w in txt)
        n = sum(1 for w in _NEG if w in txt)
        total += 1 if p > n else -1 if n > p else 0
    count = len(texts)
    score = total / count if count else 0.0
    mood = "bullish" if score > 0.1 else "bearish" if score < -0.1 else "neutral"
    return {"ok": True, "score": round(score, 2), "mood": mood, "articles": count}


async def open_dashboard_window() -> None:
    """Disabled at user request — the briefing no longer opens the dashboard
    window. Kept as a no-op so any stray caller is harmless."""
    return
    # (unreachable) original behaviour below
    dash = PORTFOLIO_DIR / "dashboard.html"
    if not dash.exists():
        return
    url = f"file://{dash}"
    # Wide enough for the 8-column table + long position names, tall enough for
    # all rows + totals + footer. Clamped to the main screen so it never exceeds it.
    script = f'''
tell application "Finder" to set sb to bounds of window of desktop
set screenW to item 3 of sb
set screenH to item 4 of sb
set winW to 1040
set winH to 760
if winW > (screenW - 40) then set winW to (screenW - 40)
if winH > (screenH - 80) then set winH to (screenH - 80)
set x1 to 40
set y1 to 60
tell application "Google Chrome"
    make new window
    set URL of active tab of front window to "{url}"
    set bounds of front window to {{x1, y1, x1 + winW, y1 + winH}}
end tell
'''
    try:
        await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        log.warning(f"open dashboard window failed: {e}")
