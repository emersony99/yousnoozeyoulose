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
    from ysyl.models import SurfaceRef

    settings = Settings(
        state_file=str(tmp_path / "state.json"),
        settings_file=str(tmp_path / "settings.json"),
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
    d._watched["s1"] = SurfaceRef(
        surface_id="s1",
        ref="surface:1",
        title="claude",
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
    assert data["impact"]["waiting"] == 1


@pytest.mark.asyncio
async def test_index_serves_html(daemon):
    ui = WebUI(daemon)
    status, ctype, body = await ui._route("GET", "/", b"")
    assert status == 200
    assert "text/html" in ctype
    assert b"YouSnoozeYouLose" in body
    # The operational dashboard remains the default view.
    assert b"open-session" in body
    assert b"id=\"theme\"" in body
    assert b'id="history"' in body
    assert b'id="connection"' in body
    assert b'id="toast"' in body
    assert b'id="help-button"' in body
    assert b'id="tutorial"' in body
    assert b"ysyl-tutorial-seen-v1" in body
    assert b"updateTutorialSpotlight" in body
    assert b"radial-gradient(ellipse" in body
    assert b"positionTutorialDialog" in body
    assert b'id="tutorial-example"' in body
    assert b"When an agent runs out of usage" in body
    assert b'data-theme="midnight"' in body
    assert b'data-theme="forest"' in body
    assert b'data-theme="moss"' in body
    assert b'data-theme="ocean"' in body
    assert b'data-theme="orchid"' in body
    assert b"prefers-reduced-motion" in body


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
    daemon.client.send_text.assert_awaited_once_with("s1", "resume\n")
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


@pytest.mark.asyncio
async def test_resume_now_reports_failure_for_missing_surface(daemon):
    ui = WebUI(daemon)
    status, _, body = await ui._route(
        "POST", "/api/resume_now", json.dumps({"surface_id": "missing"}).encode()
    )
    assert status == 200
    assert json.loads(body)["ok"] is False


@pytest.mark.asyncio
async def test_open_session_endpoint_opens_a_cmux_agent_pane(daemon):
    daemon._ledger["session-1"] = {"id": "session-1", "kind": "claude", "cwd": "/tmp/project"}
    daemon.client.open_saved_session = AsyncMock(return_value=True)
    ui = WebUI(daemon)

    status, _, body = await ui._route(
        "POST", "/api/open_session", json.dumps({"session_id": "session-1"}).encode()
    )
    assert status == 200
    assert json.loads(body) == {"ok": True, "pane_opened": True}
    daemon.client.open_saved_session.assert_awaited_once_with("claude", "session-1", "/tmp/project")


@pytest.mark.asyncio
async def test_settings_get_endpoint(daemon):
    ui = WebUI(daemon)
    status, ctype, body = await ui._route("GET", "/api/settings", b"")
    assert status == 200
    assert "application/json" in ctype
    data = json.loads(body)
    assert "history_window" in data
    assert data["history_window"] == daemon.config.history_window
    assert data["color_theme"] == "midnight"


@pytest.mark.asyncio
async def test_settings_post_endpoint_updates_history_window(daemon):
    ui = WebUI(daemon)
    original = daemon.config.history_window
    status, _, body = await ui._route(
        "POST", "/api/settings", json.dumps({"history_window": "3d"}).encode()
    )
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["settings"]["history_window"] == "3d"
    assert daemon.config.history_window == "3d"
    assert daemon.config.prune_resumed_after_hours == 72
    # Restore
    daemon.config.history_window = original
    daemon.config.prune_resumed_after_hours = {"3d": 72, "1w": 168, "3w": 504}[original]
    daemon.config.prune_dismissed_after_hours = daemon.config.prune_resumed_after_hours


@pytest.mark.asyncio
async def test_settings_post_endpoint_persists_colour_theme(daemon):
    ui = WebUI(daemon)
    status, _, body = await ui._route(
        "POST", "/api/settings", json.dumps({"color_theme": "forest"}).encode()
    )
    assert status == 200
    assert json.loads(body)["settings"]["color_theme"] == "forest"

    status, _, body = await ui._route(
        "POST", "/api/settings", json.dumps({"color_theme": "orchid"}).encode()
    )
    assert status == 200
    assert json.loads(body)["settings"]["color_theme"] == "orchid"

    status, _, body = await ui._route(
        "POST", "/api/settings", json.dumps({"color_theme": "system"}).encode()
    )
    assert status == 200
    assert json.loads(body)["settings"]["color_theme"] == "forest"

    status, _, _ = await ui._route(
        "POST", "/api/settings", json.dumps({"color_theme": "not-a-theme"}).encode()
    )
    assert status == 400
