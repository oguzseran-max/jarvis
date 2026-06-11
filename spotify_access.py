"""
JARVIS Spotify Access — voice control of Spotify playback.

JARVIS starts/controls music by voice and targets a specific Spotify Connect
device (e.g. your Wi-Fi Marshall, which already feeds both speakers), so
"play my playlist" lands on the right speakers automatically.

Requires Spotify Premium (the Web API only controls playback for Premium
accounts). Auth uses the Authorization Code flow: you create a Spotify app
once, run `python -m spotify_access --auth` to capture a refresh token, then
JARVIS exchanges it for short-lived access tokens automatically.

Configuration (see .env.example):
  SPOTIFY_CLIENT_ID       — from your Spotify developer app
  SPOTIFY_CLIENT_SECRET   — from your Spotify developer app
  SPOTIFY_REFRESH_TOKEN   — obtained via `python -m spotify_access --auth`
  SPOTIFY_DEVICE_NAME     — name of the Connect device to play on (the Marshall)
  SPOTIFY_REDIRECT_URI    — only for the auth helper (default below)
"""

import base64
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("jarvis.spotify")


def _load_env_file() -> None:
    """Load SPOTIFY_* (and any) vars from a sibling .env if present.

    Dependency-free and non-destructive: never overrides a variable already set
    in the real environment. This makes `python -m spotify_access` work on its
    own, without requiring python-dotenv or the server to have loaded .env.
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

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "").strip()
DEVICE_NAME = os.getenv("SPOTIFY_DEVICE_NAME", "").strip()
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()

# Scopes needed to read devices/playback, control playback, and read the user's
# top artists/tracks (user-top-read — used by tools/sync_music_vocab.py to prime
# Whisper with the music names he actually listens to).
SCOPES = "user-read-playback-state user-modify-playback-state user-top-read"

TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTH_URL = "https://accounts.spotify.com/authorize"
API_BASE = "https://api.spotify.com/v1"
TIMEOUT = 12.0

# Cached access token: {"token": str, "expires_at": float}
_token_cache: dict = {"token": "", "expires_at": 0.0}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PlaybackResult:
    ok: bool
    detail: str  # human-readable: a track/playlist name, or an error reason


@dataclass
class NowPlaying:
    name: str
    artist: str
    is_playing: bool


def is_configured() -> bool:
    """True when the app credentials and a refresh token are all present."""
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


def _basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

async def get_access_token() -> str | None:
    """Return a valid access token, refreshing via the refresh token if needed."""
    if not is_configured():
        return None
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                TOKEN_URL,
                headers={"Authorization": _basic_auth_header()},
                data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
            )
        if resp.status_code != 200:
            log.warning(f"Spotify token refresh failed {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        token = data.get("access_token", "")
        # Refresh a little early (60s margin).
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
        return token or None
    except Exception as e:
        log.warning(f"Spotify token refresh error: {e}")
        return None


async def _api(method: str, path: str, token: str, **kwargs):
    """Authenticated Spotify Web API call. Returns the httpx.Response or None."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await client.request(
                method,
                f"{API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
    except Exception as e:
        log.warning(f"Spotify API {method} {path} error: {e}")
        return None


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

async def get_devices(token: str | None = None) -> list[dict]:
    """List available Spotify Connect devices."""
    token = token or await get_access_token()
    if not token:
        return []
    resp = await _api("GET", "/me/player/devices", token)
    if not resp or resp.status_code != 200:
        return []
    return resp.json().get("devices", [])


# ---------------------------------------------------------------------------
# Top artists / tracks (for priming Whisper with the user's actual music)
# ---------------------------------------------------------------------------

async def get_top_artists(limit: int = 30, time_range: str = "medium_term",
                          token: str | None = None) -> list[str]:
    """The user's most-listened artist names. Needs the user-top-read scope.

    time_range: short_term (~4 weeks) | medium_term (~6 months) | long_term.
    """
    token = token or await get_access_token()
    if not token:
        return []
    resp = await _api("GET", f"/me/top/artists?limit={min(limit, 50)}&time_range={time_range}", token)
    if not resp or resp.status_code != 200:
        if resp is not None and resp.status_code == 403:
            log.warning("Spotify top-artists 403 — re-auth needed to grant user-top-read")
        return []
    return [a["name"] for a in resp.json().get("items", []) if a.get("name")]


