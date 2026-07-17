"""Tests for the minimal web dashboard (in-process, no real sockets)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ysyl.config import Settings
from ysyl.daemon import Daemon
from ysyl.models import BlockState, BlockStatus
from ysyl.webui import WebUI


@pytest.fixture
def daemon(tmp_path: Path):
    settings = Settings(
        state_file=str(tmp_path / "state.json"),
        ui_enabled=False,
        capture_dir=str(tmp_path / "captures"),
    )
    client = MagicMock()
    client.send_key = AsyncMock()
    client.send_text = AsyncMock()
    d = Daemon(config=settings, client=client)
    now = datetime.now(timezone.utc)
    d._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=now,
        reset_at=now + timedelta(hours=1),
        status=BlockStatus.SLEEPING,
        armed=True,
        title="claude",
        ref="surface:1",
    )
    return d


@pytest.mark.asyncio
async def test_state_endpoint(daemon):
    ui = WebUI(daemon)
    status, ctype, body = await ui._route("GET", "/api/state", b"")
    assert status == 200
    data = json.loads(body)
    assert data["surfaces"][0]["surface_id"] == "s1"
    assert data["surfaces"][0]["seconds_until_reset"] > 0


@pytest.mark.asyncio
async def test_index_serves_html(daemon):
    ui = WebUI(daemon)
    status, ctype, body = await ui._route("GET", "/", b"")
    assert status == 200
    assert "text/html" in ctype
    assert b"YouSnoozeYouLose" in body


@pytest.mark.asyncio
async def test_arm_endpoint(daemon):
    ui = WebUI(daemon)
    status, _, body = await ui._route(
        "POST", "/api/arm", json.dumps({"surface_id": "s1", "armed": False}).encode()
    )
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert daemon._states["s1"].armed is False


@pytest.mark.asyncio
async def test_dismiss_endpoint(daemon):
    ui = WebUI(daemon)
    status, _, body = await ui._route(
        "POST", "/api/dismiss", json.dumps({"surface_id": "s1"}).encode()
    )
    assert json.loads(body)["ok"] is True
    assert daemon._states["s1"].status == BlockStatus.DISMISSED


@pytest.mark.asyncio
async def test_resume_now_endpoint(daemon):
    ui = WebUI(daemon)
    status, _, body = await ui._route(
        "POST", "/api/resume_now", json.dumps({"surface_id": "s1"}).encode()
    )
    assert json.loads(body)["ok"] is True
    daemon.client.send_key.assert_awaited_once_with("s1", "return")
    assert daemon._states["s1"].status == BlockStatus.RESUMED


@pytest.mark.asyncio
async def test_bad_requests(daemon):
    ui = WebUI(daemon)
    status, _, _ = await ui._route("GET", "/nope", b"")
    assert status == 404
    status, _, _ = await ui._route("POST", "/api/arm", b"not-json")
    assert status == 400
    status, _, _ = await ui._route("POST", "/api/arm", b"{}")
    assert status == 400  # missing surface_id
