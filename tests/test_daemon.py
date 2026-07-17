"""Tests for the main daemon orchestration."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ysyl.config import Settings
from ysyl.daemon import Daemon
from ysyl.models import BlockState, BlockStatus


@pytest.fixture
def temp_state(tmp_path: Path):
    return tmp_path / "state.json"


@pytest.fixture
def fake_settings(temp_state: Path):
    return Settings(
        state_file=str(temp_state),
        poll_interval_seconds=1,
        sleep_tick_seconds=1,
        ui_enabled=False,
        capture_dir=str(temp_state.parent / "captures"),
    )


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.list_surfaces = AsyncMock(return_value=[
        MagicMock(surface_id="s1", title="claude"),
    ])
    client.read_surface_text = AsyncMock(return_value="")
    client.send_key = AsyncMock()
    client.send_text = AsyncMock()
    return client


@pytest.fixture
def fake_scheduler():
    scheduler = MagicMock()
    scheduler.schedule = AsyncMock()
    return scheduler


@pytest.mark.asyncio
async def test_daemon_loads_and_saves_state(temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler):
    block = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=datetime.now(timezone.utc),
        reset_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status=BlockStatus.DETECTED,
    )
    temp_state.parent.mkdir(parents=True, exist_ok=True)
    temp_state.write_text(json.dumps([block.to_state_dict()]), encoding="utf-8")

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._load_state()
    assert len(daemon.list_states()) == 1
    assert daemon.list_states()[0].surface_id == "s1"


@pytest.mark.asyncio
async def test_daemon_detects_block_and_schedules_wake(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    fake_client.read_surface_text = AsyncMock(
        return_value="Claude Code\nYour usage will reset at 1:00 PM PST"
    )

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    await daemon._poll_once()

    states = daemon.list_states()
    assert len(states) == 1
    assert states[0].agent_kind == "claude"
    assert states[0].status == BlockStatus.SLEEPING
    fake_scheduler.schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_resumes_claude_with_return(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    detected = datetime.now(timezone.utc)
    block = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=detected,
        reset_at=detected,
        status=BlockStatus.SLEEPING,
    )
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = block

    await daemon._resume_surface("s1")

    fake_client.send_key.assert_awaited_once_with("s1", "return")
    assert daemon._states["s1"].status == BlockStatus.RESUMED
    assert daemon._states["s1"].retry_count == 1


@pytest.mark.asyncio
async def test_daemon_dismisses_surface(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="kimi",
        detected_at=datetime.now(timezone.utc),
        status=BlockStatus.DETECTED,
    )
    assert daemon.dismiss("s1")
    assert daemon._states["s1"].status == BlockStatus.DISMISSED
    assert not daemon.dismiss("missing")


@pytest.mark.asyncio
async def test_daemon_run_loop_exits_on_shutdown_event(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._shutdown.set()
    await daemon.run()
    fake_client.list_surfaces.assert_not_awaited()


def test_is_agent_surface(fake_settings: Settings, fake_client, fake_scheduler):
    from ysyl.models import SurfaceRef

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    # Reliable cmux signal
    assert daemon._is_agent_surface(SurfaceRef(surface_id="a", resume_kind="claude"))
    # Title/command fallback
    assert daemon._is_agent_surface(SurfaceRef(surface_id="b", title="claude — build X"))
    assert daemon._is_agent_surface(
        SurfaceRef(surface_id="c", initial_command="/usr/bin/kimi chat")
    )
    # Plain terminal is ignored
    assert not daemon._is_agent_surface(
        SurfaceRef(surface_id="d", title="~/Desktop/project", type="terminal")
    )


@pytest.mark.asyncio
async def test_arm_toggles_status(fake_settings: Settings, fake_client, fake_scheduler):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=now,
        reset_at=now + timedelta(hours=1),
        status=BlockStatus.SLEEPING,
        armed=True,
    )
    assert daemon.arm("s1", False)
    assert daemon._states["s1"].armed is False
    assert daemon._states["s1"].status == BlockStatus.DETECTED
    assert daemon.arm("s1", True)
    assert daemon._states["s1"].status == BlockStatus.SLEEPING
    assert not daemon.arm("missing")


@pytest.mark.asyncio
async def test_resume_due_skips_unarmed(fake_settings: Settings, fake_client, fake_scheduler):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=past,
        reset_at=past,
        status=BlockStatus.SLEEPING,
        armed=False,
    )
    await daemon._resume_due()
    fake_client.send_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_backoff_when_resume_ineffective(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=now,
        reset_at=now,
        status=BlockStatus.RESUMED,
        retry_count=1,
        armed=True,
    )
    # Banner is STILL present after a resume -> retry with backoff, stay sleeping.
    daemon._observe(
        "s1", "claude", "surface:1",
        "You have reached your usage limit. Please try again later.",
        now,
    )
    block = daemon._states["s1"]
    assert block.retry_count == 2
    assert block.status == BlockStatus.SLEEPING
    assert block.reset_at is not None and block.reset_at > now


@pytest.mark.asyncio
async def test_observe_captures_unparsed_reset(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._observe(
        "s1", "claude", "surface:1",
        "You have reached your usage limit. Please try again later.",
        now,
    )
    block = daemon._states["s1"]
    assert block.agent_kind == "claude"
    assert block.reset_at is not None  # backoff scheduled even without a parseable time
    captures = list(Path(fake_settings.capture_dir).glob("*.txt"))
    assert captures, "expected a capture file for the unparsed reset"


@pytest.mark.asyncio
async def test_scrollback_keyword_above_tail_is_ignored(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    # A limit phrase buried far up in scrollback, with a normal working footer.
    scrollback = "\n".join(
        ["You have reached your usage limit."]
        + [f"line {i}" for i in range(50)]
        + ["❯ ", "⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt"]
    )
    daemon._observe("s1", "claude", "surface:1", scrollback, now)
    assert "s1" not in daemon._states  # the stale mention must not trigger a block


@pytest.mark.asyncio
async def test_observe_marks_cleared_block_resumed(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=now,
        reset_at=now,
        status=BlockStatus.SLEEPING,
        armed=True,
    )
    # No limit banner anymore -> the block cleared.
    daemon._observe("s1", "claude", "surface:1", "❯ all good, working normally", now)
    assert daemon._states["s1"].status == BlockStatus.RESUMED
