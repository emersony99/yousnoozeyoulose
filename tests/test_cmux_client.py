"""Tests for the cmux async client wrapper."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ysyl.cmux_client import CmuxClient, CmuxError


async def _run_with_mock_subprocess(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(stdout, stderr))
    mock_proc.returncode = returncode
    mock_proc.stdout = None
    mock_proc.stderr = None

    # create_subprocess_exec is a coroutine function; the client awaits its
    # result, so the patch must be awaitable (AsyncMock), not a plain MagicMock.
    cmock = AsyncMock(return_value=mock_proc)
    patcher = patch("asyncio.create_subprocess_exec", cmock)
    patcher.start()
    return cmock


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


@pytest.mark.asyncio
async def test_list_surfaces_maps_fields():
    surfaces = [
        {
            "surface_id": "s1",
            "pane_ref": "p1",
            "workspace_ref": "w1",
            "window_ref": "win1",
            "title": "claude",
            "initial_command": "claude",
        }
    ]
    cmock = await _run_with_mock_subprocess(stdout=json.dumps(surfaces).encode())

    client = CmuxClient()
    result = await client.list_surfaces()

    assert len(result) == 1
    assert result[0].surface_id == "s1"
    assert result[0].title == "claude"
    assert cmock.call_args[0][0:3] == ("cmux", "rpc", "surface.list")


@pytest.mark.asyncio
async def test_read_surface_text_returns_text_field():
    await _run_with_mock_subprocess(stdout=json.dumps({"text": "hello world"}).encode())
    client = CmuxClient()
    assert await client.read_surface_text("s1") == "hello world"


@pytest.mark.asyncio
async def test_read_surface_text_decodes_base64_data():
    import base64

    encoded = base64.b64encode(b"hello world").decode()
    await _run_with_mock_subprocess(stdout=json.dumps({"data": encoded}).encode())
    client = CmuxClient()
    assert await client.read_surface_text("s1") == "hello world"


@pytest.mark.asyncio
async def test_send_key_invokes_rpc():
    cmock = await _run_with_mock_subprocess(stdout=b"{}")
    client = CmuxClient()
    await client.send_key("s1", "return")
    args = cmock.call_args[0]
    assert args[0:3] == ("cmux", "rpc", "surface.send_key")
    assert json.loads(args[3]) == {"surface_id": "s1", "key": "return"}


@pytest.mark.asyncio
async def test_send_text_invokes_rpc():
    cmock = await _run_with_mock_subprocess(stdout=b"{}")
    client = CmuxClient()
    await client.send_text("s1", "continue\n")
    args = cmock.call_args[0]
    assert args[0:3] == ("cmux", "rpc", "surface.send_text")
    assert json.loads(args[3]) == {"surface_id": "s1", "text": "continue\n"}


@pytest.mark.asyncio
async def test_nonzero_exit_raises_cmux_error():
    await _run_with_mock_subprocess(stdout=b"", stderr=b"boom", returncode=1)
    client = CmuxClient()
    with pytest.raises(CmuxError):
        await client.list_surfaces()


@pytest.mark.asyncio
async def test_invalid_json_raises_cmux_error():
    await _run_with_mock_subprocess(stdout=b"not-json")
    client = CmuxClient()
    with pytest.raises(CmuxError):
        await client.list_surfaces()


@pytest.mark.asyncio
async def test_context_manager_noop():
    client = CmuxClient()
    async with client as c:
        assert c is client


@pytest.mark.asyncio
async def test_rpc_retries_transient_broken_pipe():
    client = CmuxClient(retries=3, retry_delay=0)
    calls = {"n": 0}

    async def fake(method, params=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise CmuxError(
                "cmux rpc surface.list exited with code 1",
                stderr="Error: Failed to write to socket (Broken pipe, errno 32)",
            )
        return {"surfaces": []}

    client._rpc_once = fake
    assert await client.list_surfaces() == []
    assert calls["n"] == 3  # retried twice, succeeded on the third


@pytest.mark.asyncio
async def test_rpc_does_not_retry_non_transient():
    client = CmuxClient(retries=3, retry_delay=0)
    calls = {"n": 0}

    async def fake(method, params=None):
        calls["n"] += 1
        raise CmuxError("boom", stderr="some unrelated error")

    client._rpc_once = fake
    with pytest.raises(CmuxError):
        await client.list_surfaces()
    assert calls["n"] == 1  # not retried


def test_live_socket_path_prefers_last_socket_path(tmp_path, monkeypatch):
    live = tmp_path / "cmux-501.sock"
    live.write_text("")  # placeholder; treated as a socket via the patch below
    (tmp_path / "last-socket-path").write_text(str(live))
    monkeypatch.setattr("ysyl.cmux_client._is_socket", lambda p: p == str(live))
    # Inherited path is stale; resolution should find the live one via last-socket-path.
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "stale.sock"))
    assert CmuxClient.live_socket_path() == str(live)
    env = CmuxClient()._child_env()
    assert env["CMUX_SOCKET_PATH"] == str(live)


def test_live_socket_path_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "whatever.sock"))
    monkeypatch.setattr(
        "ysyl.cmux_client.os.path.expanduser", lambda p: str(tmp_path / "nope")
    )
    assert CmuxClient.live_socket_path() is None


def test_password_added_to_args():
    assert CmuxClient(password="s3cret")._base_args() == ["cmux", "--password", "s3cret"]
    assert CmuxClient()._base_args() == ["cmux"]


@pytest.mark.asyncio
async def test_check_reports_ok_and_failure():
    client = CmuxClient()
    client._rpc_once = AsyncMock(return_value={"surfaces": []})
    ok, detail = await client.check()
    assert ok and "reachable" in detail

    client._rpc_once = AsyncMock(side_effect=CmuxError("nope", stderr="down"))
    ok, detail = await client.check()
    assert not ok
