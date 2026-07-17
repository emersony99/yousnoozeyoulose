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
async def test_daemon_resumes_claude_with_text_resume(
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

    fake_client.send_text.assert_awaited_once_with("s1", "resume\n")
    assert daemon._states["s1"].status == BlockStatus.RESUMED
    assert daemon._states["s1"].retry_count == 1


@pytest.mark.asyncio
async def test_daemon_resumes_kimi_with_text_resume(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    detected = datetime.now(timezone.utc)
    block = BlockState(
        surface_id="s1",
        agent_kind="kimi",
        detected_at=detected,
        reset_at=detected,
        status=BlockStatus.SLEEPING,
    )
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = block

    await daemon._resume_surface("s1")

    fake_client.send_text.assert_awaited_once_with("s1", "resume\n")
    assert daemon._states["s1"].status == BlockStatus.RESUMED


@pytest.mark.asyncio
async def test_daemon_respects_per_agent_enter_override(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    fake_settings.agent_resume_actions = {"claude": "enter"}
    detected = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=detected,
        reset_at=detected,
        status=BlockStatus.SLEEPING,
    )

    await daemon._resume_surface("s1")

    fake_client.send_key.assert_awaited_once_with("s1", "return")


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
    # Ambiguous agent prompt prefix still counts as an agent surface.
    assert daemon._is_agent_surface(
        SurfaceRef(surface_id="e", title="Role: You are an expert systems engineer")
    )
    # Plain terminal is ignored
    assert not daemon._is_agent_surface(
        SurfaceRef(surface_id="d", title="~/Desktop/project", type="terminal")
    )


def test_infer_agent_kind(fake_settings: Settings, fake_client, fake_scheduler):
    from ysyl.models import SurfaceRef

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    assert daemon._infer_agent_kind(SurfaceRef(surface_id="a", resume_kind="claude")) == "claude"
    assert daemon._infer_agent_kind(SurfaceRef(surface_id="b", resume_kind="kimi")) == "kimi"
    assert daemon._infer_agent_kind(
        SurfaceRef(surface_id="c", title="kimi session")
    ) == "kimi"
    assert daemon._infer_agent_kind(
        SurfaceRef(surface_id="d", initial_command="claude --continue")
    ) == "claude"
    # "role:" is an agent surface but ambiguous; both detectors must run.
    assert daemon._infer_agent_kind(
        SurfaceRef(surface_id="e", title="Role: You are an expert systems engineer")
    ) is None
    assert daemon._infer_agent_kind(SurfaceRef(surface_id="f", title="vim")) is None


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


@pytest.mark.asyncio
async def test_resume_surface_returns_true_on_success(
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
    result = await daemon._resume_surface("s1")
    assert result is True
    fake_client.send_text.assert_awaited_once_with("s1", "resume\n")
    assert daemon._states["s1"].status == BlockStatus.RESUMED


@pytest.mark.asyncio
async def test_resume_surface_returns_false_for_missing_surface(
    fake_settings: Settings, fake_client, fake_scheduler
):
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    result = await daemon._resume_surface("missing")
    assert result is False
    fake_client.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_updates_agent_kind_on_re_detection(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    # Surface was previously misclassified as kimi.
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="kimi",
        detected_at=now,
        reset_at=now + timedelta(hours=1),
        status=BlockStatus.SLEEPING,
        armed=True,
        retry_count=3,
    )
    # New poll sees a Claude session-limit banner.
    daemon._observe(
        "s1",
        "claude",
        "surface:1",
        "You've hit your session limit · resets 2:30 PM PDT",
        now,
        preferred_kind="claude",
    )
    assert daemon._states["s1"].agent_kind == "claude"
    assert daemon._states["s1"].retry_count == 0


@pytest.mark.asyncio
async def test_prune_stale_states_removes_closed_and_old_rows(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["old-resumed"] = BlockState(
        surface_id="old-resumed",
        agent_kind="claude",
        detected_at=now - timedelta(hours=48),
        reset_at=now,
        status=BlockStatus.RESUMED,
        armed=True,
    )
    daemon._states["closed"] = BlockState(
        surface_id="closed",
        agent_kind="claude",
        detected_at=now,
        status=BlockStatus.RESUMED,
        armed=True,
    )
    daemon._states["live"] = BlockState(
        surface_id="live",
        agent_kind="claude",
        detected_at=now,
        status=BlockStatus.RESUMED,
        armed=True,
    )
    daemon._prune_stale_states(now, {"live"})
    assert "old-resumed" not in daemon._states
    assert "closed" not in daemon._states
    assert "live" in daemon._states


def test_list_watched_includes_healthy_and_blocked_surfaces(
    fake_settings: Settings, fake_client, fake_scheduler
):
    from ysyl.models import SurfaceRef

    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._watched["healthy"] = SurfaceRef(
        surface_id="healthy", ref="surface:1", title="ok"
    )
    daemon._watched["blocked"] = SurfaceRef(
        surface_id="blocked", ref="surface:2", title="blocked"
    )
    daemon._states["blocked"] = BlockState(
        surface_id="blocked",
        agent_kind="claude",
        detected_at=now,
        reset_at=now + timedelta(hours=1),
        status=BlockStatus.SLEEPING,
        armed=True,
        title="blocked",
        ref="surface:2",
    )
    rows = daemon.list_watched()
    by_id = {r["surface_id"]: r for r in rows}
    assert len(rows) == 2
    assert by_id["healthy"]["status"] == "healthy"
    assert by_id["healthy"]["blocked"] is False
    assert by_id["blocked"]["status"] == "sleeping"
    assert by_id["blocked"]["blocked"] is True


@pytest.mark.asyncio
async def test_poll_wait_is_capped_for_far_reset(
    temp_state: Path, fake_client, fake_scheduler
):
    """A reset hours away must NOT park the loop: the wait is capped so the daemon
    keeps re-polling other surfaces instead of sleeping for hours."""
    settings = Settings(
        state_file=str(temp_state),
        ui_enabled=False,
        max_sleep_seconds=60,
        capture_dir=str(temp_state.parent / "captures"),
    )
    # No agent surfaces this poll, so the pre-seeded sleeping block survives.
    fake_client.list_surfaces = AsyncMock(return_value=[])
    daemon = Daemon(config=settings, client=fake_client, scheduler=fake_scheduler)
    now = datetime.now(timezone.utc)
    daemon._states["s1"] = BlockState(
        surface_id="s1",
        agent_kind="claude",
        detected_at=now,
        reset_at=now + timedelta(hours=3),  # far away
        status=BlockStatus.SLEEPING,
        armed=True,
    )

    before = datetime.now(timezone.utc)
    await daemon._poll_once()

    fake_scheduler.schedule.assert_awaited_once()
    scheduled_target = fake_scheduler.schedule.await_args.args[0]
    waited = (scheduled_target - before).total_seconds()
    # Capped near max_sleep_seconds, NOT the ~3h (10800s) reset.
    assert waited <= settings.max_sleep_seconds + 2
    assert daemon._states["s1"].status == BlockStatus.SLEEPING  # still tracked
