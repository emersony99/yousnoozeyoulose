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
        sessions_file=str(temp_state.parent / "sessions.json"),
        backfill_claude_history=False,
        backfill_codex_history=False,
        backfill_kimi_history=False,
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
async def test_daemon_detects_unlabelled_kimi_billing_cycle_limit(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    fake_client.list_surfaces = AsyncMock(return_value=[
        MagicMock(surface_id="kimi-1", title="Rewrite dashboard", ref="surface:21", type="terminal"),
    ])
    fake_client.read_surface_text = AsyncMock(return_value=(
        "Error: [provider.api_error] 403 You've reached your usage limit for this billing cycle.\n"
        "Your quota will be refreshed in the next cycle.\n"
        "https://www.kimi.com/membership/subscription?tab=quota"
    ))

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    await daemon._poll_once()

    rows = daemon.list_watched()
    assert len(rows) == 1
    assert rows[0]["agent_kind"] == "kimi"
    assert rows[0]["blocked"] is True
    assert daemon._content_agent_kind("[provider.api_error] 403 usage limit kimi.com/membership") is None


@pytest.mark.asyncio
async def test_daemon_detects_claude_stop_and_wait_quota_menu(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    fake_client.read_surface_text = AsyncMock(
        return_value=(
            "What do you want to do?\n\n"
            "  1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n"
        )
    )

    before = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    await daemon._poll_once()

    block = daemon.list_states()[0]
    assert block.agent_kind == "claude"
    assert block.status == BlockStatus.SLEEPING
    assert block.reset_at is not None
    assert block.reset_at >= before + timedelta(minutes=5)


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
async def test_prune_stale_states_keeps_history_and_removes_aged_out_rows(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    fake_settings.history_window = "3d"
    fake_settings.prune_resumed_after_hours = 72
    fake_settings.prune_dismissed_after_hours = 72
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["old-resumed"] = BlockState(
        surface_id="old-resumed",
        agent_kind="claude",
        detected_at=now - timedelta(hours=100),
        reset_at=now,
        status=BlockStatus.RESUMED,
        armed=True,
    )
    daemon._states["recent-closed"] = BlockState(
        surface_id="recent-closed",
        agent_kind="claude",
        detected_at=now - timedelta(hours=1),
        status=BlockStatus.RESUMED,
        armed=True,
    )
    daemon._states["old-closed"] = BlockState(
        surface_id="old-closed",
        agent_kind="claude",
        detected_at=now - timedelta(hours=80),
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
    assert "old-closed" not in daemon._states
    assert "recent-closed" in daemon._states
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
    assert by_id["healthy"]["is_working"] is False
    assert by_id["blocked"]["status"] == "sleeping"
    assert by_id["blocked"]["blocked"] is True


def test_list_watched_exposes_latest_message_and_only_unresolved_blocks(
    fake_settings: Settings, fake_client, fake_scheduler
):
    from ysyl.models import SurfaceRef

    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._watched["s1"] = SurfaceRef(
        surface_id="s1", resume_kind="claude", checkpoint_id="session-1"
    )
    daemon._kinds["s1"] = "claude"
    daemon._ledger["session-1"] = {
        "id": "session-1", "kind": "claude", "last_message": "implement the agent dashboard changes"
    }
    daemon._states["s1"] = BlockState(
        surface_id="s1", agent_kind="claude", detected_at=now, status=BlockStatus.RESUMED
    )

    row = daemon.list_watched()[0]
    assert row["last_message"] == "implement the agent dashboard changes"
    assert row["blocked"] is False

    daemon._states["s1"].status = BlockStatus.SLEEPING
    assert daemon.list_watched()[0]["blocked"] is True


@pytest.mark.asyncio
async def test_open_session_opens_a_supported_agent_pane(
    fake_settings: Settings, fake_client, fake_scheduler
):
    from ysyl.models import SurfaceRef

    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._ledger["session-1"] = {"id": "session-1", "kind": "claude", "cwd": "/tmp/project"}
    daemon._watched["s1"] = SurfaceRef(surface_id="s1", checkpoint_id="session-1")
    daemon._states["s1"] = BlockState(
        surface_id="s1", agent_kind="claude", detected_at=datetime.now(timezone.utc), status=BlockStatus.SLEEPING
    )
    fake_client.open_saved_session = AsyncMock(return_value=True)

    assert await daemon.open_session("session-1") is True
    fake_client.open_saved_session.assert_awaited_once_with("claude", "session-1", "/tmp/project")
    assert daemon._states["s1"].status == BlockStatus.RESUMED
    assert daemon._states["s1"].resumed_by_ysyl is True
    assert await daemon.open_session("missing") is False


def test_observe_keeps_old_banner_green_after_session_is_resumed_elsewhere(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["s1"] = BlockState(
        surface_id="s1", agent_kind="claude", detected_at=now, status=BlockStatus.RESUMED,
        resumed_by_ysyl=True,
    )

    daemon._observe("s1", "claude", "surface:1", "You've hit your usage limit. Try again in 2 hours.", now)
    assert daemon._states["s1"].status == BlockStatus.RESUMED


def test_help_summary_groups_recent_interventions_by_day(
    fake_settings: Settings, fake_client, fake_scheduler
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._states["recovered"] = BlockState(
        surface_id="recovered", agent_kind="claude", detected_at=now, status=BlockStatus.RESUMED,
        resumed_at=now,
    )
    daemon._states["waiting"] = BlockState(
        surface_id="waiting", agent_kind="codex", detected_at=now, status=BlockStatus.SLEEPING
    )
    daemon._ledger["prompt-session"] = {"id": "prompt-session", "prompt_times": [now.isoformat()]}

    summary = daemon.help_summary(now)
    assert summary["detected"] == 2
    assert summary["recovered"] == 1
    assert summary["waiting"] == 1
    assert summary["prompts"] == 1
    cell = summary["heatmap"][-1]["hours"][now.astimezone().hour]
    assert cell == {"interventions": 2, "prompts": 1, "resumes": {"claude": 1, "codex": 0, "kimi": 0}}


@pytest.mark.asyncio
async def test_changed_surface_output_is_reported_as_working(
    temp_state: Path, fake_settings: Settings, fake_client, fake_scheduler
):
    fake_client.read_surface_text = AsyncMock(
        side_effect=["Claude Code\nWaiting for input", "Claude Code\nWriting the change"]
    )
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)

    await daemon._poll_once()
    assert daemon.list_watched()[0]["is_working"] is False

    await daemon._poll_once()
    assert daemon.list_watched()[0]["is_working"] is True

    daemon._activity_changed_at["s1"] = datetime.now(timezone.utc) - timedelta(seconds=16)
    assert daemon.list_watched()[0]["is_working"] is False


def test_list_watched_includes_historical_closed_sessions(
    fake_settings: Settings, fake_client, fake_scheduler
):
    from ysyl.models import SurfaceRef

    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)
    daemon._watched["live"] = SurfaceRef(
        surface_id="live", ref="surface:1", title="live"
    )
    daemon._states["live"] = BlockState(
        surface_id="live",
        agent_kind="claude",
        detected_at=now,
        status=BlockStatus.SLEEPING,
        armed=True,
    )
    daemon._states["closed"] = BlockState(
        surface_id="closed",
        agent_kind="kimi",
        detected_at=now,
        status=BlockStatus.RESUMED,
        armed=True,
        title="old session",
        ref="surface:99",
    )
    rows = daemon.list_watched()
    by_id = {r["surface_id"]: r for r in rows}
    assert len(rows) == 2
    assert by_id["live"]["live"] is True
    assert by_id["closed"]["live"] is False
    assert by_id["closed"]["status"] == "resumed"
    assert by_id["closed"]["is_working"] is False


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


@pytest.mark.asyncio
async def test_session_ledger_records_live_and_backfills_claude(
    fake_settings: Settings, fake_client, fake_scheduler, tmp_path: Path
):
    from ysyl.models import SurfaceRef

    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)

    # A live Claude surface is keyed in the ledger by its checkpoint (session) id.
    watched = {
        "s1": SurfaceRef(
            surface_id="s1", ref="surface:1", title="build agent",
            resume_kind="claude", checkpoint_id="abc123", cwd="/tmp/proj",
        )
    }
    daemon._update_ledger(now, watched, {"s1": "npm run build | ✓ done"})
    live = next(s for s in daemon.list_sessions() if s["id"] == "abc123")
    assert live["open"] and live["kind"] == "claude"
    assert live["activity"] == "npm run build | ✓ done"

    # Backfill a past Claude session from disk (a closed one ysyl never watched live).
    proj = tmp_path / "claude" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "sess-xyz.jsonl").write_text(
        json.dumps({
            "type": "user", "timestamp": now.isoformat(), "cwd": "/tmp/proj",
            "message": {"content": [{"type": "text", "text": "clean up the codebase please"}]},
        }) + "\n" + json.dumps({
            "type": "user", "timestamp": (now + timedelta(minutes=1)).isoformat(), "cwd": "/tmp/proj",
            "message": {"content": [{"type": "text", "text": "now run the final checks"}]},
        }) + "\n",
        encoding="utf-8",
    )
    daemon.config.claude_projects_dir = str(tmp_path / "claude")
    daemon.config.backfill_claude_history = True
    daemon._backfill_claude_sessions(now)

    hist = next(s for s in daemon.list_sessions() if s["id"] == "sess-xyz")
    assert hist["kind"] == "claude" and not hist["open"]
    assert "clean up the codebase" in (hist["title"] or "")
    assert hist["last_message"] == "now run the final checks"
    assert hist["prompt_times"] == [now.isoformat(), (now + timedelta(minutes=1)).isoformat()]
    # Open sessions sort ahead of closed history.
    assert daemon.list_sessions()[0]["id"] == "abc123"


@pytest.mark.asyncio
async def test_backfill_codex_and_kimi_history(
    fake_settings: Settings, fake_client, fake_scheduler, tmp_path: Path
):
    now = datetime.now(timezone.utc)
    daemon = Daemon(config=fake_settings, client=fake_client, scheduler=fake_scheduler)

    # Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    cdir = tmp_path / "codex" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    cdir.mkdir(parents=True)
    (cdir / "rollout-x.jsonl").write_text(
        json.dumps({"type": "session_meta", "timestamp": now.isoformat(),
                    "payload": {"id": "cdx1", "cwd": "/proj", "timestamp": now.isoformat()}}) + "\n"
        + json.dumps({"type": "event_msg",
                      "timestamp": now.isoformat(),
                      "payload": {"type": "user_message", "message": "fix the bug in main"}}) + "\n"
        + json.dumps({"type": "event_msg",
                      "timestamp": (now + timedelta(minutes=1)).isoformat(),
                      "payload": {"type": "user_message", "message": "run the final tests"}}) + "\n",
        encoding="utf-8",
    )
    daemon.config.codex_sessions_dir = str(tmp_path / "codex")
    daemon.config.backfill_codex_history = True

    # Kimi: session_index.jsonl -> sessionDir/state.json
    sdir = tmp_path / "kimi" / "session_abc"
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text(
        json.dumps({"title": "add dark mode", "createdAt": now.isoformat(),
                    "updatedAt": now.isoformat(), "workDir": "/proj"}), encoding="utf-8",
    )
    idx = tmp_path / "kimi_index.jsonl"
    idx.write_text(json.dumps({"sessionId": "kmi1", "sessionDir": str(sdir), "workDir": "/proj"}) + "\n",
                   encoding="utf-8")
    daemon.config.kimi_index_file = str(idx)
    daemon.config.backfill_kimi_history = True

    daemon._backfill_history(now)
    by_id = {s["id"]: s for s in daemon.list_sessions()}

    assert by_id["cdx1"]["kind"] == "codex" and "fix the bug" in by_id["cdx1"]["title"]
    assert by_id["cdx1"]["last_message"] == "run the final tests"
    assert by_id["cdx1"]["prompt_times"] == [now.isoformat(), (now + timedelta(minutes=1)).isoformat()]
    assert not by_id["cdx1"]["open"]
    assert by_id["kmi1"]["kind"] == "kimi" and by_id["kmi1"]["title"] == "add dark mode"
    assert not by_id["kmi1"]["open"]


def test_codex_is_a_recognized_agent_and_detector():
    from ysyl.detectors import build_detectors, detect_all
    from datetime import datetime as _dt, timezone as _tz
    kinds = {d.agent_kind for d in build_detectors()}
    assert {"claude", "codex", "kimi"} <= kinds
    # A Codex limit banner is detected and tagged codex when that detector is preferred.
    block = detect_all("You've hit your usage limit. Try again in 2 hours.",
                       _dt.now(_tz.utc), preferred_kind="codex")
    assert block is not None and block.agent_kind == "codex"


def test_active_seconds_excludes_idle_gaps():
    from ysyl.daemon import _active_seconds
    base = 1_000_000.0
    # events at 0,60,120s (active), then a 2h idle gap, then 0,30s (active)
    epochs = [base, base + 60, base + 120, base + 7200, base + 7230]
    # gaps: 60,60 (active) + 7080 (idle, > cap, dropped) + 30 (active) = 150s
    assert _active_seconds(epochs, idle_cap=300) == 150
    assert _active_seconds([], idle_cap=300) == 0
    assert _active_seconds([base], idle_cap=300) == 0
