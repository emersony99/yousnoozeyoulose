"""Tests for the sleep-resilient scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from ysyl.scheduler import WakeScheduler


@pytest.mark.asyncio
async def test_scheduler_returns_immediately_when_target_in_past():
    scheduler = WakeScheduler(tick_seconds=30)
    target = datetime.now(timezone.utc) - timedelta(seconds=10)
    on_tick = AsyncMock()
    await scheduler.schedule(target, on_tick=on_tick)
    on_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_wakes_after_wall_clock_jump():
    """Simulate macOS sleep: asyncio.sleep returns but wall-clock jumped past target."""
    scheduler = WakeScheduler(tick_seconds=30)
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    target = base + timedelta(minutes=5)

    now_calls = [base, base + timedelta(seconds=1), target + timedelta(seconds=1)]

    def fake_now(tz):
        return now_calls.pop(0)

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    on_tick = AsyncMock()

    with patch("ysyl.scheduler.datetime") as mock_dt:
        mock_dt.now = fake_now
        mock_dt.timezone = timezone
        with patch("asyncio.sleep", fake_sleep):
            await scheduler.schedule(target, on_tick=on_tick)

    assert sleep_calls
    on_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_past_helper():
    assert WakeScheduler.is_past(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert not WakeScheduler.is_past(datetime.now(timezone.utc) + timedelta(hours=1))
