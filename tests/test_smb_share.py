"""
Unit tests for SMB file-sharing actions.

Covers the pure helpers (share-name derivation, local-IP discovery with
interface fallback) and the enable_smb_share flow with the macOS subprocess
and osascript calls mocked out, so these run on any platform.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions
from actions import _share_name, enable_smb_share, get_local_ip


# --- _share_name -----------------------------------------------------------

def test_share_name_from_documents():
    assert _share_name("~/Documents") == "Documents"


def test_share_name_strips_trailing_slash():
    assert _share_name("/Users/me/Desktop/") == "Desktop"


def test_share_name_never_empty():
    # Path("/").name is "" — must fall back to a sane default.
    assert _share_name("/") == "Share"


# --- get_local_ip ----------------------------------------------------------

@pytest.mark.asyncio
async def test_get_local_ip_prefers_first_interface():
    async def fake_run(cmd, timeout=10.0):
        # ipconfig getifaddr en0 → an address
        if cmd[:2] == ["ipconfig", "getifaddr"] and cmd[2] == "en0":
            return 0, "192.168.1.42", ""
        return 1, "", "no address"

    with patch.object(actions, "_run_cmd", side_effect=fake_run):
        assert await get_local_ip() == "192.168.1.42"


@pytest.mark.asyncio
async def test_get_local_ip_falls_back_to_default_route():
    async def fake_run(cmd, timeout=10.0):
        if cmd[:2] == ["ipconfig", "getifaddr"]:
            # en0/en1/en2 have no address; the routing-table interface does.
            if cmd[2] == "bridge100":
                return 0, "10.0.0.5", ""
            return 1, "", "no address"
        if cmd[0] == "route":
            return 0, "   gateway: 10.0.0.1\n  interface: bridge100\n", ""
        return 1, "", ""

    with patch.object(actions, "_run_cmd", side_effect=fake_run):
        assert await get_local_ip() == "10.0.0.5"


@pytest.mark.asyncio
async def test_get_local_ip_returns_none_when_offline():
    async def fake_run(cmd, timeout=10.0):
        return 1, "", "unreachable"

    with patch.object(actions, "_run_cmd", side_effect=fake_run):
        assert await get_local_ip() is None


# --- enable_smb_share ------------------------------------------------------

@pytest.mark.asyncio
async def test_enable_smb_share_missing_folder():
    result = await enable_smb_share("/definitely/not/a/real/folder/xyz")
    assert result["success"] is False
    assert result["smb_url"] is None


def _fake_osascript_ok():
    """A fake subprocess whose osascript run 'succeeds'."""
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"JARVIS_SMB_OK", b""))
    return proc


@pytest.mark.asyncio
async def test_enable_smb_share_success_builds_url(tmp_path):
    with patch.object(actions, "get_local_ip", AsyncMock(return_value="192.168.1.42")), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_osascript_ok())):
        result = await enable_smb_share(str(tmp_path))

    assert result["success"] is True
    assert result["ip"] == "192.168.1.42"
    assert result["smb_url"] == f"smb://192.168.1.42/{tmp_path.name}"
    assert tmp_path.name in result["confirmation"]


@pytest.mark.asyncio
async def test_enable_smb_share_success_no_ip(tmp_path):
    with patch.object(actions, "get_local_ip", AsyncMock(return_value=None)), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_osascript_ok())):
        result = await enable_smb_share(str(tmp_path))

    assert result["success"] is True
    assert result["smb_url"] is None


@pytest.mark.asyncio
async def test_enable_smb_share_user_cancels_auth(tmp_path):
    proc = AsyncMock()
    proc.returncode = 1
    # -128 is AppleScript's "user cancelled" error code.
    proc.communicate = AsyncMock(return_value=(b"", b"execution error: User canceled. (-128)"))

    with patch.object(actions, "get_local_ip", AsyncMock(return_value="192.168.1.42")), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await enable_smb_share(str(tmp_path))

    assert result["success"] is False
    assert result["smb_url"] is None
    assert "cancel" in result["confirmation"].lower()


@pytest.mark.asyncio
async def test_enable_smb_share_url_quotes_spaces(tmp_path):
    folder = tmp_path / "My Files"
    folder.mkdir()
    with patch.object(actions, "get_local_ip", AsyncMock(return_value="10.0.0.9")), \
         patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_fake_osascript_ok())):
        result = await enable_smb_share(str(folder))

    assert result["success"] is True
    # The space in the share name must be percent-encoded in the URL.
    assert result["smb_url"] == "smb://10.0.0.9/My%20Files"
