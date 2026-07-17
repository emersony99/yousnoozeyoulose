"""Command-line interface for YouSnoozeYouLose."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from ysyl import __version__
from ysyl.config import Settings
from ysyl.daemon import Daemon


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ysyl",
        description="YouSnoozeYouLose — auto-resume Claude and Kimi after rate limits.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the daemon.")
    run_parser.add_argument("--config", type=Path, help="Path to a TOML config file.")
    run_parser.add_argument("--state", type=str, help="Path to the state JSON file.")
    run_parser.add_argument("--interval", type=int, help="Poll interval in seconds.")
    run_parser.add_argument("--tick", type=int, help="Scheduler tick in seconds.")
    run_parser.add_argument("--debug", action="store_true", help="Enable debug captures and verbose logging.")
    run_parser.add_argument("--no-ui", action="store_true", help="Disable the local dashboard.")
    run_parser.add_argument("--ui-host", type=str, help="Dashboard bind host (default 127.0.0.1).")
    run_parser.add_argument("--ui-port", type=int, help="Dashboard port (default 8765).")

    status_parser = subparsers.add_parser("status", help="Print persisted block states.")
    status_parser.add_argument("--state", type=str, help="Path to the state JSON file.")
    status_parser.add_argument("--json", action="store_true", help="Emit raw JSON.")

    dismiss_parser = subparsers.add_parser("dismiss", help="Dismiss a block state.")
    dismiss_parser.add_argument("surface_id", type=str, help="Surface ID to dismiss.")
    dismiss_parser.add_argument("--state", type=str, help="Path to the state JSON file.")

    capture_parser = subparsers.add_parser(
        "capture", help="Dump a surface's current text (for tuning detectors)."
    )
    capture_parser.add_argument("surface_id", type=str, help="Surface ID to read.")
    capture_parser.add_argument("--config", type=Path, help="Path to a TOML config file.")
    capture_parser.add_argument(
        "--stdout", action="store_true", help="Print to stdout instead of the capture dir."
    )

    subparsers.add_parser("doctor", help="Diagnose cmux connectivity.")

    return parser


def _load_settings(args: argparse.Namespace) -> Settings:
    kwargs: dict[str, object] = {}
    if getattr(args, "state", None):
        kwargs["state_file"] = args.state
    if getattr(args, "interval", None):
        kwargs["poll_interval_seconds"] = args.interval
    if getattr(args, "tick", None):
        kwargs["sleep_tick_seconds"] = args.tick
    if getattr(args, "debug", False):
        kwargs["debug_mode"] = True
        kwargs["log_level"] = "DEBUG"
    if getattr(args, "no_ui", False):
        kwargs["ui_enabled"] = False
    if getattr(args, "ui_host", None):
        kwargs["ui_host"] = args.ui_host
    if getattr(args, "ui_port", None):
        kwargs["ui_port"] = args.ui_port

    if getattr(args, "config", None):
        class _ExplicitSettings(Settings):
            model_config = Settings.model_config.copy()
            model_config["toml_file"] = [args.config]

        return _ExplicitSettings(**kwargs)
    return Settings(**kwargs)


def _fmt_countdown(reset_at: str | None) -> str:
    if not reset_at:
        return "—"
    try:
        target = datetime.fromisoformat(reset_at)
    except ValueError:
        return reset_at
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "due"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _cmd_run(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    settings.setup_logging()
    daemon = Daemon(config=settings)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Interrupted by user")
    return 0


def _read_blocks(state_file: Path) -> list[dict]:
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [b for b in data if isinstance(b, dict)]
    if isinstance(data, dict):
        return [b for b in data.get("blocks", []) if isinstance(b, dict)]
    return []


def _cmd_status(args: argparse.Namespace) -> int:
    settings = Settings(state_file=args.state) if args.state else Settings()
    daemon = Daemon(config=settings)
    daemon._load_state()

    if getattr(args, "json", False):
        print(json.dumps([b.to_state_dict() for b in daemon.list_states()], indent=2))
        return 0

    # Try to list live surfaces so healthy agent tabs are shown too.
    try:
        surfaces = asyncio.run(daemon.client.list_surfaces())
        daemon._watched = {
            s.surface_id: s for s in surfaces if daemon._is_agent_surface(s)
        }
    except Exception as exc:
        if settings.log_level == "DEBUG":
            print(f"Could not list live surfaces: {exc}", file=sys.stderr)

    rows = daemon.list_watched()
    if not rows:
        # cmux may be unreachable; fall back to persisted block states.
        rows = [
            {
                "surface_id": b.surface_id,
                "ref": b.ref,
                "title": b.title,
                "agent_kind": b.agent_kind,
                "status": b.status,
                "armed": b.armed,
                "reset_at": b.reset_at.isoformat() if b.reset_at else None,
                "retry_count": b.retry_count,
                "blocked": True,
            }
            for b in daemon.list_states()
        ]

    if not rows:
        print("No tracked surfaces.")
        return 0

    header = f"{'SURFACE':<30} {'AGENT':<7} {'STATUS':<10} {'ARMED':<6} {'RESUMES IN':<12} {'TRIES':<5}"
    print(header)
    print("-" * len(header))
    for r in rows:
        name = (r.get("title") or r.get("ref") or r.get("surface_id") or "")[:29]
        armed = "yes" if r.get("armed", True) else "no"
        print(
            f"{name:<30} {str(r.get('agent_kind') or ''):<7} "
            f"{str(r.get('status','')):<10} {armed:<6} "
            f"{_fmt_countdown(r.get('reset_at')):<12} {r.get('retry_count',0):<5}"
        )
    return 0


def _cmd_dismiss(args: argparse.Namespace) -> int:
    settings = Settings(state_file=args.state) if args.state else Settings()
    daemon = Daemon(config=settings)
    daemon._load_state()
    if daemon.dismiss(args.surface_id):
        print(f"Dismissed {args.surface_id}")
        return 0
    print(f"Surface {args.surface_id} not found", file=sys.stderr)
    return 1


def _cmd_capture(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    from ysyl.cmux_client import CmuxClient, CmuxError

    async def _run() -> str:
        client = CmuxClient(settings.cmux_bin)
        return await client.read_surface_text(args.surface_id)

    try:
        text = asyncio.run(_run())
    except CmuxError as exc:
        print(f"Failed to read surface: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "stdout", False):
        print(text)
        return 0

    directory = Path(settings.capture_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    safe = args.surface_id.replace("/", "_")
    path = directory / f"{safe}-{stamp}.txt"
    path.write_text(f"# ysyl manual capture surface={args.surface_id} at={stamp}\n\n{text}", encoding="utf-8")
    print(f"Captured -> {path}")
    return 0


def _read_cmux_control_mode() -> str:
    """Best-effort read of cmux's automation.socketControlMode from cmux.json."""
    candidates = [
        Path.home() / ".config/cmux/cmux.json",
        Path.home() / ".config/cmux/settings.json",
        Path.home() / "Library/Application Support/com.cmuxterm.app/settings.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        mode = (data.get("automation") or {}).get("socketControlMode")
        if mode:
            return str(mode)
    return "cmuxOnly"  # cmux's default when unset


def _cmd_doctor(args: argparse.Namespace) -> int:
    import os
    import shutil
    from ysyl.cmux_client import CmuxClient

    settings = Settings()
    inherited = os.environ.get("CMUX_SOCKET_PATH", "<unset>")
    live = CmuxClient.live_socket_path() or "<not found>"
    in_cmux = bool(os.environ.get("CMUX_SURFACE_ID"))
    mode = _read_cmux_control_mode()
    bin_ok = Path(settings.cmux_bin).is_file() or bool(shutil.which(settings.cmux_bin))
    external_ok = mode.lower() in ("password", "allowall", "openaccess", "fullopenaccess", "automation", "full")

    print("ysyl doctor")
    print(f"  cmux binary          : {settings.cmux_bin}")
    print(f"  cmux binary exists   : {bin_ok}")
    print(f"  inside cmux surface  : {'yes' if in_cmux else 'no'}")
    print(f"  socketControlMode    : {mode}")
    print(f"  CMUX_SOCKET_PATH     : {inherited}")
    print(f"  live socket          : {live}")
    print(f"  socket password set  : {'yes' if settings.cmux_password else 'no'}")
    if inherited not in ("<unset>", live) and live != "<not found>":
        print("  ! inherited socket differs from the live socket — ysyl pins the live one")

    client = CmuxClient(
        settings.cmux_bin, retries=settings.cmux_retries, password=settings.cmux_password
    )
    ok, detail = asyncio.run(client.check())
    print(f"  connectivity         : {'OK' if ok else 'FAILED'} — {detail}")

    if ok:
        print("\nAll good. `ysyl run` should work here.")
        return 0

    print("\nCould not reach cmux.\n")
    if not in_cmux and mode == "cmuxOnly":
        print(
            "Root cause: cmux's socketControlMode is 'cmuxOnly', so it only accepts\n"
            "control from terminals INSIDE the cmux app — and this shell is outside it.\n\n"
            "Pick one:\n"
            "  A) Simplest — run ysyl inside cmux: open a terminal tab in the cmux app\n"
            "     and run `ysyl run` there. No config changes.\n"
            "  B) Run from this external terminal — allow socket control in cmux:\n"
            "       1. Edit ~/.config/cmux/cmux.json, set:\n"
            '            \"automation\": { \"socketControlMode\": \"password\",\n'
            '                            \"socketPassword\": \"<choose-a-password>\" }\n'
            "       2. Run: cmux reload-config\n"
            "       3. Give ysyl the password:\n"
            "            export CMUX_SOCKET_PASSWORD=<the-password>   # then `ysyl run`\n"
            "     (Or use \"allowAll\" instead of password mode for no-password local access.)"
        )
    else:
        print(
            "Try:\n"
            "  • Open a fresh terminal inside the current cmux app window.\n"
            f"  • Confirm the binary works: {settings.cmux_bin} ping\n"
            f"  • If socketControlMode is 'password', set CMUX_SOCKET_PASSWORD (currently "
            f"{'set' if settings.cmux_password else 'unset'})."
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``ysyl`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "dismiss":
        return _cmd_dismiss(args)
    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
