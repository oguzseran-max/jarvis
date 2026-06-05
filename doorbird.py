"""DoorBird gate/door control for JARVIS — local HTTP API (much simpler than BLE).

The DoorBird exposes a documented LAN REST API over WiFi. Opening the gate is a
single authenticated GET to open-door.cgi?r=<relay>. Credentials need the
"API operator" permission and live in .env. Note a DoorBird system can have more
than one station on the LAN — DOORBIRD_HOST must point at the one whose user
owns the gate relay.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("jarvis.doorbird")

HOST = os.getenv("DOORBIRD_HOST")
USER = os.getenv("DOORBIRD_USER")
PASS = os.getenv("DOORBIRD_PASS")
GATE_RELAY = os.getenv("DOORBIRD_GATE_RELAY", "1")


def enabled() -> bool:
    return bool(HOST and USER and PASS)


async def open_gate(relay: str | None = None) -> bool:
    """Trigger the gate relay. Returns True on success (DoorBird RETURNCODE 1)."""
    if not enabled():
        return False
    r = relay or GATE_RELAY
    url = f"http://{HOST}/bha-api/open-door.cgi"
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.get(url, params={"r": r}, auth=httpx.DigestAuth(USER, PASS))
            ok = resp.status_code == 200 and "RETURNCODE" in resp.text and '"1"' in resp.text
            log.info("DoorBird open relay %s -> %s (ok=%s)", r, resp.status_code, ok)
            return bool(ok)
    except Exception as e:
        log.error("DoorBird open_gate error: %s", e)
        return False


async def snapshot() -> bytes | None:
    """Grab a single JPEG from the DoorBird camera."""
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(f"http://{HOST}/bha-api/image.cgi", auth=httpx.DigestAuth(USER, PASS))
            return r.content if r.status_code == 200 and r.content else None
    except Exception as e:
        log.error("DoorBird snapshot error: %s", e)
        return None


VIDEO_CONTENT_TYPE = 'multipart/x-mixed-replace; boundary="my-boundary"'


async def video_stream():
    """Proxy the DoorBird live MJPEG stream (so the browser never sees the creds).
    Yields raw multipart bytes; stops when the consumer disconnects."""
    if not enabled():
        return
    url = f"http://{HOST}/bha-api/video.cgi"
    async with httpx.AsyncClient(timeout=None) as http:
        async with http.stream("GET", url, auth=httpx.DigestAuth(USER, PASS)) as r:
            if r.status_code != 200:
                log.warning("DoorBird video HTTP %s", r.status_code)
                return
            async for chunk in r.aiter_bytes():
                yield chunk


async def monitor_rings(on_ring, cooldown: float = 6.0):
    """Hold a streaming connection to the DoorBird event monitor and call the
    async `on_ring()` callback each time the doorbell is pressed. Auto-reconnects;
    debounces repeated edges within `cooldown` seconds."""
    if not enabled():
        return
    url = f"http://{HOST}/bha-api/monitor.cgi"
    last = 0.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as http:
                async with http.stream("GET", url, params={"ring": "doorbell"},
                                       auth=httpx.DigestAuth(USER, PASS)) as resp:
                    if resp.status_code != 200:
                        log.warning("DoorBird monitor HTTP %s", resp.status_code)
                        await asyncio.sleep(15)
                        continue
                    log.info("DoorBird ring monitor connected")
                    async for line in resp.aiter_lines():
                        s = line.strip().lower()
                        if s.startswith("doorbell:"):
                            val = s.split(":", 1)[1]
                            if val in ("h", "1", "high", "on") and time.time() - last > cooldown:
                                last = time.time()
                                log.info("DoorBird doorbell ring detected")
                                try:
                                    await on_ring()
                                except Exception as e:
                                    log.error("on_ring handler error: %s", e)
        except Exception as e:
            log.warning("DoorBird monitor stream error: %s", e)
        await asyncio.sleep(8)  # reconnect after a drop