async def get_top_tracks(limit: int = 30, time_range: str = "medium_term",
                         token: str | None = None) -> list[dict]:
    """The user's most-played tracks as {name, artist}. Needs user-top-read."""
    token = token or await get_access_token()
    if not token:
        return []
    resp = await _api("GET", f"/me/top/tracks?limit={min(limit, 50)}&time_range={time_range}", token)
    if not resp or resp.status_code != 200:
        return []
    out = []
    for t in resp.json().get("items", []):
        name = t.get("name")
        artists = t.get("artists", [])
        artist = artists[0]["name"] if artists and artists[0].get("name") else ""
        if name:
            out.append({"name": name, "artist": artist})
    return out


def _match_device(devices: list[dict], name: str) -> dict | None:
    """Case-insensitive match: exact name first, then substring."""
    if not name:
        return None
    low = name.lower()
    for d in devices:
        if d.get("name", "").lower() == low:
            return d
    for d in devices:
        if low in d.get("name", "").lower():
            return d
    return None


async def _resolve_device(token: str, device_name: str | None) -> tuple[str | None, list[str]]:
    """Return (device_id, available_names). Falls back to the active device."""
    devices = await get_devices(token)
    names = [d.get("name", "") for d in devices]
    target = device_name or DEVICE_NAME
    match = _match_device(devices, target) if target else None
    if not match:
        # Fall back to whatever is currently active.
        match = next((d for d in devices if d.get("is_active")), None)
    return (match.get("id") if match else None), names


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def _search(token: str, query: str) -> tuple[str, str, str] | None:
    """Resolve a free-text query to (play_mode, uri, label).

    play_mode is "uris" for a single track or "context_uri" for an
    artist/album/playlist. An exact name match on artist/playlist/album wins;
    otherwise the top track is used (the common "play a song" case).
    """
    resp = await _api(
        "GET", "/search", token,
        params={"q": query, "type": "track,artist,album,playlist", "limit": 3},
    )
    if not resp or resp.status_code != 200:
        return None
    data = resp.json()
    low = query.lower()

    def first(items):
        return items[0] if items else None

    artists = data.get("artists", {}).get("items", [])
    playlists = data.get("playlists", {}).get("items", [])
    albums = data.get("albums", {}).get("items", [])
    tracks = data.get("tracks", {}).get("items", [])

    # Exact-name matches on a "context" type take priority.
    for items, label_kind in ((playlists, "playlist"), (artists, "artist"), (albums, "album")):
        for it in items:
            if it and it.get("name", "").lower() == low:
                return "context_uri", it["uri"], it["name"]

    # Otherwise prefer a track (most "play X" requests are songs).
    t = first(tracks)
    if t:
        artist = t["artists"][0]["name"] if t.get("artists") else ""
        label = f"{t['name']} by {artist}" if artist else t["name"]
        return "uris", t["uri"], label

    # Last resort: any context result.
    for items in (playlists, artists, albums):
        it = first(items)
        if it:
            return "context_uri", it["uri"], it["name"]
    return None


# ---------------------------------------------------------------------------
# Playback control
# ---------------------------------------------------------------------------

async def play(query: str | None = None, device_name: str | None = None) -> PlaybackResult:
    """Start or resume playback on the target device.

    With a query, searches and plays the best match. Without one, resumes the
    current track. Always targets DEVICE_NAME (the Marshall) unless overridden.
    """
    token = await get_access_token()
    if not token:
        return PlaybackResult(False, "Spotify isn't connected")

    device_id, names = await _resolve_device(token, device_name)
    if not device_id:
        target = device_name or DEVICE_NAME or "a device"
        avail = ", ".join(n for n in names if n) or "none"
        return PlaybackResult(False, f"couldn't find {target} (available: {avail})")

    body: dict = {}
    label = ""
    if query:
        found = await _search(token, query)
        if not found:
            return PlaybackResult(False, f"couldn't find anything for '{query}'")
        mode, uri, label = found
        body = {mode: [uri]} if mode == "uris" else {mode: uri}

    resp = await _api("PUT", "/me/player/play", token, params={"device_id": device_id}, json=body)
    if not resp or resp.status_code not in (200, 202, 204):
        code = resp.status_code if resp else "no response"
        return PlaybackResult(False, f"playback failed ({code})")
    return PlaybackResult(True, label or "resumed")


async def pause() -> PlaybackResult:
    token = await get_access_token()
    if not token:
        return PlaybackResult(False, "Spotify isn't connected")
    resp = await _api("PUT", "/me/player/pause", token)
    if not resp or resp.status_code not in (200, 202, 204):
        return PlaybackResult(False, "pause failed")
    return PlaybackResult(True, "paused")


async def next_track() -> PlaybackResult:
    token = await get_access_token()
    if not token:
        return PlaybackResult(False, "Spotify isn't connected")
    resp = await _api("POST", "/me/player/next", token)
    if not resp or resp.status_code not in (200, 202, 204):
        return PlaybackResult(False, "skip failed")
    return PlaybackResult(True, "skipped")


