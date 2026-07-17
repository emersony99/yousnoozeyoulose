# YouSnoozeYouLose (ysyl)

[![CI](https://github.com/emersony99/yousnoozeyoulose/actions/workflows/ci.yml/badge.svg)](https://github.com/emersony99/yousnoozeyoulose/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

YouSnoozeYouLose is a lightweight Python daemon that watches Claude Code and Kimi
sessions running inside [cmux](https://cmux.com) surfaces, detects usage /
rate-limit blocks, parses the reset time, and automatically resumes the agent
once the quota refreshes — so a limit that hits while you're away doesn't cost
you the rest of your session.

It ships with a **minimal local dashboard** so you can see exactly what it's
locked onto and choose what gets auto-resumed.

## How it works

1. Every few seconds it lists cmux surfaces and keeps only the ones that look
   like agents — cmux tags wrapped Claude panes with `resume_binding.kind`, and
   titles/commands are matched as a fallback.
2. It reads the **live tail** of each agent surface (not the whole scrollback,
   which avoids false positives from an agent merely *discussing* rate limits)
   and runs the Claude/Kimi detectors.
3. On a detected block it parses the reset time (`reset at 3pm (America/New_York)`,
   `resets in 5 hours`, `retry after 300s`, …) and sleeps until then. The
   scheduler is resilient to laptop sleep — it wakes correctly even if the wall
   clock jumps.
4. At reset it sends **Enter** to the agent to nudge it to retry, then verifies
   on the next poll that the limit actually cleared. If it didn't, it backs off
   and retries, up to a configurable cap.

Only surfaces recognised as agents are ever read or resumed.

## Requirements

- **macOS** with the [cmux](https://cmux.com) app installed (this is where the
  agent sessions live).
- **Python 3.11+**.

## Install

Install the `ysyl` CLI with [pipx](https://pipx.pypa.io) (recommended — keeps it
isolated and on your PATH):

```bash
pipx install git+https://github.com/emersony99/yousnoozeyoulose.git
```

Or with pip:

```bash
pip install git+https://github.com/emersony99/yousnoozeyoulose.git
```

Or from a local clone (editable, for development):

```bash
git clone https://github.com/emersony99/yousnoozeyoulose.git
cd yousnoozeyoulose
pip install -e '.[dev]'
```

> **Run it inside a cmux terminal.** cmux only accepts control from terminals
> inside the app by default, so launch `ysyl run` from a cmux tab (or see
> [Troubleshooting](#troubleshooting) to allow external access). Run `ysyl doctor`
> if anything looks off.

## Usage

Run the daemon (dashboard on http://127.0.0.1:8765 by default):

```bash
ysyl run
```

Override the polling interval, scheduler tick, dashboard port, or state file:

```bash
ysyl run --interval 10 --tick 30 --ui-port 8765 --state ~/.ysyl/state.json
ysyl run --no-ui            # headless
```

Inspect tracked surfaces or dismiss one from the CLI:

```bash
ysyl status
ysyl dismiss <surface_id>
```

Diagnose cmux connectivity (run this if the daemon reports socket errors):

```bash
ysyl doctor
```

## Troubleshooting

Run **`ysyl doctor`** first — it reports the cmux binary, whether you're inside a
cmux surface, the socket control mode, the live vs. inherited socket, and a
connectivity verdict with a tailored fix.

**`Failed to write to socket (Broken pipe)`.** cmux's `automation.socketControlMode`
defaults to **`cmuxOnly`**, which only accepts control from terminals **inside the
cmux app**. If you launched `ysyl` from a normal macOS Terminal/iTerm, cmux drops
the connection. Two fixes:

- **Simplest — run ysyl inside cmux.** Open a terminal tab in the cmux app and run
  `ysyl run` there. It watches your other surfaces. No config changes.
- **Run from an external terminal.** Allow socket control in cmux:
  1. Edit `~/.config/cmux/cmux.json` (back it up first):
     ```json
     "automation": { "socketControlMode": "password", "socketPassword": "<pick-one>" }
     ```
  2. `cmux reload-config`
  3. `export CMUX_SOCKET_PASSWORD=<the-password>` then `ysyl run` (ysyl forwards it
     via `--password`). Use `"allowAll"` instead for no-password local access.

**`cmux: command not found`.** ysyl auto-resolves the binary from cmux's env vars,
PATH, and the install path, so this is rare; if it happens, set
`YSYL_CMUX_BIN=/full/path/to/cmux`.

ysyl also pins every call to the **live** socket (via cmux's `last-socket-path`)
and retries transient drops, so a stale `CMUX_SOCKET_PATH` from a cmux restart is
handled automatically.

Dump a surface's current text (useful for tuning detectors against a real limit
banner):

```bash
ysyl capture <surface_id>            # writes to ~/.ysyl/captures/
ysyl capture <surface_id> --stdout
```

## The dashboard

`ysyl run` serves a localhost-only page listing every tracked agent surface with
its status, a live **resume-in** countdown, retry count, and a text preview. Each
row has an **Armed** toggle (whether it may auto-resume), plus **Dismiss** and
**Resume now** buttons — so you always see what's about to be resumed and stay in
control of it.

## Configuration

Settings come from environment variables (prefixed `YSYL_`) or a `.ysyl.toml` in
the current or home directory. Useful keys:

| Key | Default | Purpose |
|-----|---------|---------|
| `poll_interval_seconds` | `10` | Seconds between surface polls |
| `auto_arm` | `true` | Newly-detected blocks auto-resume unless you toggle them off |
| `resume_action` | `enter` | `enter` (send Return) or `text` |
| `resume_text` | `continue` | Text sent when `resume_action = "text"` |
| `max_retries` | `5` | Resume attempts before a surface is auto-dismissed |
| `tail_lines` | `30` | How many trailing lines are scanned for a banner |
| `agent_title_patterns` | `["claude","kimi"]` | Substrings that mark a surface as an agent |
| `capture_on_detect` | `false` | Capture surface text on every detection |
| `detector_banner_patterns` | `{}` | Extra regexes appended per agent kind |
| `ui_enabled` / `ui_host` / `ui_port` | `true` / `127.0.0.1` / `8765` | Dashboard |

Because real limit wording changes, the detectors are best-effort and
**capture mode** logs surface text (to `~/.ysyl/captures/`) whenever a banner is
seen but the reset time can't be parsed — collect those samples and add exact
patterns via `detector_banner_patterns` without touching the code.

## Development

```bash
pip install -e '.[dev]'
pytest
```
