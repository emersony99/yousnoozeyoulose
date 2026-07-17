"""Tests for Claude and Kimi detectors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ysyl.detectors import ClaudeDetector, KimiDetector, detect_all


NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_claude_detects_absolute_reset_time():
    text = (
        "Claude Code\n"
        "You have reached your usage limit.\n"
        "Your usage will reset at 2:30 PM PDT"
    )
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.agent_kind == "claude"
    assert result.reset_at is not None
    # PDT is UTC-7, so 14:30 PDT == 21:30 UTC
    assert result.reset_at.hour == 21


def test_claude_detects_relative_reset():
    text = "Rate limited. Usage resets in 5 hours."
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.agent_kind == "claude"
    assert result.reset_at == NOW + timedelta(hours=5)


def test_claude_ignores_unrelated_text():
    text = "Everything is working fine. No limits here."
    assert ClaudeDetector().detect(text, now=NOW) is None


def test_kimi_detects_429_with_seconds():
    text = "429 Too Many Requests. Please retry after 300s."
    result = KimiDetector().detect(text, now=NOW)
    assert result is not None
    assert result.agent_kind == "kimi"
    assert result.reset_at == NOW + timedelta(seconds=300)


def test_kimi_detects_minutes():
    text = "Token quota exceeded, try again in 5 minutes."
    result = KimiDetector().detect(text, now=NOW)
    assert result is not None
    assert result.reset_at == NOW + timedelta(minutes=5)


def test_kimi_falls_back_to_backoff_without_time():
    text = "Rate limit: requests are limited, please try again later."
    result = KimiDetector().detect(text, now=NOW)
    assert result is not None
    assert result.agent_kind == "kimi"
    assert result.reset_at is None


def test_detect_all_returns_first_enabled_match():
    text = "Claude Code\nYour usage will reset at 3:00 PM PST"
    result = detect_all(text, NOW, enabled={"claude": True, "kimi": True})
    assert result is not None
    assert result.agent_kind == "claude"


def test_detect_all_respects_disabled():
    text = "Claude Code\nYour usage will reset at 3:00 PM PST"
    result = detect_all(text, NOW, enabled={"claude": False, "kimi": True})
    assert result is None


def test_claude_parses_iana_timezone():
    # 3pm America/New_York on Jan 1 is EST (UTC-5) -> 20:00 UTC.
    text = "Claude usage limit reached. Your limit will reset at 3pm (America/New_York)."
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.reset_at is not None
    assert result.reset_at.hour == 20


def test_claude_parses_24h_utc():
    text = "You have reached your usage limit. Resets at 15:00 UTC."
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.reset_at is not None
    assert result.reset_at.hour == 15


def test_claude_parses_status_line_resets():
    # Status-line style: "5-hour limit reached ∙ resets 3pm PDT" (no "at").
    text = "5-hour limit reached ∙ resets 3pm PDT"
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.reset_at is not None  # 3pm PDT == 22:00 UTC
    assert result.reset_at.hour == 22


def test_claude_unparseable_reset_still_detects():
    text = "You have reached your usage limit. Please try again later."
    result = ClaudeDetector().detect(text, now=NOW)
    assert result is not None
    assert result.agent_kind == "claude"
    assert result.reset_at is None  # daemon applies a backoff instead