async def current_track() -> NowPlaying | None:
    token = await get_access_token()
    if not token:
        return None
    resp = await _api("GET", "/me/player/currently-playing", token)
    if not resp or resp.status_code != 200 or not resp.content:
        return None
    data = resp.json()
    item = data.get("item") or {}
    if not item:
        return None
    artist = item["artists"][0]["name"] if item.get("artists") else ""
    return NowPlaying(name=item.get("name", ""), artist=artist, is_playing=bool(data.get("is_playing")))


# ---------------------------------------------------------------------------
# Voice-command parsing — map a free-text utterance to an action + query.
# ---------------------------------------------------------------------------

_MUSIC_WORDS = ("musique", "music", "müzik", "chanson", "morceau", "şarkı")
_PAUSE_PHRASES = (
    "coupe la musique", "arrête la musique", "arrete la musique", "stop la musique",
    "stop the music", "stop music", "turn off the music", "pause la musique",
    "mets en pause", "mets sur pause", "metz en pause", "pause music",
    "éteins la musique", "eteins la musique", "müziği durdur", "müziği kapat",
)
_NEXT_PHRASES = (
    "musique suivante", "chanson suivante", "prochaine chanson", "morceau suivant",
    "titre suivant", "chanson d'après", "next track", "next song", "passe la chanson",
    "passe la musique", "chanson d'apres", "sonraki şarkı", "sonraki",
)
_PLAY_VERBS = ("lance", "mets", "met", "joue", "écoute", "ecoute", "balance", "play", "mettre")
# Phrases stripped out when extracting the search query.
_FILLER = (
    "s'il te plaît", "s il te plait", "stp", "peux-tu", "peux tu", "tu peux", "pour moi",
    "un peu de", "la musique de", "la musique", "de la musique", "de musique",
    "la chanson de", "la chanson", "le titre de", "le morceau de", "le morceau",
    "put on", "some music", "the song", "the music", "go to hell", "please",
)


def parse_command(text: str):
    """Map a spoken utterance to a Spotify action.

    Returns a tuple ``(action, query)`` where action is one of
    ``"play" | "pause" | "next" | "resume"`` (query only set for ``play``),
    or ``None`` if the utterance isn't a music command at all.
    """
    t = (text or "").lower()
    for ch in ",.!?;:\"'":
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    pad = f" {t} "

    if any(p in pad for p in _PAUSE_PHRASES):
        return ("pause", None)
    if any(p in pad for p in _NEXT_PHRASES):
        return ("next", None)

    # Play requires an explicit play VERB ("mets", "joue", …). A bare mention of
    # "musique" must NOT auto-play — otherwise complaints ("je ne t'ai pas demandé
    # de changer de musique") or lyrics the mic caught trigger random playback.
    # When the verb is garbled by speech-to-text the LLM's [ACTION:MUSIC] fallback
    # handles it instead.
    has_verb = any(f" {v} " in pad or t.startswith(f"{v} ") for v in _PLAY_VERBS)
    if not has_verb:
        return None
    # Only fast-path short imperatives; longer, sentence-like utterances (questions,
    # complaints, mis-heard song lyrics) go to the LLM, which judges intent in context.
    if len(t.split()) > 7:
        return None

    # Build the search query: cut everything up to and including the last play
    # verb (so "paris on lance la musique de X" -> "la musique de X"), then drop
    # the music filler words, leaving just "X".
    q = t
    cut = -1
    for v in _PLAY_VERBS:
        idx = q.rfind(f" {v} ")
        if idx >= 0:
            cut = max(cut, idx + len(v) + 2)
        elif q.startswith(f"{v} "):
            cut = max(cut, len(v) + 1)
    if cut > 0:
        q = q[cut:]
    for f in _FILLER:
        q = q.replace(f, " ")
    # Drop leading stop-words/articles/music words (token by token) so
    # "de la musique" / "la chanson" collapse to nothing -> just resume.
    _STOP = {"de", "du", "des", "la", "le", "les", "l", "d", "un", "une",
             "the", "a", "of", "on", "moi", *(_MUSIC_WORDS)}
    toks = q.split()
    while toks and toks[0] in _STOP:
        toks.pop(0)
    q = " ".join(toks).strip()
    # Nothing meaningful left => just resume/start playback.
    if not q:
        return ("resume", None)
    return ("play", q)


# ---------------------------------------------------------------------------
# Voice formatting (British butler tone, 1-2 sentences)
# ---------------------------------------------------------------------------

