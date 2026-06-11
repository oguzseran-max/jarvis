"""D-ID talking-avatar integration (Phase 2 — lip-sync for the Marion persona).

JARVIS already speaks with a Fish Audio cloned voice. This module turns that
audio into a *lip-synced* video of Marion using the D-ID API, so the FR/TR
personas show a face that actually articulates the words — while keeping the
Fish voice (we feed D-ID our own audio, not its built-in TTS).

Flow per spoken reply:
  1. Upload Marion's portrait once → a D-ID-hosted source URL (cached on disk).
  2. Upload the Fish MP3 bytes → a D-ID-hosted audio URL.
  3. Create a /talks job (source_url + script{type:audio, audio_url}).
  4. Poll /talks/{id} until done → return the result mp4 URL.

The whole thing is best-effort: any failure (no key, face not detected, timeout)
returns None, and the caller falls back to the static face + plain audio. No D-ID
key → the module is simply disabled and JARVIS behaves exactly as before.

Auth: D-ID uses HTTP Basic. The key from the D-ID console may be given either as
`username:password` (we base64-encode it) or as the ready base64 token (used
as-is). DID_API_KEY accepts either; `Basic ` prefix is tolerated too.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("jarvis.did")

DID_API_URL = os.getenv("DID_API_URL", "https://api.d-id.com")
DID_API_KEY = os.getenv("DID_API_KEY", "").strip()

# Marion's portrait (served by the frontend) — also the source we upload to D-ID.
_REPO = Path(__file__).resolve().parent
_PUBLIC = _REPO / "frontend" / "public"
# Weather-dependent source portraits (transparent cutouts). The talking-head
# video uses the look matching the current weather.
_SOURCES = {
    "default": _PUBLIC / "marion-cutout.png",
    "rain": _PUBLIC / "marion-rain.png",
    "sun": _PUBLIC / "marion-sun.png",
}
SOURCE_IMAGE = _SOURCES["default"]  # back-compat for the (unused) batch path

_POLL_INTERVAL = 1.0   # seconds between status checks
_POLL_TIMEOUT = 90.0   # give up after this long (Lite renders in well under this
                       # once the plan has propagated to D-ID's render pipeline)
_source_urls: dict = {}  # look -> uploaded D-ID image URL (in-process cache)


def is_enabled() -> bool:
    return bool(DID_API_KEY)


def _auth_header() -> str:
    key = DID_API_KEY
    if key.lower().startswith("basic "):
        return key  # already a full header value
    token = base64.b64encode(key.encode()).decode() if ":" in key else key
    return f"Basic {token}"


def _headers(json: bool = True) -> dict:
    h = {"Authorization": _auth_header(), "accept": "application/json"}
    if json:
        h["content-type"] = "application/json"
    return h


def _invalidate_source_url(look: str = "default") -> None:
    """Drop a stale hosted URL (D-ID image URLs expire) so the next call
    re-uploads. Clears both the in-process cache and the on-disk cache."""
    if look not in _SOURCES:
        look = "default"
    _source_urls.pop(look, None)
    cache = _REPO / "data" / f"did_source_{look}.txt"
    try:
        cache.unlink(missing_ok=True)
    except Exception:
        pass


async def _get_source_url(http: httpx.AsyncClient, look: str = "default",
                          force: bool = False) -> Optional[str]:
    """Upload the look's portrait to D-ID once; reuse the hosted URL thereafter.
    Cached per look on disk (data/did_source_<look>.txt). `force` bypasses the
    cache and re-uploads — D-ID image URLs expire, so a cached URL eventually
    yields a 500 'Stream Error' until refreshed."""
    if look not in _SOURCES:
        look = "default"
    if not force:
        if _source_urls.get(look):
            return _source_urls[look]
        cache = _REPO / "data" / f"did_source_{look}.txt"
        if cache.exists():
            cached = cache.read_text().strip()
            if cached:
                _source_urls[look] = cached
                return cached
    else:
        cache = _REPO / "data" / f"did_source_{look}.txt"
    img = _SOURCES[look]
    if not img.exists():
        log.error(f"D-ID source image missing: {img}")
        return await _get_source_url(http, "default") if look != "default" else None
    files = {"image": (img.name, img.read_bytes(), "image/png")}
    r = await http.post(f"{DID_API_URL}/images", headers=_headers(json=False), files=files)
    if r.status_code not in (200, 201):
        log.error(f"D-ID image upload failed: {r.status_code} {r.text[:200]}")
        return None
    url = r.json().get("url")
    if url:
        _source_urls[look] = url
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(url)
        except Exception:
            pass
    return url


async def _upload_audio(http: httpx.AsyncClient, audio: bytes) -> Optional[dict]:
    """Upload audio to D-ID; returns the response dict ({url, duration, ...})."""
    files = {"audio": ("speech.mp3", audio, "audio/mpeg")}
    r = await http.post(f"{DID_API_URL}/audios", headers=_headers(json=False), files=files)
    if r.status_code not in (200, 201):
        log.error(f"D-ID audio upload failed: {r.status_code} {r.text[:200]}")
        return None
    return r.json()


async def render_talk(audio: bytes) -> Optional[str]:
    """Turn Fish-Audio MP3 bytes into a lip-synced Marion video. Returns the
    result mp4 URL, or None on any failure (caller falls back to static face)."""
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            source_url = await _get_source_url(http)
            if not source_url:
                return None
            up = await _upload_audio(http, audio)
            if not up or not up.get("url"):
                return None
            audio_url = up["url"]

            body = {
                "source_url": source_url,
                "script": {"type": "audio", "audio_url": audio_url},
                # stitch keeps the body still and only animates the face region,
                # which looks far more natural on a half-body portrait.
                "config": {"stitch": True},
            }
            r = await http.post(f"{DID_API_URL}/talks", headers=_headers(), json=body)
            # Expired cached source URL → 5xx; retry once with a fresh upload.
            if r.status_code >= 500:
                log.warning(f"D-ID talk create failed: {r.status_code} {r.text[:200]} — re-uploading source")
                _invalidate_source_url("default")
                source_url = await _get_source_url(http, "default", force=True)
                if source_url:
                    body["source_url"] = source_url
                    r = await http.post(f"{DID_API_URL}/talks", headers=_headers(), json=body)
            if r.status_code not in (200, 201):
                log.error(f"D-ID talk create failed: {r.status_code} {r.text[:200]}")
                return None
            talk_id = r.json().get("id")
            if not talk_id:
                return None

            # Poll until the render is done.
            waited = 0.0
            while waited < _POLL_TIMEOUT:
                await asyncio.sleep(_POLL_INTERVAL)
                waited += _POLL_INTERVAL
                pr = await http.get(f"{DID_API_URL}/talks/{talk_id}", headers=_headers())
                if pr.status_code != 200:
                    log.error(f"D-ID poll failed: {pr.status_code} {pr.text[:160]}")
                    return None
                data = pr.json()
                status = data.get("status")
                if status == "done":
                    return data.get("result_url")
                if status in ("error", "rejected"):
                    log.error(f"D-ID talk {status}: {data.get('error') or data}")
                    return None
            log.warning("D-ID talk timed out")
            return None
    except Exception as e:
        log.error(f"D-ID render_talk error: {e}")
        return None


# ---------------------------------------------------------------------------
# Real-time streaming (WebRTC) — the /talks/streams API.
#
# /talks (above) is a batch render (~minutes); streaming pushes a live lip-synced
# video track over a WebRTC peer connection so Marion speaks within ~1-2s. The
# browser owns the RTCPeerConnection; these helpers are proxied by the backend so
# the D-ID API key never reaches the client. Flow:
#   create_stream() -> {id, session_id, offer, ice_servers}
#   browser builds an SDP answer -> stream_sdp(); trickles ICE -> stream_ice()
#   stream_speak(audio) makes her talk; close_stream() tears it down.
# ---------------------------------------------------------------------------

async def create_stream(look: str = "default") -> Optional[dict]:
    """Open a streaming session with the weather-appropriate portrait. Returns the
    D-ID offer/ice_servers/session_id (+ our stream id) for the WebRTC handshake."""
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            source_url = await _get_source_url(http, look)
            if not source_url:
                return None
            r = await http.post(f"{DID_API_URL}/talks/streams",
                                headers=_headers(), json={"source_url": source_url})
            # A cached source URL that D-ID has since expired returns a 5xx
            # ('Stream Error'). Invalidate it and retry once with a fresh upload.
            # (4xx like 403 'Max user sessions' aren't a source problem — skip.)
            if r.status_code >= 500:
                log.warning(f"D-ID stream create failed: {r.status_code} {r.text[:200]} — re-uploading source")
                _invalidate_source_url(look)
                source_url = await _get_source_url(http, look, force=True)
                if source_url:
                    r = await http.post(f"{DID_API_URL}/talks/streams",
                                        headers=_headers(), json={"source_url": source_url})
            if r.status_code not in (200, 201):
                log.error(f"D-ID stream create failed: {r.status_code} {r.text[:200]}")
                return None
            d = r.json()
            return {
                "id": d.get("id"),
                "session_id": d.get("session_id"),
                "offer": d.get("offer"),
                "ice_servers": d.get("ice_servers"),
            }
    except Exception as e:
        log.error(f"D-ID create_stream error: {e}")
        return None


async def stream_sdp(stream_id: str, session_id: str, answer: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(f"{DID_API_URL}/talks/streams/{stream_id}/sdp",
                                headers=_headers(), json={"answer": answer, "session_id": session_id})
            if r.status_code not in (200, 201, 204):
                log.error(f"D-ID stream sdp failed: {r.status_code} {r.text[:160]}")
                return False
            return True
    except Exception as e:
        log.error(f"D-ID stream_sdp error: {e}")
        return False


async def stream_ice(stream_id: str, session_id: str, candidate: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            body = {"session_id": session_id, **candidate}
            r = await http.post(f"{DID_API_URL}/talks/streams/{stream_id}/ice",
                                headers=_headers(), json=body)
            return r.status_code in (200, 201, 204)
    except Exception as e:
        log.error(f"D-ID stream_ice error: {e}")
        return False


async def stream_speak(stream_id: str, session_id: str, audio: bytes) -> Optional[float]:
    """Make the live avatar speak the given audio over the open stream.
    Returns the audio duration in seconds on success (so the client can resume
    the mic exactly when she finishes), or None on failure."""
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            up = await _upload_audio(http, audio)
            if not up or not up.get("url"):
                return None
            body = {
                "script": {"type": "audio", "audio_url": up["url"]},
                "config": {"stitch": True},
                "session_id": session_id,
            }
            r = await http.post(f"{DID_API_URL}/talks/streams/{stream_id}",
                                headers=_headers(), json=body)
            if r.status_code not in (200, 201, 204):
                log.error(f"D-ID stream speak failed: {r.status_code} {r.text[:200]}")
                return None
            # Always truthy on success (callers use `if stream_speak(...)`).
            return float(up.get("duration") or 5.0)
    except Exception as e:
        log.error(f"D-ID stream_speak error: {e}")
        return None


async def close_stream(stream_id: str, session_id: str) -> None:
    # Teardown is HTTP DELETE on the stream resource. NB: a POST to
    # `/talks/streams/{id}/delete` is NOT a real endpoint — it 403s at the
    # gateway, leaving the stream OPEN; on the lite plan those orphans pile up
    # until "Max user sessions reached" and Marion can no longer lip-sync.
    if not stream_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.request("DELETE", f"{DID_API_URL}/talks/streams/{stream_id}",
                                   headers=_headers(), json={"session_id": session_id})
            if r.status_code not in (200, 201, 204, 404):
                log.warning(f"D-ID close_stream unexpected: {r.status_code} {r.text[:120]}")
    except Exception as e:
        log.debug(f"D-ID close_stream error: {e}")
