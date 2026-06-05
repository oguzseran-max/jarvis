"""Unit tests for spotify_access — voice control of Spotify playback.

No network: the Spotify Web API is faked with a small URL-routing async client.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import spotify_access as sp
from spotify_access import (
    NowPlaying,
    PlaybackResult,
    _match_device,
    format_now_playing,
    format_play,
)


# --- Fake async client routing by (method, path substring) -----------------

class _FakeResp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = ""
        self.content = b"x" if payload else b""

    def json(self):
        return self._payload


class _Router:
    """Fake httpx.AsyncClient: matches requests against configured routes."""
    def __init__(self, routes):
        self._routes = routes  # list of (method, substr, _FakeResp)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, method, url):
        for m, sub, resp in self._routes:
            if m == method and sub in url:
                return resp
        raise AssertionError(f"no fake route for {method} {url}")

    async def request(self, method, url, **k):
        return self._match(method, url)

    async def post(self, url, **k):
        return self._match("POST", url)

    async def get(self, url, **k):
        return self._match("GET", url)


def _install(monkeypatch, routes):
    monkeypatch.setattr(sp.httpx, "AsyncClient", lambda *a, **k: _Router(routes))


@pytest.fixture(autouse=True)
def _reset_token():
    sp._token_cache["token"] = ""
    sp._token_cache["expires_at"] = 0.0
    yield


# --- Configuration ---------------------------------------------------------

def test_is_configured_true(monkeypatch):
    monkeypatch.setattr(sp, "CLIENT_ID", "id")
    monkeypatch.setattr(sp, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(sp, "REFRESH_TOKEN", "refresh")
    assert sp.is_configured() is True


def test_is_configured_false(monkeypatch):
    monkeypatch.setattr(sp, "CLIENT_ID", "")
    assert sp.is_configured() is False


# --- Token management ------------------------------------------------------

@pytest.mark.asyncio
async def test_get_access_token_refreshes(monkeypatch):
    monkeypatch.setattr(sp, "CLIENT_ID", "id")
    monkeypatch.setattr(sp, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(sp, "REFRESH_TOKEN", "refresh")
    _install(monkeypatch, [("POST", "accounts.spotify.com", _FakeResp({"access_token": "AT", "expires_in": 3600}))])
    assert await sp.get_access_token() == "AT"
    assert sp._token_cache["token"] == "AT" and sp._token_cache["expires_at"] > time.time()


@pytest.mark.asyncio
async def test_get_access_token_uses_cache(monkeypatch):
    monkeypatch.setattr(sp, "CLIENT_ID", "id")
    monkeypatch.setattr(sp, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(sp, "REFRESH_TOKEN", "refresh")
    sp._token_cache["token"] = "CACHED"
    sp._token_cache["expires_at"] = time.time() + 999
    # No routes installed: a network call would raise, proving the cache is used.
    _install(monkeypatch, [])
    assert await sp.get_access_token() == "CACHED"


@pytest.mark.asyncio
async def test_get_access_token_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sp, "CLIENT_ID", "")
    assert await sp.get_access_token() is None


# --- Device matching -------------------------------------------------------

def test_match_device_exact_then_substring():
    devices = [{"name": "Marshall Stanmore", "id": "1"}, {"name": "Kitchen", "id": "2"}]
    assert _match_device(devices, "kitchen")["id"] == "2"          # exact (case-insensitive)
    assert _match_device(devices, "marshall")["id"] == "1"          # substring
    assert _match_device(devices, "bathroom") is None
    assert _match_device(devices, "") is None


# --- Search ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_prefers_exact_artist_context(monkeypatch):
    payload = {
        "artists": {"items": [{"name": "Daft Punk", "uri": "spotify:artist:dp"}]},
        "tracks": {"items": [{"name": "Some Song", "uri": "spotify:track:x", "artists": [{"name": "X"}]}]},
        "albums": {"items": []}, "playlists": {"items": []},
    }
    _install(monkeypatch, [("GET", "/search", _FakeResp(payload))])
    mode, uri, label = await sp._search("tok", "Daft Punk")
    assert mode == "context_uri" and uri == "spotify:artist:dp" and label == "Daft Punk"


@pytest.mark.asyncio
async def test_search_defaults_to_track(monkeypatch):
    payload = {
        "artists": {"items": []}, "albums": {"items": []}, "playlists": {"items": []},
        "tracks": {"items": [{"name": "Get Lucky", "uri": "spotify:track:gl", "artists": [{"name": "Daft Punk"}]}]},
    }
    _install(monkeypatch, [("GET", "/search", _FakeResp(payload))])
    mode, uri, label = await sp._search("tok", "get lucky")
    assert mode == "uris" and uri == "spotify:track:gl" and label == "Get Lucky de Daft Punk"


# --- Playback control ------------------------------------------------------

@pytest.mark.asyncio
async def test_play_targets_device_and_track(monkeypatch):
    monkeypatch.setattr(sp, "get_access_token", lambda: _async("tok"))
    monkeypatch.setattr(sp, "DEVICE_NAME", "Marshall")
    devices = {"devices": [{"name": "Marshall", "id": "dev1", "is_active": False, "type": "Speaker"}]}
    track = {"artists": {"items": []}, "albums": {"items": []}, "playlists": {"items": []},
             "tracks": {"items": [{"name": "Get Lucky", "uri": "spotify:track:gl", "artists": [{"name": "Daft Punk"}]}]}}
    _install(monkeypatch, [
        ("GET", "/me/player/devices", _FakeResp(devices)),
        ("GET", "/search", _FakeResp(track)),
        ("PUT", "/me/player/play", _FakeResp(status_code=204)),
    ])
    result = await sp.play("get lucky")
    assert result.ok and result.detail == "Get Lucky de Daft Punk"


@pytest.mark.asyncio
async def test_play_device_not_found(monkeypatch):
    monkeypatch.setattr(sp, "get_access_token", lambda: _async("tok"))
    monkeypatch.setattr(sp, "DEVICE_NAME", "Marshall")
    devices = {"devices": [{"name": "Kitchen", "id": "k", "is_active": False}]}
    _install(monkeypatch, [("GET", "/me/player/devices", _FakeResp(devices))])
    result = await sp.play("anything")
    assert result.ok is False and "Marshall" in result.detail and "Kitchen" in result.detail
    assert format_play(result).startswith("Je n'ai pas pu lancer la lecture, mon amour")


@pytest.mark.asyncio
async def test_pause_and_next(monkeypatch):
    monkeypatch.setattr(sp, "get_access_token", lambda: _async("tok"))
    _install(monkeypatch, [
        ("PUT", "/me/player/pause", _FakeResp(status_code=204)),
        ("POST", "/me/player/next", _FakeResp(status_code=204)),
    ])
    assert (await sp.pause()).ok is True
    assert (await sp.next_track()).ok is True


@pytest.mark.asyncio
async def test_current_track(monkeypatch):
    monkeypatch.setattr(sp, "get_access_token", lambda: _async("tok"))
    payload = {"is_playing": True, "item": {"name": "Get Lucky", "artists": [{"name": "Daft Punk"}]}}
    _install(monkeypatch, [("GET", "/me/player/currently-playing", _FakeResp(payload))])
    now = await sp.current_track()
    assert now.name == "Get Lucky" and now.artist == "Daft Punk" and now.is_playing is True


# --- Formatting ------------------------------------------------------------

def test_format_play_success_and_failure():
    assert format_play(PlaybackResult(True, "Get Lucky de Daft Punk")) == "Je lance Get Lucky de Daft Punk, mon amour."
    assert format_play(PlaybackResult(True, "resumed")) == "Je reprends, mon amour."
    assert "mon amour" in format_play(PlaybackResult(False, "la lecture a échoué (404)"))


def test_format_now_playing():
    assert format_now_playing(None) == "Rien ne joue pour le moment, mon amour."
    assert format_now_playing(NowPlaying("Get Lucky", "Daft Punk", True)) == "En lecture : Get Lucky de Daft Punk, mon amour."
    assert format_now_playing(NowPlaying("Get Lucky", "Daft Punk", False)).startswith("En pause sur")


# --- .env loader -----------------------------------------------------------

def test_load_env_file_sets_missing_without_overriding(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text('SPOTIFY_CLIENT_ID=fromfile\nSPOTIFY_DEVICE_NAME="Marshall"\n# comment\n')
    monkeypatch.setattr(sp, "__file__", str(tmp_path / "spotify_access.py"))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("SPOTIFY_DEVICE_NAME", "Kitchen")  # already set -> must win
    sp._load_env_file()
    assert os.environ["SPOTIFY_CLIENT_ID"] == "fromfile"   # filled from file
    assert os.environ["SPOTIFY_DEVICE_NAME"] == "Kitchen"  # not overridden


# --- .env writer -----------------------------------------------------------

def test_save_refresh_token_appends_when_absent(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SPOTIFY_CLIENT_ID=abc\n")
    monkeypatch.setattr(sp, "__file__", str(tmp_path / "spotify_access.py"))
    path = sp._save_refresh_token("REFRESH123")
    assert path is not None
    text = env.read_text()
    assert "SPOTIFY_CLIENT_ID=abc" in text
    assert "SPOTIFY_REFRESH_TOKEN=REFRESH123" in text


def test_save_refresh_token_updates_existing(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SPOTIFY_REFRESH_TOKEN=OLD\nSPOTIFY_DEVICE_NAME=Marshall\n")
    monkeypatch.setattr(sp, "__file__", str(tmp_path / "spotify_access.py"))
    sp._save_refresh_token("NEW")
    text = env.read_text()
    assert "SPOTIFY_REFRESH_TOKEN=NEW" in text
    assert "OLD" not in text
    assert "SPOTIFY_DEVICE_NAME=Marshall" in text  # other lines preserved


def test_save_refresh_token_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "__file__", str(tmp_path / "spotify_access.py"))
    sp._save_refresh_token("TOK")
    assert (tmp_path / ".env").read_text().strip() == "SPOTIFY_REFRESH_TOKEN=TOK"


# --- helper ----------------------------------------------------------------

async def _async(value):
    return value