def format_play(result: PlaybackResult) -> str:
    if result.ok:
        if result.detail in ("resumed", ""):
            return "Resuming, sir."
        return f"Playing {result.detail}, sir."
    if result.detail == "Spotify isn't connected":
        return "Spotify isn't connected yet, sir. Add the credentials and I'll handle the rest."
    return f"I couldn't start playback, sir — {result.detail}."


def format_now_playing(now: NowPlaying | None) -> str:
    if not now or not now.name:
        return "Nothing is playing at the moment, sir."
    by = f" by {now.artist}" if now.artist else ""
    verb = "Currently playing" if now.is_playing else "Paused on"
    return f"{verb} {now.name}{by}, sir."


# ---------------------------------------------------------------------------
# Auth helper + diagnostic CLI — `python -m spotify_access [--auth]`
# ---------------------------------------------------------------------------

def _save_refresh_token(token: str) -> str | None:
    """Write/update SPOTIFY_REFRESH_TOKEN in the repo-root .env. Returns path or None."""
    env_path = Path(__file__).parent / ".env"
    new_line = f"SPOTIFY_REFRESH_TOKEN={token}"
    try:
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("SPOTIFY_REFRESH_TOKEN=") and not stripped.startswith("#"):
                    lines[i] = new_line
                    break
            else:
                lines.append(new_line)
            env_path.write_text("\n".join(lines) + "\n")
        else:
            env_path.write_text(new_line + "\n")
        return str(env_path)
    except Exception as e:
        log.warning(f"Could not write .env automatically: {e}")
        return None


def _run_auth_flow() -> int:
    """One-time Authorization Code flow to capture a refresh token."""
    import urllib.parse
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first (see .env.example).")
        return 2

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8888

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    })
    authorize = f"{AUTH_URL}?{params}"
    print("Opening your browser to authorize Spotify…")
    print(f"If it doesn't open, visit:\n  {authorize}\n")
    print(f"(Make sure {REDIRECT_URI} is added as a Redirect URI in your Spotify app settings.)\n")
    webbrowser.open(authorize)

    code_box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            code_box["code"] = urllib.parse.parse_qs(qs).get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>JARVIS: Spotify connected. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    print(f"Waiting for the Spotify redirect on {host}:{port} …")
    server = HTTPServer((host, port), Handler)
    server.handle_request()  # serve exactly one request (the callback)
    code = code_box.get("code")
    if not code:
        print("No authorization code received.")
        return 1

    resp = httpx.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth_header()},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"Token exchange failed {resp.status_code}: {resp.text[:300]}")
        return 1
    refresh = resp.json().get("refresh_token", "")
    if not refresh:
        print("No refresh token returned.")
        return 1

    saved = _save_refresh_token(refresh)
    if saved:
        print(f"\n✅ Success — saved SPOTIFY_REFRESH_TOKEN to {saved}")
        print("You're all set. Verify with:  python -m spotify_access")
    else:
        print("\n✅ Success. Add this line to your .env manually:\n")
        print(f"SPOTIFY_REFRESH_TOKEN={refresh}\n")
    return 0


async def _diagnose() -> int:
    """Print config status, available Connect devices, and current track."""
    print("JARVIS · Spotify — local check\n")
    print("Configuration:")
    print(f"  credentials : {'set' if (CLIENT_ID and CLIENT_SECRET) else 'MISSING (client id/secret)'}")
    print(f"  refresh tok : {'set' if REFRESH_TOKEN else 'MISSING (run --auth)'}")
    print(f"  device name : {DEVICE_NAME or '(not set — will use the active device)'}\n")

    if not is_configured():
        print("Not fully configured yet. See .env.example, then run: python -m spotify_access --auth")
        return 2

    token = await get_access_token()
    if not token:
        print("Could not obtain an access token — check credentials / refresh token.")
        return 1

    devices = await get_devices(token)
    print("Spotify Connect devices:")
    if not devices:
        print("  (none online — open Spotify on the Marshall / a device so it appears)")
    for d in devices:
        flags = []
        if d.get("is_active"):
            flags.append("active")
        if DEVICE_NAME and d.get("name", "").lower() == DEVICE_NAME.lower():
            flags.append("← target")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  - {d.get('name')} ({d.get('type')}){suffix}")

    now = await current_track()
    print("\n" + format_now_playing(now))
    return 0


if __name__ == "__main__":
    import asyncio as _asyncio
    import sys as _sys

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if "--auth" in _sys.argv[1:]:
        _sys.exit(_run_auth_flow())
    _sys.exit(_asyncio.run(_diagnose()))
