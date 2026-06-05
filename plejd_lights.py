"""Plejd lights control for JARVIS — local BLE, crypto key fetched from the cloud.

Why this is non-trivial: macOS hides BLE MAC addresses (it exposes random UUIDs),
but Plejd encrypts every command with the address of the device you're connected
to (the "gateway"). We can authenticate without it, but not send commands. The
workaround: auto-detect the gateway address via the mesh echo — the correct
address makes a command "stick" and the mesh echoes a LASTDATA notification. The
detected address is cached (data/plejd_gateway.txt) and only re-detected if a
command stops echoing (e.g. the Mac later connects to a different nearby unit).

The BLE connection is opened once and kept alive (ping loop) so voice commands
are near-instant after the first connect.
"""
from __future__ import annotations

import asyncio
import logging
import os
import types
import unicodedata
from pathlib import Path

log = logging.getLogger("jarvis.lights")

USER = os.getenv("PLEJD_USER")
PASS = os.getenv("PLEJD_PASS")
SITE = os.getenv("PLEJD_SITE_ID")
_GW_CACHE = Path(__file__).parent / "data" / "plejd_gateway.txt"

_ALL_WORDS = {"all", "tout", "toute", "toutes", "tous", "partout", "everything", "maison", "house"}


def enabled() -> bool:
    return bool(USER and PASS and SITE)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def _cached_gateway() -> str | None:
    try:
        if _GW_CACHE.exists():
            return _GW_CACHE.read_text().strip() or None
    except Exception:
        pass
    return os.getenv("PLEJD_GATEWAY") or None


def _save_gateway(addr: str) -> None:
    try:
        _GW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _GW_CACHE.write_text(addr)
    except Exception:
        pass


