# YouSnoozeYouLose — Architecture Plan

## Overview
A lightweight open-source Python daemon that watches Claude Code and Kimi sessions inside cmux panes, detects usage/rate-limit blocks, parses the reset time, and automatically resumes the agent exactly when the quota refreshes.

## Components

### `ysyl/models.py`
Data classes used across the daemon:
- `SurfaceRef`: `surface_id`, `pane_ref`, `workspace_ref`, `window_ref`, `title`, `initial_command`
- `BlockState`: `surface_id`, `agent_kind` (`claude` | `kimi`), `detected_at` (ISO timestamp), `reset_at` (ISO timestamp | null), `retry_count`, `status` (`detected` | `sleeping` | `resumed` | `dismissed`)

### `ysyl/config.py`
- Pydantic `Settings` from environment / optional `.ysyl.toml`
- Fields: `poll_interval_seconds`, `sleep_tick_seconds`, `cmux_bin`, `state_file`, `log_level`, `agents` map of agent names to detection enable flags

### `ysyl/cmux_client.py`
Async wrapper around `cmux rpc` subprocess:
- `list_surfaces() -> list[SurfaceRef]` via `surface.list`
- `read_surface_text(surface_id: str) -> str` via `surface.read_text`
- `send_key(surface_id: str, key: str)` via `surface.send_key`
- `send_text(surface_id: str, text: str)` via `surface.send_text`

### `ysyl/detectors.py`
Detector protocol / base class and implementations:
- `AgentDetector.detect(text: str) -> BlockState | None`
- `ClaudeDetector`: regexes for the usage-limit banner and the 5-hour rolling window reset line. Parses "Your usage will reset at HH:MM AM/PM PST/PDT" or relative window.
- `KimiDetector`: regexes for 429 / token quota / rate-limit messages. Extracts retry-after seconds or minutes; falls back to exponential backoff state.

### `ysyl/scheduler.py`
Sleep-resilient wake scheduler:
- `WakeScheduler.schedule(target_datetime: datetime, on_tick=None)` sleeps in `sleep_tick_seconds` chunks, comparing wall-clock time after each chunk. If system sleeps, wall-clock jump is detected and the scheduler returns immediately when past target.
- `schedule(target, on_tick)` is async.

### `ysyl/daemon.py`
Main loop:
- Load persistent state JSON (so daemon survives restart)
- Every `poll_interval_seconds`: list surfaces, read text, run detectors
- On new block: log, update state, schedule wake
- On wake: send resume keystroke/text, update state
- Handle signals (SIGTERM/SIGINT) gracefully

### `tests/`
Unit tests with `pytest`:
- `test_cmux_client.py`: mock subprocess RPC calls
- `test_detectors.py`: sample Claude / Kimi output fixtures
- `test_scheduler.py`: mock time/sleep to verify wake-after-sleep behavior
- `test_daemon.py`: end-to-end with mocked client/scheduler

## Packaging
- `pyproject.toml` with hatchling, Python >=3.11, no heavy deps (stdlib + pydantic + pytest optional)
- Console entry point: `ysyl`

## CLI
```
ysyl run [--config path] [--state path] [--interval 10] [--tick 30]
ysyl status [--state path]
ysyl dismiss <surface_id> [--state path]
```
