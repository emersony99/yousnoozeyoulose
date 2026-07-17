"""Sleep-resilient wake scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def is_past(target: datetime) -> bool:
    """Return True if ``target`` is at or before current UTC time."""
    return datetime.now(timezone.utc) >= target


class WakeScheduler:
    """Scheduler that sleeps in chunks and detects wall-clock jumps (e.g. system wake)."""

    def __init__(self, tick_seconds: int) -> None:
        self.tick_seconds = tick_seconds

    @staticmethod
    def is_past(target: datetime) -> bool:
        """Return True if ``target`` is at or before current UTC time."""
        return is_past(target)

    async def schedule(
        self,
        target_datetime: datetime,
        on_tick: Callable[[datetime], Awaitable[None]] | None = None,
    ) -> None:
        """Sleep until ``target_datetime`` in ``tick_seconds`` chunks.

        If the system sleeps, wall-clock time jumps forward and the scheduler
        returns as soon as current time is past ``target_datetime``.
        """
        while True:
            now = datetime.now(timezone.utc)
            if now >= target_datetime:
                logger.debug("Target %s reached (now=%s)", target_datetime, now)
                return
            remaining = (target_datetime - now).total_seconds()
            sleep_for = min(self.tick_seconds, max(1, remaining))
            logger.debug("Scheduler sleeping for %s seconds", sleep_for)
            await asyncio.sleep(sleep_for)
            now = datetime.now(timezone.utc)
            if on_tick is not None:
                await on_tick(now)
            if now >= target_datetime:
                logger.debug("Target %s reached after tick (now=%s)", target_datetime, now)
                return
