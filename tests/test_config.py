"""Tests for settings / cmux binary resolution."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ysyl.config import Settings, _resolve_cmux_bin


def _make_exe(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_resolve_prefers_env(tmp_path, monkeypatch):
    exe = _make_exe(tmp_path / "cmux")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(exe))
    assert _resolve_cmux_bin() == str(exe)


def test_resolve_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CMUX_BUNDLED_CLI_PATH", raising=False)
    monkeypatch.delenv("CMUX_CLAUDE_HOOK_CMUX_BIN", raising=False)
    exe = _make_exe(tmp_path / "cmux")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _resolve_cmux_bin() == str(exe)


def test_settings_uses_resolved_bin(tmp_path, monkeypatch):
    exe = _make_exe(tmp_path / "cmux")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(exe))
    assert Settings(ui_enabled=False).cmux_bin == str(exe)


def test_explicit_bin_overrides_resolution(tmp_path, monkeypatch):
    exe = _make_exe(tmp_path / "cmux")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(exe))
    assert Settings(cmux_bin="/custom/cmux", ui_enabled=False).cmux_bin == "/custom/cmux"


def test_history_window_sets_prune_hours():
    s = Settings(history_window="3d", ui_enabled=False)
    assert s.prune_resumed_after_hours == 72
    assert s.prune_dismissed_after_hours == 72

    s = Settings(history_window="3w", ui_enabled=False)
    assert s.prune_resumed_after_hours == 504
    assert s.prune_dismissed_after_hours == 504


def test_history_window_rejects_invalid_value():
    import pytest
    with pytest.raises(ValueError):
        Settings(history_window="1month", ui_enabled=False)
