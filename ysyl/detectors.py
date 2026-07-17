"""Text detectors for Claude and Kimi rate-limit / usage-limit banners.

The exact wording of a provider's limit banner changes over time, so detection
is deliberately forgiving and the patterns are overridable from config (see
``Settings.detector_banner_patterns``). When a banner is recognised but the
reset time cannot be parsed, the daemon falls back to a backoff schedule and
(optionally) captures the raw surface text so the patterns can be tuned against
real samples.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

try:  # zoneinfo is stdlib on 3.9+, but tzdata may be missing on some systems.
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - extremely unlikely on macOS/Linux
    ZoneInfo = None  # type: ignore[assignment]

from ysyl.models import BlockState

logger = logging.getLogger(__name__)

# Fixed-offset fallbacks for the common North-American abbreviations Claude uses.
_TZ_ABBREVIATIONS = {
    "PST": "-08:00",
    "PDT": "-07:00",
    "EST": "-05:00",
    "EDT": "-04:00",
    "CST": "-06:00",
    "CDT": "-05:00",
    "MST": "-07:00",
    "MDT": "-06:00",
    "GMT": "+00:00",
    "UTC": "+00:00",
}


def _offset_to_tzinfo(offset_str: str) -> timezone:
    """Convert an ISO offset like ``-08:00`` into a ``timezone``."""
    sign = -1 if offset_str[0] == "-" else 1
    hh, _, mm = offset_str[1:].partition(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)))


def _resolve_tz(token: str | None):
    """Resolve a timezone token (``PST`` or ``America/New_York``) to a tzinfo.

    Returns ``None`` when the token is unknown, in which case the caller assumes
    the machine's local timezone.
    """
    if not token:
        return None
    token = token.strip()
    up = token.upper()
    if up in _TZ_ABBREVIATIONS:
        return _offset_to_tzinfo(_TZ_ABBREVIATIONS[up])
    if ZoneInfo is not None and "/" in token:
        try:
            return ZoneInfo(token)
        except Exception:
            return None
    return None


class AgentDetector(Protocol):
    """Protocol for agent block detectors."""

    agent_kind: str

    def detect(self, text: str, *, now: datetime | None = None) -> BlockState | None:
        """Inspect ``text`` and return a ``BlockState`` if a block is detected."""
        ...


class ClaudeDetector:
    """Detect Claude Code usage-limit banners and parse reset times."""

    agent_kind = "claude"

    # Any of these phrases is treated as evidence the session is blocked/limited.
    _DEFAULT_BANNER_PATTERNS = (
        r"usage limit",
        r"rate limit",
        r"reached your (?:usage |weekly )?limit",
        r"you have reached",
        r"limit reached",
        r"usage will reset",
        r"usage resets",
        r"limit will reset",
        r"limit resets",
        r"\d+-hour limit",
        r"weekly limit",
        r"too many requests",
        r"please try again later",
        r"approaching (?:your )?usage limit",
        r"out of (?:usage|credits)",
    )

    # Absolute reset time: "reset at 2:30 PM PDT", "resets 3pm (America/New_York)",
    # "reset at 15:00 UTC", "resets 3pm".
    _RESET_ABSOLUTE_RE = re.compile(
        r"reset[a-z]*\s*(?:at|by)?\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*"
        r"([ap]\.?m\.?)?\s*"
        r"(?:\(([A-Za-z]+/[A-Za-z_]+)\)|([A-Za-z]{2,4}))?",
        re.IGNORECASE,
    )
    # Relative window: "resets in 5 hours", "try again in 2 hours 30 minutes".
    _RESET_RELATIVE_RE = re.compile(
        r"(?:reset[a-z]*|try again|available again)\s+in\s+"
        r"(?:(\d+)\s*hour[s]?)?\s*(?:(\d+)\s*min[a-z]*)?",
        re.IGNORECASE,
    )

    def __init__(self, extra_banner_patterns: list[str] | None = None) -> None:
        patterns = list(self._DEFAULT_BANNER_PATTERNS) + list(extra_banner_patterns or [])
        self._banner_re = re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)

    def detect(self, text: str, *, now: datetime | None = None) -> BlockState | None:
        now = now or datetime.now(timezone.utc)
        if not self._banner_re.search(text):
            return None

        reset_at = self._parse_absolute(text, now) or self._parse_relative(text, now)
        return BlockState(
            surface_id="",
            agent_kind="claude",
            detected_at=now,
            reset_at=reset_at,
            status="detected",
        )

    def _parse_absolute(self, text: str, now: datetime) -> datetime | None:
        match = self._RESET_ABSOLUTE_RE.search(text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        ampm = (match.group(3) or "").lower().replace(".", "")
        tz_token = match.group(4) or match.group(5)

        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

        tz = _resolve_tz(tz_token) or datetime.now().astimezone().tzinfo
        reference = now.astimezone(tz)
        candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def _parse_relative(self, text: str, now: datetime) -> datetime | None:
        match = self._RESET_RELATIVE_RE.search(text)
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        if hours == 0 and minutes == 0:
            return None
        return now + timedelta(hours=hours, minutes=minutes)


class KimiDetector:
    """Detect Kimi 429 / token quota / rate-limit exhausted messages."""

    agent_kind = "kimi"

    _DEFAULT_INDICATOR_PATTERNS = (
        r"429",
        r"too many requests",
        r"rate limit",
        r"token quota",
        r"quota exceeded",
        r"requests are limited",
        r"请稍后重试",
        r"try again later",
        r"retry after",
        r"insufficient quota",
    )
    _RETRY_SECONDS_RE = re.compile(
        r"(?:retry after|try again in)\s+(\d+)\s*(?:s|sec|seconds?)\b",
        re.IGNORECASE,
    )
    _RETRY_MINUTES_RE = re.compile(
        r"(?:retry after|try again in)\s+(\d+)\s*(?:m|min|minutes?)\b",
        re.IGNORECASE,
    )

    def __init__(self, extra_indicator_patterns: list[str] | None = None) -> None:
        patterns = list(self._DEFAULT_INDICATOR_PATTERNS) + list(extra_indicator_patterns or [])
        self._indicators_re = re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)

    def detect(self, text: str, *, now: datetime | None = None) -> BlockState | None:
        now = now or datetime.now(timezone.utc)
        if not self._indicators_re.search(text):
            return None

        reset_at: datetime | None = None
        seconds_match = self._RETRY_SECONDS_RE.search(text)
        if seconds_match:
            reset_at = now + timedelta(seconds=int(seconds_match.group(1)))
        else:
            minutes_match = self._RETRY_MINUTES_RE.search(text)
            if minutes_match:
                reset_at = now + timedelta(minutes=int(minutes_match.group(1)))

        return BlockState(
            surface_id="",
            agent_kind="kimi",
            detected_at=now,
            reset_at=reset_at,
            status="detected",
        )


def build_detectors(
    banner_patterns: dict[str, list[str]] | None = None,
) -> list[AgentDetector]:
    """Construct the default detector set, optionally with config-supplied extras.

    ``banner_patterns`` maps an agent kind to extra regex strings appended to
    that detector's built-in patterns.
    """
    banner_patterns = banner_patterns or {}
    return [
        ClaudeDetector(extra_banner_patterns=banner_patterns.get("claude")),
        KimiDetector(extra_indicator_patterns=banner_patterns.get("kimi")),
    ]


def detect_all(
    text: str,
    now: datetime,
    *,
    enabled: dict[str, bool] | None = None,
    detectors: list[AgentDetector] | None = None,
) -> BlockState | None:
    """Run enabled detectors in order and return the first positive match.

    Args:
        text: Surface text to inspect.
        now: Current UTC datetime.
        enabled: Map of agent name to enabled flag. Defaults to both enabled.
        detectors: Optional list of detector instances to use.
    """
    enabled = enabled or {"claude": True, "kimi": True}
    detectors = detectors if detectors is not None else build_detectors()
    for detector in detectors:
        if not enabled.get(detector.agent_kind, False):
            continue
        result = detector.detect(text, now=now)
        if result is not None:
            logger.debug("Detector %s matched", detector.agent_kind)
            return result
    return None