class _Controller:
    def __init__(self):
        self.mgr = None
        self.lights: list = []          # PlejdLight / PlejdRelay
        self.gateway: str | None = _cached_gateway()
        self.lock = asyncio.Lock()
        self._state: dict = {}        # node index -> on/off (real, from LIGHTLEVEL)
        self._index_of: dict = {}     # BLEaddress -> learned node index
        self._gw_ready = False        # gateway verified for the CURRENT connection
        self._cal_index = None        # node index toggled during calibration
        self._keepalive = None

    async def _ensure_mgr(self):
        if self.mgr:
            return
        import pyplejd
        self.mgr = pyplejd.PlejdManager(USER, PASS, SITE)
        await self.mgr.init()
        self.lights = [d for d in self.mgr.devices
                       if type(d).__name__ in ("PlejdLight", "PlejdRelay")]
        log.info("Plejd site loaded: %d controllable outputs", len(self.lights))

    @property
    def _connected(self) -> bool:
        try:
            return bool(self.mgr and self.mgr.mesh._client and self.mgr.mesh._client.is_connected)
        except Exception:
            return False

    def _set_gateway(self, addr: str):
        self.gateway = addr
        self.mgr.mesh._gateway_node = types.SimpleNamespace(
            BLEaddress=addr, is_gateway=True, update=lambda: None)

    def _on_lightlevel(self, _, data: bytearray):
        """Real physical state, by node index — NOT encrypted with the gateway
        address, so it's our ground truth (the network echo lies)."""
        from pyplejd import LightLevel
        for i in range(0, len(data), 10):
            chunk = data[i:i + 10]
            if len(chunk) >= 2:
                ll = LightLevel(chunk)
                self._state[ll.address] = bool(ll.state)

    async def _poll_state(self) -> dict:
        from pyplejd.ble import gatt
        self._state.clear()
        try:
            await self.mgr.mesh._client.write_gatt_char(gatt.PLEJD_LIGHTLEVEL, bytes([1]), response=True)
        except Exception:
            pass
        await asyncio.sleep(1.3)
        return dict(self._state)

    async def _attempt_connect(self) -> bool:
        import pyplejd
        from bleak import BleakScanner
        from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
        from pyplejd.ble import gatt
        mesh = self.mgr.mesh
        PS = pyplejd.PLEJD_SERVICE
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: PS.lower() in [u.lower() for u in (ad.service_uuids or [])], timeout=15)
        if not dev:
            raise RuntimeError("Aucun appareil Plejd détecté en Bluetooth")
        client = await establish_connection(BleakClientWithServiceCache, dev, dev.name or "plejd")
        # Plejd firmware workaround (2026-05): disconnect, wait, reconnect.
        try:
            await client.disconnect()
        except Exception:
            pass
        await asyncio.sleep(5)
        client = await establish_connection(BleakClientWithServiceCache, dev, dev.name or "plejd")
        mesh._client = client
        if not await mesh._authenticate(client):
            try:
                await client.disconnect()
            except Exception:
                pass
            return False
        await client.start_notify(gatt.PLEJD_LIGHTLEVEL, self._on_lightlevel)
        self._set_gateway(self.gateway or (self.lights[0].BLEaddress if self.lights else None))
        self._gw_ready = False  # new connection: gateway must be re-verified
        log.info("Plejd connected (cached gateway=%s)", self.gateway)
        if not self._keepalive or self._keepalive.done():
            self._keepalive = asyncio.create_task(self._keepalive_loop())
        return True

    async def _connect(self):
        await self._ensure_mgr()
        if self._connected:
            return
        # BLE on macOS is flaky on a cold connect (auth "Write Not Permitted" when
        # the unit is still busy from a prior link). Retry a few times with backoff.
        last = None
        for attempt in range(4):
            try:
                if await self._attempt_connect():
                    return
                last = "auth"
            except Exception as e:
                last = e
                log.warning("Plejd connect attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(3 + attempt * 3)
        raise RuntimeError(f"Connexion Plejd échouée après plusieurs essais ({last})")

    async def _keepalive_loop(self):
        while self._connected:
            await asyncio.sleep(25)
            try:
                await self.mgr.mesh._ping(self.mgr.mesh._client)
            except Exception:
                break

    async def _raw_cmd(self, dev, op: str, level):
        if op == "off":
            await dev.turn_off()
        elif op == "dim" and level is not None and hasattr(dev, "turn_on"):
            await dev.turn_on(dim=max(1, min(255, round(int(level) * 255 / 100))))
        else:
            await dev.turn_on()

    async def _calibrate(self) -> bool:
        """Find the gateway address that actually controls the mesh for THIS
        connection — independent of the command. We force a calibration light
        OFF→ON and confirm via real LIGHTLEVEL state that a node turned on; the
        gateway that produces it is correct. Restores the light afterwards. Runs
        once per connection (cached gateway tried first → usually no flicker)."""
        if self._gw_ready and self.gateway:
            return True
        cal = next((d for d in self.lights if type(d).__name__ == "PlejdLight"), None)
        if not cal:
            return False
        # The connected unit can be ANY Plejd device (light, relay, or wall button),
        # so consider every device address — cached first.
        all_addrs = [d.BLEaddress for d in self.mgr.devices if getattr(d, "BLEaddress", None)]
        cands = [c for c in dict.fromkeys([self.gateway] + all_addrs) if c]
        before = await self._poll_state()
        before_on = {i for i, v in before.items() if v}
        for a in cands:
            self._set_gateway(a)
            try:
                await cal.turn_off()
                await asyncio.sleep(0.5)
                mid = await self._poll_state()
                await cal.turn_on()
                await asyncio.sleep(0.6)
                after = await self._poll_state()
            except Exception as e:
                log.warning("calib attempt %s error: %s", a, e)
                continue
            mid_on = {i for i, v in mid.items() if v}
            after_on = {i for i, v in after.items() if v}
            newon = after_on - mid_on            # turned on by our OFF→ON toggle
            if newon:
                self.gateway = a
                self._gw_ready = True
                self._cal_index = next(iter(newon))
                _save_gateway(a)
                log.info("Plejd gateway calibrated -> %s (cal node %s)", a, self._cal_index)
                # Restore the calibration light to its pre-calibration state.
                if self._cal_index not in before_on:
                    try:
                        await cal.turn_off()
                    except Exception:
                        pass
                return True
        log.error("Plejd calibration failed — no gateway controlled the mesh")
        return False

    def find(self, query: str):
        """Return (matching output devices, human label)."""
        q = _norm(query)
        lights = [d for d in self.lights if type(d).__name__ == "PlejdLight"]
        if not q or q in _ALL_WORDS:
            return lights, "toutes les lumières"
        matches = []
        for d in self.lights:
            room = _norm(getattr(d, "room", None))
            name = _norm(d.name)
            if q == room or q == name or (len(q) >= 3 and (q in room or room in q or q in name or name in q)):
                matches.append(d)
        return matches, query.strip()

    def room_list(self) -> list[str]:
        return sorted({(getattr(d, "room", "") or "").strip()
                       for d in self.lights if type(d).__name__ == "PlejdLight"} - {""})

    async def control(self, query: str, op: str, level=None):
        """op: 'on' | 'off' | 'dim'. Returns ('ok'|'notfound'|'error', label/detail)."""
        if not enabled():
            return ("error", "Plejd non configuré")
        async with self.lock:
            try:
                await self._connect()
            except Exception as e:
                log.error("Plejd connect failed: %s", e)
                return ("error", str(e))
            devs, label = self.find(query)
            if not devs:
                return ("notfound", query)
            # Calibrate the gateway once for this connection (independent of the
            # command, so it works even when the target is already in the wanted
            # state, e.g. "éteins tout").
            if not await self._calibrate():
                return ("error", label)
            for d in devs:
                try:
                    await self._raw_cmd(d, op, level)
                except Exception as e:
                    log.error("Plejd send error on %s: %s", getattr(d, "name", "?"), e)
            return ("ok", label)


_ctrl = _Controller()


async def control(query: str, op: str, level=None):
    return await _ctrl.control(query, op, level)


async def preload():
    """Fetch the site (rooms/lights) from the cloud without connecting BLE — so the
    room list is available for the LLM prompt. Safe to call at startup."""
    if enabled():
        try:
            await _ctrl._ensure_mgr()
        except Exception as e:
            log.warning("Plejd preload failed: %s", e)


async def prewarm():
    """Open the BLE connection AND calibrate the gateway at startup, so the FIRST
    voice command is already fast (otherwise the ~20s scan/connect + calibration
    lands on it). After this, commands reuse the held connection (~1-2s)."""
    if not enabled():
        return
    try:
        async with _ctrl.lock:
            await _ctrl._connect()
            await _ctrl._calibrate()
        log.info("Plejd pre-warmed — connection held + gateway ready")
    except Exception as e:
        log.warning("Plejd prewarm failed (will connect lazily on first command): %s", e)


def room_list() -> list[str]:
    return _ctrl.room_list()
