"""Minimal localhost dashboard for YouSnoozeYouLose.

A tiny stdlib-only HTTP server (no extra deps) that runs inside the daemon's
event loop and shares its ``_states`` directly. It shows what YSYL is locked
onto and what it will auto-resume, and lets you arm/dismiss/resume each surface.

Routes:
  GET  /              -> self-contained HTML dashboard
  GET  /api/state     -> JSON snapshot of tracked surfaces
  POST /api/arm       -> {"surface_id": str, "armed": bool}
  POST /api/dismiss   -> {"surface_id": str}
  POST /api/resume_now-> {"surface_id": str}
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ysyl.webui")


class WebUI:
    """Localhost-only dashboard bound to the daemon."""

    def __init__(self, daemon: Any, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.daemon = daemon
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        """A JSON-friendly view of all watched agent surfaces."""
        now = datetime.now(timezone.utc)
        rows = self.daemon.list_watched()
        # Blocked / sleeping / armed first, then healthy.
        rows.sort(
            key=lambda r: (
                0 if r.get("blocked") else 1,
                0 if (r["status"] == "sleeping" and r["armed"]) else 1,
                r["seconds_until_reset"] if r["seconds_until_reset"] is not None else 1 << 30,
            )
        )
        sessions = []
        if hasattr(self.daemon, "list_sessions"):
            sessions = self.daemon.list_sessions()
        impact = self.daemon.help_summary(now) if hasattr(self.daemon, "help_summary") else {}
        return {"now": now.isoformat(), "surfaces": rows, "sessions": sessions, "impact": impact}

    # --------------------------------------------------------------- routing
    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        """Handle one request; returns (status_code, content_type, body_bytes)."""
        path = path.split("?", 1)[0]
        if method == "GET" and path == "/":
            return 200, "text/html; charset=utf-8", _PAGE.encode("utf-8")
        if method == "GET" and path == "/api/state":
            return 200, "application/json", json.dumps(self.snapshot()).encode("utf-8")
        if method == "GET" and path == "/api/settings":
            return 200, "application/json", json.dumps(self.daemon.get_ui_settings()).encode("utf-8")

        if method == "POST" and path == "/api/settings":
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return 400, "application/json", b'{"ok": false, "error": "bad json"}'
            allowed = {"history_window", "color_theme"}
            updates = {k: v for k, v in data.items() if k in allowed}
            try:
                self.daemon.save_ui_settings(updates)
            except ValueError as exc:
                return 400, "application/json", json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            return 200, "application/json", json.dumps({"ok": True, "settings": self.daemon.get_ui_settings()}).encode("utf-8")

        if method == "POST" and path in ("/api/arm", "/api/dismiss", "/api/resume_now", "/api/open_session"):
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return 400, "application/json", b'{"ok": false, "error": "bad json"}'
            if path == "/api/open_session":
                session_id = data.get("session_id")
                if not session_id:
                    return 400, "application/json", b'{"ok": false, "error": "session_id required"}'
                pane_opened = await self.daemon.open_session(session_id)
                return 200, "application/json", json.dumps({"ok": True, "pane_opened": pane_opened}).encode("utf-8")

            surface_id = data.get("surface_id")
            if not surface_id:
                return 400, "application/json", b'{"ok": false, "error": "surface_id required"}'

            if path == "/api/arm":
                ok = self.daemon.arm(surface_id, bool(data.get("armed", True)))
            elif path == "/api/dismiss":
                ok = self.daemon.dismiss(surface_id)
            else:  # /api/resume_now
                ok = await self.daemon._resume_surface(surface_id)
            return 200, "application/json", json.dumps({"ok": bool(ok)}).encode("utf-8")

        return 404, "application/json", b'{"ok": false, "error": "not found"}'

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            except (asyncio.TimeoutError, TimeoutError):
                return
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            body = b""
            length = int(headers.get("content-length", 0) or 0)
            if length > 0:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=10)

            status, content_type, payload = await self._route(method, path, body)
            self._write(writer, status, content_type, payload)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # never let a bad request crash the daemon
            logger.debug("web request error: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    def _write(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1"))
        writer.write(body)


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouSnoozeYouLose | Recovery Desk</title>
<style>
  :root {
    color-scheme: light;
    --canvas: #f5f0e5;
    --canvas-deep: #e8eee7;
    --surface: rgba(255, 253, 247, 0.88);
    --surface-strong: #fffdf8;
    --ink: #16251f;
    --ink-soft: #405049;
    --muted: #718078;
    --line: #d4ded4;
    --line-strong: #b9c9bd;
    --teal: #08675e;
    --teal-deep: #075149;
    --teal-soft: #d8efe9;
    --green: #28734a;
    --green-soft: #dff1e5;
    --amber: #a96000;
    --amber-soft: #fff0d8;
    --red: #a63d2d;
    --red-soft: #fde4de;
    --stone: #6d736e;
    --stone-soft: #ecebe5;
    --shadow: 0 18px 42px rgba(41, 62, 50, 0.1);
    --display: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
    --sans: "Avenir Next", "Trebuchet MS", sans-serif;
    --mono: "SFMono-Regular", "Roboto Mono", Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --canvas: #11201c;
      --canvas-deep: #172b25;
      --surface: rgba(25, 43, 36, 0.9);
      --surface-strong: #1b3028;
      --ink: #edf4ed;
      --ink-soft: #c3d1c8;
      --muted: #99aaa0;
      --line: #365046;
      --line-strong: #4b685a;
      --teal: #75cfbd;
      --teal-deep: #9ee5d5;
      --teal-soft: #173f37;
      --green: #83d4a3;
      --green-soft: #1d442e;
      --amber: #ffc66e;
      --amber-soft: #513d1c;
      --red: #ff9b88;
      --red-soft: #50291f;
      --stone: #bdc7bf;
      --stone-soft: #35433c;
      --shadow: 0 18px 42px rgba(0, 0, 0, 0.2);
    }
  }
  /* Palette options keep semantic red/amber/green stable while changing the
     surrounding hue family, so recognition does not depend on preference. */
  body[data-theme="midnight"] { color-scheme: dark; --canvas: #101827; --canvas-deep: #172338; --surface: rgba(22, 34, 53, 0.92); --surface-strong: #1a2940; --ink: #edf4ff; --ink-soft: #c7d6ea; --muted: #91a4bd; --line: #324764; --line-strong: #48627f; --teal: #61d1c4; --teal-deep: #96ede0; --teal-soft: #163f42; --green: #83d7a6; --green-soft: #1d4939; --amber: #ffc76e; --amber-soft: #543f1d; --red: #ff9aa2; --red-soft: #512b37; --stone: #b8c4d4; --stone-soft: #303f55; }
  body[data-theme="forest"] { color-scheme: dark; --canvas: #0d211b; --canvas-deep: #143128; --surface: rgba(20, 48, 39, 0.94); --surface-strong: #1a3c31; --ink: #e4f5e9; --ink-soft: #bfdccc; --muted: #91b6a0; --line: #315d4b; --line-strong: #4a7962; --teal: #63d7bf; --teal-deep: #9aecda; --teal-soft: #174b40; --green: #86dda3; --green-soft: #1d5438; --amber: #ffd071; --amber-soft: #59451d; --red: #ff9f98; --red-soft: #592d31; --stone: #bdcec3; --stone-soft: #2d5142; }
  body[data-theme="moss"] { color-scheme: dark; --canvas: #1d2113; --canvas-deep: #2a3119; --surface: rgba(43, 51, 25, 0.94); --surface-strong: #374221; --ink: #f0f3d9; --ink-soft: #d0d7ad; --muted: #abb587; --line: #59693a; --line-strong: #75894b; --teal: #74d4b5; --teal-deep: #a5ead3; --teal-soft: #245043; --green: #a2d56d; --green-soft: #3d5924; --amber: #ffcf70; --amber-soft: #604716; --red: #ffa29a; --red-soft: #5c2c2e; --stone: #c8cfac; --stone-soft: #4b5430; }
  body[data-theme="ocean"] { color-scheme: dark; --canvas: #0c1c30; --canvas-deep: #102a45; --surface: rgba(17, 42, 69, 0.94); --surface-strong: #163754; --ink: #e4f2ff; --ink-soft: #bed7ed; --muted: #8fb4d0; --line: #2f5c82; --line-strong: #477ba8; --teal: #5cdaeb; --teal-deep: #9bedf4; --teal-soft: #124f61; --green: #78ddae; --green-soft: #1a543d; --amber: #ffd079; --amber-soft: #5f471a; --red: #ffa1a9; --red-soft: #5b2d3a; --stone: #b8cfdf; --stone-soft: #294964; }
  body[data-theme="orchid"] { color-scheme: dark; --canvas: #21152d; --canvas-deep: #311d43; --surface: rgba(49, 29, 67, 0.94); --surface-strong: #412657; --ink: #f5e9ff; --ink-soft: #ddc9ed; --muted: #b89acf; --line: #694987; --line-strong: #8a61af; --teal: #70d9ca; --teal-deep: #a7eee2; --teal-soft: #1d514c; --green: #98d79e; --green-soft: #31543c; --amber: #ffd076; --amber-soft: #60461c; --red: #ffa1af; --red-soft: #5f2c42; --stone: #d3bce3; --stone-soft: #53376d; }
  * { box-sizing: border-box; }
  html { min-height: 100%; }
  body {
    min-height: 100vh;
    margin: 0;
    color: var(--ink);
    background:
      radial-gradient(circle at 5% 0%, rgba(245, 185, 90, 0.24), transparent 28rem),
      radial-gradient(circle at 96% 6%, rgba(66, 174, 145, 0.16), transparent 31rem),
      linear-gradient(135deg, var(--canvas), var(--canvas-deep));
    font: 14px/1.5 var(--sans);
  }
  body::before {
    position: fixed;
    z-index: -1;
    inset: 0;
    content: "";
    pointer-events: none;
    opacity: 0.34;
    background-image: linear-gradient(rgba(56, 88, 72, 0.055) 1px, transparent 1px),
      linear-gradient(90deg, rgba(56, 88, 72, 0.055) 1px, transparent 1px);
    background-size: 28px 28px;
    mask-image: linear-gradient(to bottom, black, transparent 85%);
  }
  button, select, input { font: inherit; }
  button { cursor: pointer; }
  button:focus-visible, select:focus-visible, input:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--teal) 42%, transparent);
    outline-offset: 2px;
  }
  .page-shell { width: min(1440px, 100%); margin: 0 auto; padding: 24px clamp(16px, 3vw, 42px) 46px; }
  .topbar {
    display: flex;
    align-items: center;
    gap: 18px;
    padding-bottom: 22px;
    border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .brand-mark {
    display: grid;
    width: 42px;
    height: 42px;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid color-mix(in srgb, var(--teal) 36%, var(--line));
    border-radius: 14px 14px 14px 4px;
    color: var(--teal-deep);
    background: var(--teal-soft);
    box-shadow: inset 0 0 0 4px color-mix(in srgb, var(--teal-soft) 70%, transparent);
    font: 700 12px/1 var(--mono);
    letter-spacing: 0.08em;
  }
  .eyebrow {
    margin: 0 0 2px;
    color: var(--teal);
    font: 700 10px/1.2 var(--mono);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  h1 { margin: 0; font: 700 clamp(21px, 3vw, 29px)/1 var(--display); letter-spacing: -0.035em; }
  h1 span { color: var(--muted); font-weight: 400; }
  .topbar-actions { display: flex; align-items: center; gap: 12px; margin-left: auto; }
  .connection {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 34px;
    padding: 0 11px;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--ink-soft);
    background: color-mix(in srgb, var(--surface-strong) 75%, transparent);
    font-size: 12px;
    white-space: nowrap;
  }
  .connection::before {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    content: "";
    background: var(--green);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--green) 15%, transparent);
  }
  .connection.is-offline::before { background: var(--red); box-shadow: 0 0 0 4px color-mix(in srgb, var(--red) 16%, transparent); }
  .settings { display: flex; align-items: center; gap: 8px; color: var(--ink-soft); font-size: 12px; }
  .settings label { font-weight: 700; white-space: nowrap; }
  .settings select {
    min-height: 34px;
    padding: 0 28px 0 10px;
    border: 1px solid var(--line-strong);
    border-radius: 9px;
    color: var(--ink);
    background: var(--surface-strong);
  }
  .help-button { min-height: 34px; padding: 0 11px; border: 1px solid var(--line); border-radius: 9px; color: var(--ink-soft); background: var(--surface-strong); font: 700 11px/1 var(--sans); }
  .help-button:hover, .help-button:focus-visible { border-color: var(--teal); color: var(--ink); outline: none; }
  main { padding-top: clamp(24px, 4vw, 46px); }
  .hero-grid { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, 0.7fr); gap: 22px; align-items: stretch; }
  .hero-copy { padding: clamp(8px, 1vw, 18px) 0; }
  .hero-copy h2 { max-width: 730px; margin: 7px 0 12px; font: 700 clamp(38px, 5.6vw, 74px)/0.94 var(--display); letter-spacing: -0.065em; }
  .hero-copy h2 em { color: var(--teal); font-weight: inherit; }
  .hero-copy p:last-child { max-width: 560px; margin: 0; color: var(--ink-soft); font-size: 15px; }
  .signal-panel, .panel, .kpi-card {
    border: 1px solid color-mix(in srgb, var(--line) 84%, transparent);
    background: var(--surface);
    box-shadow: var(--shadow);
    backdrop-filter: blur(14px);
  }
  .signal-panel { display: flex; flex-direction: column; justify-content: space-between; min-height: 205px; padding: 22px; border-radius: 22px; }
  .signal-panel h3, .panel-title { margin: 0; font: 700 17px/1.1 var(--display); letter-spacing: -0.02em; }
  .signal-panel p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
  .mix-bar { display: flex; min-height: 16px; overflow: hidden; margin: 26px 0 13px; border-radius: 999px; background: var(--stone-soft); }
  .mix-segment { min-width: 0; transition: flex-basis 220ms ease; }
  .mix-segment--blocked { background: var(--red); }
  .mix-segment--working { background: var(--amber); }
  .mix-segment--ongoing { background: var(--green); }
  .mix-legend { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .legend-item { min-width: 0; }
  .legend-value { display: block; color: var(--ink); font: 700 18px/1.1 var(--mono); font-variant-numeric: tabular-nums; }
  .legend-label { display: block; overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-top: 28px; }
  .kpi-card { position: relative; min-height: 160px; overflow: hidden; padding: 19px; border-radius: 18px; }
  .kpi-card::after { position: absolute; right: -23px; bottom: -28px; width: 98px; height: 98px; border: 1px solid currentColor; border-radius: 50%; content: ""; opacity: 0.1; }
  .kpi-card--attention { color: var(--red); border-color: color-mix(in srgb, var(--red) 29%, var(--line)); background: color-mix(in srgb, var(--red-soft) 54%, var(--surface)); }
  .kpi-card--attention.is-clear { color: var(--green); border-color: color-mix(in srgb, var(--green) 28%, var(--line)); background: color-mix(in srgb, var(--green-soft) 46%, var(--surface)); }
  .kpi-card--armed { color: var(--teal); }
  .kpi-card--live { color: var(--ink-soft); }
  .kpi-card--next { color: var(--teal-deep); background: color-mix(in srgb, var(--teal-soft) 48%, var(--surface)); }
  .kpi-label { display: block; color: var(--ink-soft); font: 700 10px/1.2 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; }
  .kpi-value { display: block; margin: 17px 0 8px; color: var(--ink); font: 700 clamp(32px, 4vw, 46px)/0.9 var(--display); letter-spacing: -0.055em; font-variant-numeric: tabular-nums; }
  .kpi-card--next .kpi-value { font-family: var(--mono); font-size: clamp(24px, 3vw, 35px); letter-spacing: -0.07em; }
  .kpi-detail { position: relative; z-index: 1; margin: 0; color: var(--ink-soft); font-size: 12px; }
  .dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(300px, 0.75fr); gap: 18px; margin-top: 18px; }
  .panel { border-radius: 22px; }
  .panel--actions { min-height: 308px; }
  .panel--context { min-height: 308px; }
  .panel--watch { margin-top: 18px; }
  .panel-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 21px 22px 16px; border-bottom: 1px solid var(--line); }
  .panel-header .eyebrow { margin-bottom: 5px; }
  .panel-count { padding: 4px 8px; border-radius: 999px; color: var(--teal-deep); background: var(--teal-soft); font: 700 11px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
  .surface-list { padding: 10px; }
  .surface-card { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 18px; align-items: center; padding: 15px 12px; border-radius: 14px; }
  .surface-card + .surface-card { border-top: 1px solid var(--line); border-radius: 0; }
  .surface-card--action { background: color-mix(in srgb, var(--red-soft) 42%, transparent); }
  .surface-card--compact { grid-template-columns: minmax(0, 1fr) auto auto; }
  .surface-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .surface-title-row .surface-title { flex: 1 1 auto; }
  .surface-title { margin: 0; overflow: hidden; color: var(--ink); font: 700 16px/1.1 var(--display); letter-spacing: -0.018em; text-overflow: ellipsis; white-space: nowrap; }
  .surface-meta { display: flex; gap: 7px; align-items: center; overflow: hidden; margin-top: 6px; color: var(--muted); font: 11px/1.3 var(--mono); }
  .surface-meta span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .status-pill { display: inline-flex; align-items: center; width: fit-content; max-width: 100%; min-height: 23px; padding: 3px 8px; border-radius: 999px; font: 700 10px/1 var(--mono); letter-spacing: 0.035em; text-transform: uppercase; }
  .status-pill--blocked { color: var(--red); background: var(--red-soft); }
  .status-pill--working { color: var(--amber); background: var(--amber-soft); }
  .status-pill--ongoing { color: var(--green); background: var(--green-soft); }
  .status-pill--neutral { color: var(--stone); background: var(--stone-soft); }
  .surface-time { min-width: 80px; text-align: right; }
  .surface-time-label { display: block; color: var(--muted); font: 700 9px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase; }
  .countdown { display: block; margin-top: 5px; color: var(--ink); font: 700 18px/1 var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.06em; }
  .surface-controls { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .button { min-height: 34px; padding: 0 10px; border: 1px solid var(--line-strong); border-radius: 9px; color: var(--ink-soft); background: transparent; font-size: 12px; font-weight: 700; white-space: nowrap; transition: transform 150ms ease, background 150ms ease, border-color 150ms ease; }
  .button:hover { transform: translateY(-1px); border-color: var(--teal); background: var(--teal-soft); }
  .button--primary { border-color: var(--teal); color: #fff; background: var(--teal); }
  .button--primary:hover { color: #fff; background: var(--teal-deep); }
  .button--quiet { color: var(--muted); }
  .switch { display: inline-flex; align-items: center; gap: 6px; min-height: 34px; color: var(--ink-soft); font-size: 11px; font-weight: 700; white-space: nowrap; }
  .switch input { width: 15px; height: 15px; accent-color: var(--teal); }
  .empty-state { display: grid; min-height: 190px; place-items: center; padding: 28px; text-align: center; }
  .empty-state strong { display: block; color: var(--ink); font: 700 22px/1.1 var(--display); }
  .empty-state p { max-width: 280px; margin: 8px 0 0; color: var(--muted); font-size: 12px; }
  .context-body { display: grid; gap: 18px; padding: 21px 22px; }
  .context-callout { padding: 15px; border: 1px solid color-mix(in srgb, var(--teal) 20%, var(--line)); border-radius: 14px; background: color-mix(in srgb, var(--teal-soft) 42%, transparent); }
  .context-callout strong { display: block; color: var(--teal-deep); font: 700 13px/1.25 var(--sans); }
  .context-callout p { margin: 6px 0 0; color: var(--ink-soft); font-size: 12px; }
  .breakdown { display: grid; gap: 11px; }
  .breakdown-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }
  .breakdown-label { color: var(--ink-soft); font-size: 12px; }
  .breakdown-value { color: var(--ink); font: 700 14px/1 var(--mono); font-variant-numeric: tabular-nums; }
  .meter { grid-column: 1 / -1; height: 6px; overflow: hidden; border-radius: 999px; background: var(--stone-soft); }
  .meter > span { display: block; height: 100%; border-radius: inherit; transition: width 220ms ease; }
  .meter--blocked > span { background: var(--red); }
  .meter--working > span { background: var(--amber); }
  .meter--ongoing > span { background: var(--green); }
  .group-label { margin: 15px 12px 6px; color: var(--muted); font: 700 10px/1.2 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; }
  .preview { overflow: hidden; margin-top: 8px; color: var(--ink-soft); font: 12px/1.35 var(--sans); text-overflow: ellipsis; white-space: nowrap; }
  .retry-risk { color: var(--red); }
  .toast { position: fixed; z-index: 10; right: 20px; bottom: 20px; max-width: min(360px, calc(100vw - 40px)); padding: 12px 15px; border: 1px solid color-mix(in srgb, var(--red) 40%, var(--line)); border-radius: 12px; color: var(--red); background: var(--surface-strong); box-shadow: var(--shadow); font-size: 12px; }
  .toast[hidden] { display: none; }
  .tutorial[hidden] { display: none; }
  .tutorial { position: fixed; z-index: 20; inset: 0; display: block; padding: 20px; }
  .tutorial-backdrop { position: absolute; inset: 0; background: rgb(4 10 20 / 0.72); transition: background 180ms ease; }
  .tutorial-dialog { position: fixed; z-index: 1; width: min(410px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 26px; border: 1px solid color-mix(in srgb, var(--teal) 35%, var(--line)); border-radius: 22px; background: var(--surface-strong); box-shadow: 0 26px 75px rgb(0 0 0 / 0.42); }
  .tutorial-step { margin: 0 0 11px; color: var(--teal); font: 700 10px/1 var(--mono); letter-spacing: 0.11em; text-transform: uppercase; }
  .tutorial-title { margin: 0; font: 700 27px/1.08 var(--display); letter-spacing: -0.035em; }
  .tutorial-copy { min-height: 70px; margin: 12px 0 22px; color: var(--ink-soft); font-size: 14px; }
  .tutorial-example { margin: -7px 0 20px; padding: 12px; border: 1px solid color-mix(in srgb, var(--red) 45%, var(--line)); border-radius: 13px; background: color-mix(in srgb, var(--red-soft) 34%, var(--surface)); }
  .tutorial-example[hidden] { display: none; }
  .tutorial-example-label { display: block; margin-bottom: 8px; color: var(--red); font: 700 9px/1 var(--mono); letter-spacing: 0.09em; text-transform: uppercase; }
  .tutorial-agent-demo { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-strong); }
  .tutorial-agent-demo-head, .tutorial-agent-demo-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .tutorial-agent-demo-name { font-weight: 700; font-size: 12px; }
  .tutorial-agent-demo-status { padding: 4px 6px; border-radius: 999px; color: var(--red); background: var(--red-soft); font: 700 8px/1 var(--mono); letter-spacing: 0.07em; text-transform: uppercase; }
  .tutorial-agent-demo-message { color: var(--ink-soft); font-size: 11px; line-height: 1.35; }
  .tutorial-agent-demo-foot { color: var(--muted); font: 700 9px/1 var(--mono); letter-spacing: 0.07em; text-transform: uppercase; }
  .tutorial-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .tutorial-dots { display: flex; gap: 6px; }
  .tutorial-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); }
  .tutorial-dot.is-active { width: 20px; border-radius: 999px; background: var(--teal); }
  .tutorial-actions { display: flex; gap: 8px; }
  .tutorial-button { min-height: 34px; padding: 0 12px; border: 1px solid var(--line); border-radius: 9px; color: var(--ink-soft); background: transparent; font-weight: 700; font-size: 12px; }
  .tutorial-button--primary { border-color: var(--teal); color: #06231f; background: var(--teal); }
  @media (max-width: 980px) {
    .hero-grid, .dashboard-grid { grid-template-columns: 1fr; }
    .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .panel--context { min-height: auto; }
  }
  @media (max-width: 680px) {
    .page-shell { padding-top: 16px; }
    .topbar { align-items: flex-start; flex-wrap: wrap; }
    .topbar-actions { width: 100%; flex-wrap: wrap; margin-left: 0; }
    .connection { order: 2; }
    .settings { margin-left: auto; }
    .hero-copy h2 { font-size: clamp(39px, 15vw, 58px); }
    .kpi-grid { grid-template-columns: 1fr; }
    .kpi-card { min-height: 125px; }
    .surface-card, .surface-card--compact { grid-template-columns: minmax(0, 1fr) auto; gap: 12px; }
    .surface-controls { grid-column: 1 / -1; justify-content: flex-start; flex-wrap: wrap; }
    .surface-time { min-width: 65px; }
    .surface-card--compact .surface-controls { display: none; }
    .panel-header { padding: 18px 17px 14px; }
    .surface-list { padding: 7px; }
  }
  /* ---- Agent board (per-agent columns) --------------------------------- */
  .agent-insights-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 18px; }
  .panel--uptime, .panel--board { margin-top: 0; }
  @media (max-width: 980px) { .agent-insights-grid { grid-template-columns: 1fr; } }
  .agent-board {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(272px, 1fr);
    gap: 14px;
    padding: 16px;
    overflow-x: auto;
    scroll-snap-type: x proximity;
  }
  .agent-board .empty-state { grid-column: 1 / -1; }
  .agent-board::-webkit-scrollbar { height: 9px; }
  .agent-board::-webkit-scrollbar-thumb { border-radius: 999px; background: var(--line-strong); }
  .agent-column {
    display: flex;
    flex-direction: column;
    min-height: 210px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: color-mix(in srgb, var(--surface-strong) 72%, transparent);
    scroll-snap-align: start;
  }
  .agent-column--blocked { border-color: color-mix(in srgb, var(--red) 44%, var(--line)); background: color-mix(in srgb, var(--red-soft) 34%, var(--surface-strong)); }
  .agent-column--working { border-color: color-mix(in srgb, var(--amber) 42%, var(--line)); background: color-mix(in srgb, var(--amber-soft) 28%, var(--surface-strong)); }
  .agent-column--ongoing { border-color: color-mix(in srgb, var(--green) 34%, var(--line)); background: color-mix(in srgb, var(--green-soft) 22%, var(--surface-strong)); }
  .agent-column-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 13px 14px; border-bottom: 1px solid var(--line); }
  .agent-id { min-width: 0; }
  .agent-name { margin: 0 0 5px; overflow: hidden; color: var(--ink); font: 700 15px/1.15 var(--display); letter-spacing: -0.02em; text-overflow: ellipsis; white-space: nowrap; }
  .agent-activity { display: flex; flex: 1 1 auto; flex-direction: column; justify-content: flex-start; gap: 3px; padding: 12px 14px; overflow: auto; }
  .activity-line { color: var(--ink-soft); font: 11px/1.5 var(--mono); overflow-wrap: anywhere; white-space: pre-wrap; }
  .activity-line:last-child { color: var(--ink); }
  .activity-empty { color: var(--muted); font: 12px/1.4 var(--sans); font-style: italic; }
  .agent-column-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 14px; border-top: 1px solid var(--line); background: color-mix(in srgb, var(--surface) 45%, transparent); }
  .agent-eta { display: flex; flex-direction: column; }
  .agent-eta .countdown { margin-top: 2px; font-size: 16px; }
  .agent-actions { display: flex; align-items: center; gap: 8px; }
  .agent-foot-note { color: var(--muted); font: 700 10px/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase; }

  /* ---- All-sessions ledger --------------------------------------------- */
  .panel--sessions { margin-top: 18px; }
  .session-list { display: flex; flex-direction: column; }
  .session-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 12px; align-items: center; width: 100%; padding: 11px 18px; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; cursor: pointer; }
  .session-row:hover, .session-row:focus-visible { background: color-mix(in srgb, var(--teal-soft) 35%, transparent); outline: none; }
  .session-row:first-child { border-top: none; }
  .session-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--stone); }
  .session-dot--open { background: var(--green); box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 16%, transparent); }
  .session-main { min-width: 0; }
  .session-title { overflow: hidden; color: var(--ink); font: 600 13px/1.3 var(--sans); text-overflow: ellipsis; white-space: nowrap; }
  .session-sub { overflow: hidden; color: var(--muted); font: 11px/1.35 var(--mono); text-overflow: ellipsis; white-space: nowrap; }
  .session-tags { display: flex; gap: 6px; }
  .session-tag { padding: 2px 7px; border-radius: 999px; font: 700 9px/1.5 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; }
  .session-tag--open { color: var(--green); background: var(--green-soft); }
  .session-tag--limit { color: var(--amber); background: var(--amber-soft); }
  .session-tag--claude { color: #a2530a; background: #ffe6c4; }   /* orange */
  .session-tag--codex  { color: #1c4fa8; background: #dde7fc; }   /* dark blue */
  .session-tag--kimi   { color: #066d94; background: #c9efff; }   /* light sky blue */
  @media (prefers-color-scheme: dark) {
    .session-tag--claude { color: #ffc178; background: #4a3212; }
    .session-tag--codex  { color: #aac6ff; background: #182f57; }
    .session-tag--kimi   { color: #b8ecff; background: #185471; }
  }
  .session-when { color: var(--muted); font: 11px/1 var(--mono); white-space: nowrap; text-align: right; }
  .session-pager { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 14px 18px; border-top: 1px solid var(--line); }
  .session-pager:empty { display: none; }
  .session-pager .button:disabled { opacity: 0.4; cursor: default; }
  .pager-info { color: var(--muted); font: 700 11px/1 var(--mono); letter-spacing: 0.04em; min-width: 96px; text-align: center; }

  /* ---- YSYL impact ----------------------------------------------------- */
  .panel--impact { margin-top: 18px; }
  .impact-body { padding: 20px 22px 22px; }
  .impact-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  .impact-stat { padding: 13px; border: 1px solid var(--line); border-radius: 13px; background: color-mix(in srgb, var(--surface-strong) 72%, transparent); }
  .impact-stat-value { display: block; color: var(--ink); font: 700 25px/1 var(--display); }
  .impact-stat-label { display: block; margin-top: 5px; color: var(--muted); font: 700 9px/1.3 var(--mono); letter-spacing: 0.08em; text-transform: uppercase; }
  .impact-chart { display: grid; grid-template-columns: 28px repeat(14, minmax(13px, 1fr)); grid-template-rows: 17px repeat(24, 12px); gap: 3px; margin-top: 22px; overflow-x: auto; }
  .impact-day-label, .impact-hour-label { align-self: center; overflow: hidden; color: var(--muted); font: 8px/1 var(--mono); text-align: center; text-overflow: ellipsis; white-space: nowrap; }
  .impact-hour-label { text-align: right; padding-right: 3px; }
  .impact-cell { min-width: 13px; min-height: 12px; border-radius: 2px; background: var(--stone-soft); }
  .impact-note { margin: 14px 0 0; color: var(--ink-soft); font-size: 12px; }
  @media (max-width: 680px) { .impact-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } .impact-chart { gap: 2px; } }

  /* ---- Agent uptime donut --------------------------------------------- */
  .panel--uptime { margin-top: 18px; }
  .uptime-body { display: flex; align-items: center; gap: 18px; padding: 20px 22px; flex-wrap: wrap; }
  .uptime-donut { position: relative; width: 112px; height: 112px; flex: 0 0 auto; }
  .uptime-svg { width: 100%; height: 100%; }
  .uptime-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; pointer-events: none; }
  .uptime-total { color: var(--ink); font: 700 22px/1 var(--display); letter-spacing: -0.03em; }
  .uptime-total-label { margin-top: 3px; color: var(--muted); font: 700 9px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; }
  .uptime-legend { display: flex; flex: 1 1 auto; flex-direction: column; gap: 10px; min-width: 0; }
  .uptime-legend-row { display: flex; align-items: center; gap: 10px; }
  .uptime-swatch { width: 11px; height: 11px; border-radius: 3px; flex: 0 0 auto; }
  .uptime-legend-name { min-width: 54px; color: var(--ink); font: 600 13px/1.2 var(--sans); text-transform: capitalize; }
  .uptime-legend-val { color: var(--ink-soft); font: 700 12px/1 var(--mono); font-variant-numeric: tabular-nums; }
  .uptime-score { padding: 4px 22px 22px; }
  .score-stats { display: flex; gap: 12px; flex-wrap: wrap; }
  .score-stat { flex: 1 1 110px; min-width: 96px; padding: 13px 15px; border: 1px solid var(--line); border-radius: 14px; background: color-mix(in srgb, var(--surface-strong) 60%, transparent); }
  .score-stat--worked { border-color: color-mix(in srgb, var(--green) 34%, var(--line)); background: color-mix(in srgb, var(--green-soft) 42%, var(--surface-strong)); }
  .score-stat--focus { border-color: color-mix(in srgb, var(--teal) 30%, var(--line)); }
  .score-value { color: var(--ink); font: 700 27px/1 var(--display); letter-spacing: -0.04em; font-variant-numeric: tabular-nums; }
  .score-stat--worked .score-value { color: var(--green); }
  .score-stat--focus .score-value { color: var(--teal); }
  .score-label { margin-top: 6px; color: var(--muted); font: 700 10px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; }
  .score-bar { display: flex; height: 8px; margin-top: 14px; overflow: hidden; border-radius: 999px; background: var(--stone-soft); }
  .score-bar > span { transition: width 300ms ease; }
  .score-bar-active { background: var(--green); }
  .score-bar-idle { background: var(--stone); }
  @media (max-width: 680px) { .session-sub, .session-tags { display: none; } }

  /* ---- Motion ---------------------------------------------------------- */
  @keyframes ysRise {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes ysPop {
    0%   { opacity: 0; transform: translateY(8px) scale(0.965); }
    60%  { opacity: 1; }
    100% { opacity: 1; transform: translateY(0) scale(1); }
  }
  @keyframes ysValuePop {
    0%   { transform: scale(1); }
    35%  { transform: scale(1.16); color: var(--teal); }
    100% { transform: scale(1); }
  }
  @keyframes ysPulseRing {
    0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--green) 46%, transparent); }
    70%  { box-shadow: 0 0 0 8px color-mix(in srgb, var(--green) 0%, transparent); }
    100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--green) 0%, transparent); }
  }
  @keyframes ysGlow { 0%, 100% { opacity: 0.32; } 50% { opacity: 1; } }
  @keyframes ysBreathe { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
  @keyframes ysToastIn {
    from { opacity: 0; transform: translateY(14px) scale(0.98); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* Staged entrances for the static shell. */
  .hero-copy    { animation: ysRise 620ms cubic-bezier(0.22, 0.61, 0.36, 1) both; }
  .signal-panel { animation: ysRise 620ms cubic-bezier(0.22, 0.61, 0.36, 1) 90ms both; }
  .kpi-card     { animation: ysPop 560ms cubic-bezier(0.22, 0.61, 0.36, 1) both;
                  transition: transform 180ms ease, box-shadow 220ms ease; }
  .kpi-grid .kpi-card:nth-child(1) { animation-delay: 110ms; }
  .kpi-grid .kpi-card:nth-child(2) { animation-delay: 180ms; }
  .kpi-grid .kpi-card:nth-child(3) { animation-delay: 250ms; }
  .kpi-grid .kpi-card:nth-child(4) { animation-delay: 320ms; }
  .kpi-card:hover { transform: translateY(-3px); box-shadow: 0 22px 48px rgba(41, 62, 50, 0.16); }
  .panel        { animation: ysRise 640ms cubic-bezier(0.22, 0.61, 0.36, 1) 150ms both; }

  /* Rows animate in as the queue/history re-render. */
  .surface-card { animation: ysRise 400ms cubic-bezier(0.22, 0.61, 0.36, 1) both;
                  transition: background 200ms ease, transform 200ms ease; }
  .surface-list > .surface-card:nth-child(2) { animation-delay: 45ms; }
  .surface-list > .surface-card:nth-child(3) { animation-delay: 90ms; }
  .surface-list > .surface-card:nth-child(4) { animation-delay: 135ms; }
  .surface-list > .surface-card:nth-child(n+5) { animation-delay: 175ms; }
  .surface-card--compact:hover { transform: translateX(3px); background: color-mix(in srgb, var(--teal-soft) 24%, transparent); }

  .agent-column { animation: ysPop 460ms cubic-bezier(0.22, 0.61, 0.36, 1) both; transition: transform 180ms ease, box-shadow 200ms ease; }
  .agent-board > .agent-column:nth-child(2) { animation-delay: 55ms; }
  .agent-board > .agent-column:nth-child(3) { animation-delay: 110ms; }
  .agent-board > .agent-column:nth-child(4) { animation-delay: 165ms; }
  .agent-board > .agent-column:nth-child(n+5) { animation-delay: 205ms; }
  .agent-column:hover { transform: translateY(-3px); box-shadow: var(--shadow); }

  /* Live heartbeat on the connection dot; stops (turns red) when offline. */
  .connection:not(.is-offline)::before { animation: ysPulseRing 2200ms ease-out infinite; }

  /* Red inner ring breathes only while a live session is blocked. */
  .kpi-card--attention:not(.is-clear)::before {
    position: absolute;
    inset: 0;
    z-index: 0;
    content: "";
    border-radius: inherit;
    pointer-events: none;
    box-shadow: inset 0 0 0 1.5px color-mix(in srgb, var(--red) 48%, transparent);
    animation: ysGlow 2400ms ease-in-out infinite;
  }
  .status-pill--blocked { animation: ysBreathe 2000ms ease-in-out infinite; }

  /* Tactile press + snappy metric changes. */
  .button:active { transform: translateY(0) scale(0.97); }
  .switch input, .button { transition: transform 150ms ease, background 150ms ease, border-color 150ms ease; }
  .value-pop { animation: ysValuePop 460ms cubic-bezier(0.22, 0.61, 0.36, 1); }
  .toast:not([hidden]) { animation: ysToastIn 260ms cubic-bezier(0.22, 0.61, 0.36, 1); }

  /* ---- Minimalist: strip secondary copy, keep the numbers and the board -- */
  .eyebrow { display: none; }                 /* uppercase micro-labels */
  .kpi-detail { display: none; }              /* sentence under each metric */
  .hero-copy h2 { display: none; }            /* oversized hero headline */
  .signal-panel { display: none; }            /* live-mix panel = redundant with KPIs */
  .hero-grid { grid-template-columns: 1fr; align-items: center; }
  .hero-copy { padding: 0; }
  #summary { margin: 0; font-size: 13px; color: var(--muted); }
  .context-callout { display: none; }         /* prose box in coverage */
  .empty-state p { display: none; }           /* keep the one-line headline only */
  .panel-header { padding: 16px 22px 12px; }
  .kpi-card { min-height: 108px; }
  .kpi-value { margin: 6px 0 0; }
  main { padding-top: clamp(14px, 2.4vw, 26px); }
  .kpi-grid { margin-top: 16px; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; }
  }
</style>
</head>
<body data-theme="midnight">
<div class="page-shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">YS</div>
      <div>
        <p class="eyebrow">Local agent operations</p>
        <h1>YouSnoozeYouLose <span>/ recovery desk</span></h1>
      </div>
    </div>
    <div class="topbar-actions">
      <div class="connection" id="connection" role="status">Live monitor</div>
      <button id="help-button" class="help-button" type="button" aria-haspopup="dialog">Help</button>
      <div class="settings">
        <label for="history">History</label>
        <select id="history" aria-label="Session history window">
          <option value="3d">3 days</option>
          <option value="1w">1 week</option>
          <option value="3w">3 weeks</option>
        </select>
        <label for="theme">Theme</label>
        <select id="theme" aria-label="Colour theme">
          <option value="midnight">Midnight</option>
          <option value="forest">Forest</option>
          <option value="moss">Moss</option>
          <option value="ocean">Ocean</option>
          <option value="orchid">Orchid</option>
        </select>
      </div>
    </div>
  </header>
  <main>
    <section class="hero-grid" aria-labelledby="overview-title">
      <div class="hero-copy">
        <p class="eyebrow">Operational overview</p>
        <h2 id="overview-title">Keep every <em>recovery</em> on time.</h2>
        <p id="summary">Connecting to the local daemon and preparing the recovery queue.</p>
      </div>
      <aside class="signal-panel" aria-labelledby="mix-title">
        <div>
          <p class="eyebrow">Live session mix</p>
          <h3 id="mix-title">Where attention is needed</h3>
          <p id="mix-caption">Waiting for the first state snapshot.</p>
        </div>
        <div>
          <div class="mix-bar" id="mix-bar" aria-label="Live session mix"></div>
          <div class="mix-legend" id="mix-legend"></div>
        </div>
      </aside>
    </section>

    <section class="kpi-grid" aria-label="Recovery metrics">
      <article class="kpi-card kpi-card--attention" id="attention-card">
        <span class="kpi-label">Needs attention</span>
        <strong class="kpi-value" id="attention-value">--</strong>
        <p class="kpi-detail" id="attention-detail">Checking live limits</p>
      </article>
      <article class="kpi-card kpi-card--armed">
        <span class="kpi-label">Auto-resume armed</span>
        <strong class="kpi-value" id="armed-value">--</strong>
        <p class="kpi-detail" id="armed-detail">Waiting for the recovery queue</p>
      </article>
      <article class="kpi-card kpi-card--live">
        <span class="kpi-label">Live sessions</span>
        <strong class="kpi-value" id="live-value">--</strong>
        <p class="kpi-detail" id="live-detail">Surfaces currently observed</p>
      </article>
      <article class="kpi-card kpi-card--next">
        <span class="kpi-label">Next recovery</span>
        <strong class="kpi-value" id="next-value">--</strong>
        <p class="kpi-detail" id="next-detail">No active recovery window</p>
      </article>
    </section>

    <div class="agent-insights-grid">
      <section class="panel panel--uptime" aria-labelledby="uptime-title">
        <header class="panel-header">
          <div>
            <h2 class="panel-title" id="uptime-title">Hours worked</h2>
          </div>
        </header>
        <div class="uptime-body">
          <div class="uptime-donut" id="uptime-donut"></div>
          <div class="uptime-legend" id="uptime-legend"></div>
        </div>
        <div class="uptime-score" id="uptime-score"></div>
      </section>

      <section class="panel panel--board" aria-labelledby="board-title">
        <header class="panel-header">
          <div>
            <p class="eyebrow">Agent board</p>
            <h2 class="panel-title" id="board-title">What each agent is doing</h2>
          </div>
          <span class="panel-count" id="board-count">--</span>
        </header>
        <div class="agent-board" id="agent-board"></div>
      </section>
    </div>

    <section class="panel panel--sessions" aria-labelledby="sessions-title">
      <header class="panel-header">
        <div>
          <h2 class="panel-title" id="sessions-title">All sessions</h2>
        </div>
        <span class="panel-count" id="sessions-count">--</span>
      </header>
      <div class="session-list" id="session-list"></div>
      <div class="session-pager" id="session-pager"></div>
    </section>

    <section class="panel panel--impact" aria-labelledby="impact-title">
      <header class="panel-header">
        <div>
          <p class="eyebrow">Recovery record</p>
          <h2 class="panel-title" id="impact-title">YSYL interventions</h2>
        </div>
      </header>
      <div class="impact-body">
        <div class="impact-summary" id="impact-summary"></div>
        <div class="impact-chart" id="impact-chart" aria-label="Limits detected by day and hour"></div>
        <p class="impact-note" id="impact-note"></p>
      </div>
    </section>
  </main>
</div>
<div id="tutorial" class="tutorial" hidden>
  <div class="tutorial-backdrop" id="tutorial-backdrop"></div>
  <section class="tutorial-dialog" role="dialog" aria-modal="true" aria-labelledby="tutorial-title" aria-describedby="tutorial-copy">
    <p id="tutorial-step" class="tutorial-step">Step 1 of 5</p>
    <h2 id="tutorial-title" class="tutorial-title">Welcome to YSYL</h2>
    <p id="tutorial-copy" class="tutorial-copy"></p>
    <aside id="tutorial-example" class="tutorial-example" hidden>
      <span class="tutorial-example-label">When an agent runs out of usage</span>
      <div class="tutorial-agent-demo">
        <div class="tutorial-agent-demo-head"><strong class="tutorial-agent-demo-name">Claude</strong><span class="tutorial-agent-demo-status">Blocked</span></div>
        <div class="tutorial-agent-demo-message">You’ve reached your usage limit. Your latest message remains here so you know what will continue.</div>
        <div class="tutorial-agent-demo-foot"><span>Resumes in 2h 14m</span><span>Auto-resume on</span></div>
      </div>
    </aside>
    <footer class="tutorial-footer">
      <div id="tutorial-dots" class="tutorial-dots" aria-hidden="true"></div>
      <div class="tutorial-actions">
        <button id="tutorial-skip" class="tutorial-button" type="button">Skip</button>
        <button id="tutorial-next" class="tutorial-button tutorial-button--primary" type="button">Next</button>
      </div>
    </footer>
  </section>
</div>
<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
<script>
const $ = (id) => document.getElementById(id);
let previousStructure = "";
let previousSessions = "";
let refreshInFlight = false;
let allSessions = [];
let latestRows = [];
let latestGroups = null;
const pinnedSessionIds = new Set();
let sessionsPage = 0;
const SESSIONS_PER_PAGE = 10;
const REFRESH_MS = 3000;   // how often the open tab re-fetches state
let refreshTimer = null;
let countdownBase = new Map();
let countdownAt = 0;
const TUTORIAL_STORAGE_KEY = "ysyl-tutorial-seen-v1";
const tutorialSteps = [
  ["Welcome to YSYL", "This highlighted overview is the live recovery desk. YSYL watches agent terminals in cmux, notices usage limits, and can resume an armed agent when its limit clears.", "#overview-title"],
  ["Read the agent board", "This highlighted board shows the latest captured message from every watched agent. Red means blocked, teal means recently active, and green means ongoing or successfully resumed by YSYL. The example below shows a typical blocked card.", "#agent-board"],
  ["Use the recovery controls", "This highlighted board is also where blocked agents expose their retry time. Resume sends the configured action now; Auto-resume keeps that agent armed for the next retry.", "#agent-board"],
  ["Review retained work", "This highlighted list is your retained session history. Selecting a session pins it to the board and opens its supported resume command in cmux. Hours worked uses the same History window.", "#session-list"],
  ["Understand interventions", "This highlighted heatmap records when limits were caught, manual prompts were entered, and agents were resumed. Hover a cell for counts and resumed agent types.", "#impact-chart"],
];
let tutorialIndex = 0;

function plural(value, singular, pluralForm) {
  return value + " " + (value === 1 ? singular : (pluralForm || singular + "s"));
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "--";
  if (seconds <= 0) return "due";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours) return hours + "h " + minutes + "m";
  if (minutes) return minutes + "m " + remainingSeconds + "s";
  return remainingSeconds + "s";
}

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function displayName(row) {
  return row.title || row.ref || row.surface_id || "Unnamed surface";
}

function visibleStatus(row) {
  if (String(row.status || "").toLowerCase() === "resumed" && row.resumed_by_ysyl) return "resumed by YSYL";
  const tone = statusTone(row);
  if (tone === "blocked") return "blocked";
  if (tone === "working") return "working";
  if (tone === "ongoing") return "ongoing";
  return row.live ? "dismissed" : "closed";
}

function statusTone(row) {
  const status = String(row.status || "").toLowerCase();
  if (!row.live || status === "dismissed") return "neutral";
  if (isActionable(row)) return "blocked";
  if (row.is_working) return "working";
  return "ongoing";
}

function isActionable(row) {
  if (!row.live || !row.blocked) return false;
  const status = String(row.status || "").toLowerCase();
  return status === "sleeping" || status === "detected";
}

function classify(rows) {
  const live = rows.filter((row) => row.live);
  const blocked = live.filter(isActionable);
  const neutral = live.filter((row) => String(row.status || "").toLowerCase() === "dismissed");
  const working = live.filter((row) => !isActionable(row) && !neutral.includes(row) && row.is_working);
  const ongoing = live.filter((row) => !isActionable(row) && !neutral.includes(row) && !row.is_working);
  const armed = blocked.filter((row) => row.armed);
  const history = rows.filter((row) => !row.live);
  const candidates = blocked
    .filter((row) => row.seconds_until_reset !== null && row.seconds_until_reset !== undefined)
    .sort((left, right) => left.seconds_until_reset - right.seconds_until_reset);
  return { live, blocked, working, ongoing, neutral, armed, history, next: candidates[0] || null };
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => { toast.hidden = true; }, 4200);
}

function setMetric(id, value) {
  const node = $(id);
  const next = String(value);
  if (node.textContent === next) return;
  node.textContent = next;
  if (id === "next-value") return;  // ticks every second — pulsing it would be noise
  node.classList.remove("value-pop");
  void node.offsetWidth;            // force reflow so the animation restarts
  node.classList.add("value-pop");
}

function updateOverview(rows, now) {
  const groups = classify(rows);
  const closed = groups.history.length;
  $("summary").textContent =
    plural(groups.live.length, "live session") + " under observation" +
    (closed ? " and " + plural(closed, "closed surface") + " in history" : "") +
    ". Updated " + new Date(now).toLocaleTimeString() + ".";
  setMetric("attention-value", groups.blocked.length);
  $("attention-detail").textContent = groups.blocked.length
    ? plural(groups.blocked.length, "surface") + " currently blocked"
    : "No live surfaces are blocked";
  $("attention-card").classList.toggle("is-clear", groups.blocked.length === 0);
  setMetric("armed-value", groups.armed.length);
  $("armed-detail").textContent = groups.armed.length
    ? plural(groups.armed.length, "recovery") + " will resume automatically"
    : "No active recoveries are armed";
  setMetric("live-value", groups.live.length);
  $("live-detail").textContent = groups.working.length
    ? plural(groups.working.length, "session") + " showing recent output"
    : plural(groups.ongoing.length, "session") + " ongoing";
  setMetric("next-value", groups.next ? formatDuration(groups.next.seconds_until_reset) : "--");
  $("next-detail").textContent = groups.next
    ? "From " + displayName(groups.next)
    : "No active recovery window";
  return groups;
}

function renderMix(groups) {
  const total = Math.max(groups.live.length, 1);
  const segments = [
    { key: "blocked", label: "blocked", count: groups.blocked.length },
    { key: "working", label: "working", count: groups.working.length },
    { key: "ongoing", label: "ongoing", count: groups.ongoing.length },
  ];
  const bar = $("mix-bar");
  const legend = $("mix-legend");
  bar.replaceChildren();
  legend.replaceChildren();
  for (const segment of segments) {
    const fill = create("span", "mix-segment mix-segment--" + segment.key);
    fill.style.flex = String(segment.count || 0.01) + " 0 0";
    fill.setAttribute("aria-label", plural(segment.count, segment.label));
    bar.append(fill);
    const item = create("div", "legend-item");
    item.append(create("span", "legend-value", segment.count));
    item.append(create("span", "legend-label", segment.label));
    legend.append(item);
  }
  $("mix-caption").textContent = groups.live.length
    ? plural(groups.live.length, "live session") + " distributed by current recovery status."
    : "No live sessions are currently reported.";
}

function makeStatusPill(row) {
  return create("span", "status-pill status-pill--" + statusTone(row), visibleStatus(row));
}

function makeSurfaceMeta(row) {
  const meta = create("div", "surface-meta");
  meta.append(create("span", "", row.agent_kind || "unknown agent"));
  if (row.ref) meta.append(create("span", "", row.ref));
  return meta;
}

function makeTime(row) {
  const time = create("div", "surface-time");
  time.append(create("span", "surface-time-label", "Resumes in"));
  const value = create("strong", "countdown js-countdown", formatDuration(row.seconds_until_reset));
  value.dataset.surfaceId = row.surface_id;
  time.append(value);
  return time;
}

function makeButton(label, action, surfaceId, className) {
  const button = create("button", "button " + className, label);
  button.type = "button";
  button.dataset.action = action;
  button.dataset.surfaceId = surfaceId;
  button.setAttribute("aria-label", label + " for " + surfaceId);
  return button;
}

function makeArmControl(row) {
  const label = create("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = row.armed !== false;
  input.dataset.action = "arm";
  input.dataset.surfaceId = row.surface_id;
  input.setAttribute("aria-label", "Enable automatic resume for " + displayName(row));
  label.append(input, document.createTextNode("Auto-resume"));
  return label;
}

function renderActionQueue(rows) {
  const queue = $("action-queue");
  queue.replaceChildren();
  if (!rows.length) {
    const empty = create("div", "empty-state");
    empty.append(create("strong", "", "The recovery queue is clear."));
    empty.append(create("p", "", "YSYL is still watching for new agent limits and will surface them here."));
    queue.append(empty);
    return;
  }
  for (const row of rows) {
    const card = create("article", "surface-card surface-card--action");
    const summary = create("div", "surface-summary");
    summary.append(create("h3", "surface-title", displayName(row)), makeSurfaceMeta(row));
    if (row.preview) summary.append(create("p", "preview", row.preview));
    const controls = create("div", "surface-controls");
    controls.append(makeButton("Resume now", "resume", row.surface_id, "button--primary"));
    controls.append(makeArmControl(row));
    controls.append(makeButton("Dismiss", "dismiss", row.surface_id, "button--quiet"));
    card.append(summary, makeTime(row), controls);
    queue.append(card);
  }
}

function makeCompactCard(row) {
  const card = create("article", "surface-card surface-card--compact");
  const summary = create("div", "surface-summary");
  const titleRow = create("div", "surface-title-row");
  titleRow.append(create("h3", "surface-title", displayName(row)), makeStatusPill(row));
  summary.append(titleRow, makeSurfaceMeta(row));
  const retries = Number(row.retry_count || 0);
  if (row.preview) summary.append(create("p", "preview", row.preview));
  const details = create("div", "surface-time");
  details.append(create("span", "surface-time-label", retries ? "Retry count" : "Status"));
  details.append(create("strong", retries >= 3 ? "countdown retry-risk" : "countdown", retries || visibleStatus(row)));
  card.append(summary, details);
  return card;
}

function renderWatchList(rows, groups) {
  const list = $("watch-list");
  list.replaceChildren();
  const watching = rows.filter((row) => row.live && !isActionable(row));
  const history = rows.filter((row) => !row.live);
  if (!watching.length && !history.length) {
    const empty = create("div", "empty-state");
    empty.append(create("strong", "", "No surfaces have been observed yet."));
    empty.append(create("p", "", "Open an agent session and YSYL will add its current recovery state here."));
    list.append(empty);
    return;
  }
  const sections = [
    { label: "Watching now", rows: watching },
    { label: "Recent history", rows: history },
  ];
  for (const section of sections) {
    if (!section.rows.length) continue;
    list.append(create("p", "group-label", section.label));
    for (const row of section.rows) list.append(makeCompactCard(row));
  }
}

function renderCoverage(groups) {
  const total = Math.max(groups.live.length, 1);
  const rows = [
    { label: "Blocked", value: groups.blocked.length, tone: "blocked" },
    { label: "Working now", value: groups.working.length, tone: "working" },
    { label: "Ongoing", value: groups.ongoing.length, tone: "ongoing" },
  ];
  const breakdown = $("coverage-breakdown");
  breakdown.replaceChildren();
  for (const row of rows) {
    const item = create("div", "breakdown-row");
    item.append(create("span", "breakdown-label", row.label));
    item.append(create("strong", "breakdown-value", row.value));
    const meter = create("div", "meter meter--" + row.tone);
    const fill = create("span");
    fill.style.width = (row.value / total * 100) + "%";
    meter.append(fill);
    item.append(meter);
    breakdown.append(item);
  }
  if (!groups.live.length) {
    $("coverage-headline").textContent = "No live sessions are visible yet";
    $("coverage-detail").textContent = "YSYL will populate this view as cmux agent surfaces are discovered.";
  } else if (groups.blocked.length) {
    $("coverage-headline").textContent = plural(groups.blocked.length, "live surface") + " is blocked";
    $("coverage-detail").textContent = groups.armed.length
      ? plural(groups.armed.length, "recovery") + " remains armed for automatic resume."
      : "No active recovery is armed yet.";
  } else {
    $("coverage-headline").textContent = plural(groups.working.length, "session") + " working now";
    $("coverage-detail").textContent = plural(groups.ongoing.length, "session") + " ongoing without recent output changes.";
  }
}

function updateCountdowns(rows) {
  // Capture the server's countdown values; a local 1s ticker decrements them
  // between the (slower) network refreshes so timers still count down smoothly.
  countdownBase = new Map(
    rows
      .filter((row) => row.seconds_until_reset !== null && row.seconds_until_reset !== undefined)
      .map((row) => [String(row.surface_id), row.seconds_until_reset])
  );
  countdownAt = Date.now();
  tickCountdowns();
}

function tickCountdowns() {
  const elapsed = Math.floor((Date.now() - countdownAt) / 1000);
  document.querySelectorAll(".js-countdown").forEach((node) => {
    const base = countdownBase.get(node.dataset.surfaceId);
    node.textContent = formatDuration(base === undefined ? undefined : base - elapsed);
  });
}

function structureSignature(rows) {
  return JSON.stringify(rows.map((row) => [
    row.surface_id, row.session_id, row.title, row.ref, row.agent_kind, row.status, row.armed,
    row.retry_count, row.preview, row.last_message, row.blocked, row.resumed_by_ysyl, row.is_working, row.live,
  ]));
}

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const key in attrs) el.setAttribute(key, attrs[key]);
  return el;
}

function formatHours(seconds) {
  const hours = seconds / 3600;
  if (hours < 1) return Math.max(0, Math.round(seconds / 60)) + "m";
  if (hours < 10) return hours.toFixed(1) + "h";
  return Math.round(hours) + "h";
}

// Matches the model tag colors; validated for CVD + normal-vision separation.
const UPTIME_COLORS = { claude: "#a2530a", codex: "#1c4fa8", kimi: "#159fbf" };

function renderUptime(sessions) {
  const wrap = $("uptime-donut");
  const legend = $("uptime-legend");
  if (!wrap || !legend) return;
  wrap.replaceChildren();
  legend.replaceChildren();

  const order = ["claude", "codex", "kimi"];
  const totals = { claude: 0, codex: 0, kimi: 0 };
  for (const s of sessions) {
    if (!(s.kind in totals)) continue;
    if (typeof s.active_seconds === "number") totals[s.kind] += s.active_seconds;
  }
  const total = order.reduce((sum, k) => sum + totals[k], 0);

  const svg = svgEl("svg", { viewBox: "0 0 42 42", class: "uptime-svg", role: "img",
    "aria-label": "Agent uptime by model" });
  svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r: 15.9155, fill: "none",
    stroke: "var(--stone-soft)", "stroke-width": 5 }));
  let acc = 0;  // circumference is 100, so percentages map directly to arc length
  for (const k of order) {
    const pct = total > 0 ? (totals[k] / total) * 100 : 0;
    if (pct > 0) {
      const seg = Math.max(0.5, pct - 1.4);  // leave a 1.4-unit gap between slices
      svg.appendChild(svgEl("circle", { cx: 21, cy: 21, r: 15.9155, fill: "none",
        stroke: UPTIME_COLORS[k], "stroke-width": 5,
        "stroke-dasharray": seg + " " + (100 - seg), "stroke-dashoffset": 25 - acc }));
    }
    acc += pct;
  }
  wrap.appendChild(svg);

  const center = create("div", "uptime-center");
  center.append(create("div", "uptime-total", total > 0 ? formatHours(total) : "0h"));
  center.append(create("div", "uptime-total-label", "total"));
  wrap.appendChild(center);

  for (const k of order) {
    const row = create("div", "uptime-legend-row");
    const swatch = create("span", "uptime-swatch");
    swatch.style.background = UPTIME_COLORS[k];
    const rawPct = total > 0 ? (totals[k] / total) * 100 : 0;
    const pctText = total > 0 ? "  ·  " + (rawPct > 0 && rawPct < 1 ? "<1%" : Math.round(rawPct) + "%") : "";
    row.append(swatch, create("span", "uptime-legend-name", k),
      create("span", "uptime-legend-val", formatHours(totals[k]) + pctText));
    legend.append(row);
  }

  // Scoreboard: hours worked vs downtime, and a focus score (work / time present).
  const score = $("uptime-score");
  if (score) {
    score.replaceChildren();
    const idle = sessions.reduce((n, s) => n + (typeof s.idle_seconds === "number" ? s.idle_seconds : 0), 0);
    const present = total + idle;
    const focus = present > 0 ? Math.round((total / present) * 100) : 0;
    const stats = create("div", "score-stats");
    stats.append(makeStat("Worked", total > 0 ? formatHours(total) : "0h", "worked"));
    stats.append(makeStat("Downtime", formatHours(idle), "downtime"));
    stats.append(makeStat("Focus", present > 0 ? focus + "%" : "—", "focus"));
    score.append(stats);
    const bar = create("div", "score-bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", "Worked " + formatHours(total) + ", downtime " + formatHours(idle));
    const activeSpan = create("span", "score-bar-active");
    activeSpan.style.width = (present > 0 ? (total / present) * 100 : 0) + "%";
    const idleSpan = create("span", "score-bar-idle");
    idleSpan.style.width = (present > 0 ? (idle / present) * 100 : 0) + "%";
    bar.append(activeSpan, idleSpan);
    score.append(bar);
  }
}

function makeStat(label, value, tone) {
  const el = create("div", "score-stat score-stat--" + tone);
  el.append(create("div", "score-value", value));
  el.append(create("div", "score-label", label));
  return el;
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "";
  let s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60); if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}

function shortCwd(cwd) {
  if (!cwd) return "";
  const parts = String(cwd).split("/").filter(Boolean);
  return parts[parts.length - 1] || String(cwd);
}

function makeSessionRow(s) {
  const row = create("button", "session-row");
  row.type = "button";
  row.dataset.action = "open-session";
  row.dataset.sessionId = s.id;
  row.setAttribute("aria-label", "Open " + (s.title || s.id) + " in the agent board and cmux");
  row.append(create("span", "session-dot" + (s.open ? " session-dot--open" : "")));
  const main = create("div", "session-main");
  main.append(create("div", "session-title", s.title || s.id));
  const sub = [s.kind || "agent"];
  if (s.ref) sub.push(s.ref);
  if (shortCwd(s.cwd)) sub.push(shortCwd(s.cwd));
  main.append(create("div", "session-sub", sub.join("  ·  ")));
  const tags = create("div", "session-tags");
  const kind = String(s.kind || "").toLowerCase();
  if (["claude", "codex", "kimi"].includes(kind)) {
    tags.append(create("span", "session-tag session-tag--" + kind, kind));
  }
  if (s.open) tags.append(create("span", "session-tag session-tag--open", "open"));
  if (s.blocked) tags.append(create("span", "session-tag session-tag--limit", "limit"));
  row.append(main, tags);
  row.append(create("div", "session-when", s.open ? "active now" : timeAgo(s.last_active)));
  return row;
}

function renderSessions(sessions) {
  allSessions = sessions;
  renderUptime(sessions);
  const list = $("session-list");
  const pager = $("session-pager");
  list.replaceChildren();
  pager.replaceChildren();
  $("sessions-count").textContent = plural(sessions.length, "session");
  if (!sessions.length) {
    const empty = create("div", "empty-state");
    empty.append(create("strong", "", "No sessions recorded yet."));
    list.append(empty);
    return;
  }
  const pageCount = Math.max(1, Math.ceil(sessions.length / SESSIONS_PER_PAGE));
  sessionsPage = Math.min(Math.max(sessionsPage, 0), pageCount - 1);
  const start = sessionsPage * SESSIONS_PER_PAGE;
  for (const s of sessions.slice(start, start + SESSIONS_PER_PAGE)) list.append(makeSessionRow(s));

  if (pageCount > 1) {
    const prev = create("button", "button", "‹ Prev");
    prev.type = "button";
    prev.disabled = sessionsPage === 0;
    prev.addEventListener("click", () => { sessionsPage -= 1; renderSessions(allSessions); });
    const info = create("span", "pager-info", "Page " + (sessionsPage + 1) + " of " + pageCount);
    const next = create("button", "button", "Next ›");
    next.type = "button";
    next.disabled = sessionsPage >= pageCount - 1;
    next.addEventListener("click", () => { sessionsPage += 1; renderSessions(allSessions); });
    pager.append(prev, info, next);
  }
}

function renderImpact(impact) {
  const data = impact || {};
  const summary = $("impact-summary");
  const chart = $("impact-chart");
  if (!summary || !chart) return;
  summary.replaceChildren();
  chart.replaceChildren();
  const stats = [
    [data.detected || 0, "limits caught"],
    [data.recovered || 0, "recoveries cleared"],
    [data.prompts || 0, "manual prompts"],
    [data.waiting || 0, "currently waiting"],
  ];
  for (const [value, label] of stats) {
    const stat = create("div", "impact-stat");
    stat.append(create("strong", "impact-stat-value", value), create("span", "impact-stat-label", label));
    summary.append(stat);
  }
  const heatmap = Array.isArray(data.heatmap) ? data.heatmap : [];
  const max = Math.max(1, ...heatmap.flatMap((day) => (day.hours || []).map((value) => {
    const resumes = Object.values(value.resumes || {}).reduce((sum, count) => sum + Number(count || 0), 0);
    return Math.max(Number(value.interventions || 0), Number(value.prompts || 0), resumes);
  })));
  chart.append(create("span", "impact-hour-label", ""));
  for (const day of heatmap) {
    const parsed = new Date((day.date || "") + "T00:00:00");
    chart.append(create("span", "impact-day-label", isNaN(parsed) ? "" : (parsed.getMonth() + 1) + "/" + parsed.getDate()));
  }
  for (let hour = 0; hour < 24; hour += 1) {
    chart.append(create("span", "impact-hour-label", hour % 3 === 0 ? String(hour).padStart(2, "0") : ""));
    for (const day of heatmap) {
      const value = (day.hours || [])[hour] || {};
      const interventions = Number(value.interventions || 0);
      const prompts = Number(value.prompts || 0);
      const resumes = value.resumes || {};
      const resumedAgents = ["claude", "codex", "kimi"]
        .filter((kind) => resumes[kind])
        .map((kind) => kind + " " + resumes[kind]);
      const cell = create("span", "impact-cell");
      const label = (day.date || "") + " " + String(hour).padStart(2, "0") + ":00";
      cell.title = label + "\\nYSYL interventions: " + interventions
        + "\\nManual prompts: " + prompts
        + "\\nYSYL resumes: " + (resumedAgents.length ? resumedAgents.join(", ") : "none");
      if (interventions && prompts) {
        const teal = 0.2 + 0.8 * interventions / max;
        const violet = 0.2 + 0.8 * prompts / max;
        cell.style.background = "linear-gradient(135deg, rgb(0 196 180 / " + teal + ") 0 50%, rgb(112 61 170 / " + violet + ") 50% 100%)";
      } else if (interventions) {
        cell.style.background = "rgb(0 196 180 / " + (0.35 + 0.65 * interventions / max) + ")";
      } else if (prompts) {
        cell.style.background = "rgb(112 61 170 / " + (0.2 + 0.8 * prompts / max) + ")";
      } else if (resumedAgents.length) {
        cell.style.background = "rgb(27 79 168 / " + (0.2 + 0.8 * resumedAgents.reduce((sum, item) => sum + Number(item.split(" ")[1]), 0) / max) + ")";
      }
      chart.append(cell);
    }
  }
  $("impact-note").textContent = (data.detected || 0)
    ? "Bright turquoise = YSYL intervention, violet = a manual prompt, blue = YSYL resume; split cells contain both prompt and intervention activity. Hover for per-agent resumes."
    : "YSYL will add intervention, prompt, and resume activity here as it is recorded.";
}

function activityLines(row) {
  const text = row.last_message;
  if (!text) return [];
  return [String(text).trim()];
}

function makeAgentColumn(row) {
  const column = create("article", "agent-column agent-column--" + statusTone(row));
  const head = create("div", "agent-column-head");
  const identity = create("div", "agent-id");
  identity.append(create("h3", "agent-name", displayName(row)), makeSurfaceMeta(row));
  head.append(identity, makeStatusPill(row));
  column.append(head);

  const activity = create("div", "agent-activity");
  const lines = activityLines(row);
  if (lines.length) {
    for (const line of lines) activity.append(create("div", "activity-line", line));
  } else {
    activity.append(create("div", "activity-empty", row.live ? "No message captured yet" : "Session closed"));
  }
  column.append(activity);

  const foot = create("div", "agent-column-foot");
  if (isActionable(row)) {
    const eta = create("div", "agent-eta");
    eta.append(create("span", "surface-time-label", "Resumes in"));
    const countdown = create("strong", "countdown js-countdown", formatDuration(row.seconds_until_reset));
    countdown.dataset.surfaceId = row.surface_id;
    eta.append(countdown);
    const actions = create("div", "agent-actions");
    actions.append(makeButton("Resume", "resume", row.surface_id, "button--primary"));
    actions.append(makeArmControl(row));
    foot.append(eta, actions);
  } else {
    const retries = Number(row.retry_count || 0);
    foot.append(create("span", "agent-foot-note", row.is_pinned ? "Opened from history" : (!row.live ? "History" : (retries ? plural(retries, "retry", "retries") : visibleStatus(row)))));
    if (row.live && !row.is_pinned && row.armed) foot.append(create("span", "agent-foot-note", "Auto-resume on"));
  }
  column.append(foot);
  return column;
}

function renderAgentBoard(rows, groups) {
  const board = $("agent-board");
  board.replaceChildren();
  const agents = rows.filter((row) => row.live);
  const liveSessionIds = new Set(agents.map((row) => String(row.session_id || row.surface_id)));
  const pinned = allSessions
    .filter((session) => pinnedSessionIds.has(String(session.id)) && !liveSessionIds.has(String(session.id)))
    .map((session) => ({
      surface_id: "history:" + session.id,
      session_id: session.id,
      title: session.title,
      ref: session.ref,
      agent_kind: session.kind,
      last_message: session.last_message || session.title,
      status: "healthy",
      armed: true,
      is_working: false,
      blocked: false,
      live: true,
      is_pinned: true,
    }));
  $("board-count").textContent = plural(agents.length + pinned.length, "agent");
  if (!agents.length && !pinned.length) {
    const empty = create("div", "empty-state");
    empty.append(create("strong", "", "No agents are being watched."));
    empty.append(create("p", "", "Open a Claude or Kimi session in cmux and its live activity will appear here."));
    board.append(empty);
    return;
  }
  // Blocked sessions lead, followed by working and then ongoing sessions.
  const ordered = [...groups.blocked, ...groups.working, ...groups.ongoing, ...groups.neutral, ...pinned];
  for (const row of ordered) board.append(makeAgentColumn(row));
}

function renderDashboard(rows, groups) {
  renderMix(groups);
  renderAgentBoard(rows, groups);
}

async function postAction(url, payload) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "The daemon did not accept that action.");
    await refresh(true);
  } catch (error) {
    showToast(error.message || "The action could not be completed.");
  }
}

async function saveSettings(updates) {
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "Could not save history preference.");
    if (data.settings.history_window) $("history").value = data.settings.history_window;
    if (data.settings.color_theme) applyTheme(data.settings.color_theme);
    if ("history_window" in updates) refresh(true);
  } catch (error) {
    showToast(error.message || "Could not save history preference.");
  }
}

async function refresh(forceRender) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("Dashboard request failed");
    const data = await response.json();
    const rows = Array.isArray(data.surfaces) ? data.surfaces : [];
    const groups = updateOverview(rows, data.now);
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    latestRows = rows;
    latestGroups = groups;
    allSessions = sessions;
    const signature = structureSignature(rows);
    if (forceRender || signature !== previousStructure) {
      previousStructure = signature;
      renderDashboard(rows, groups);
    }
    updateCountdowns(rows);
    renderImpact(data.impact);
    const sessionSig = JSON.stringify(sessions.map((s) => [s.id, s.title, s.open, s.blocked, s.last_active]));
    if (forceRender || sessionSig !== previousSessions) {
      previousSessions = sessionSig;
      renderSessions(sessions);
    }
    $("connection").textContent = "Live monitor";
    $("connection").classList.remove("is-offline");
  } catch (error) {
    $("connection").textContent = "Daemon unreachable";
    $("connection").classList.add("is-offline");
    $("summary").textContent = "The dashboard cannot reach the local daemon. It will retry automatically.";
  } finally {
    refreshInFlight = false;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const surfaceId = button.dataset.surfaceId;
  if (button.dataset.action === "resume") postAction("/api/resume_now", { surface_id: surfaceId });
  if (button.dataset.action === "dismiss") postAction("/api/dismiss", { surface_id: surfaceId });
  if (button.dataset.action === "open-session") {
    pinnedSessionIds.add(String(button.dataset.sessionId));
    if (latestGroups) renderAgentBoard(latestRows, latestGroups);
    postAction("/api/open_session", { session_id: button.dataset.sessionId });
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "history") saveSettings({ history_window: target.value });
  if (target.id === "theme") {
    applyTheme(target.value);
    saveSettings({ color_theme: target.value });
  }
  if (target.matches("input[data-action='arm']")) {
    postAction("/api/arm", { surface_id: target.dataset.surfaceId, armed: target.checked });
  }
});

$("help-button").addEventListener("click", openTutorial);
$("tutorial-backdrop").addEventListener("click", closeTutorial);
$("tutorial-skip").addEventListener("click", closeTutorial);
$("tutorial-next").addEventListener("click", () => {
  if (tutorialIndex === tutorialSteps.length - 1) closeTutorial();
  else { tutorialIndex += 1; renderTutorial(); }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("tutorial").hidden) closeTutorial();
});
window.addEventListener("resize", updateTutorialSpotlight);

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const settings = await response.json();
    if (settings.history_window) $("history").value = settings.history_window;
    if (settings.color_theme) applyTheme(settings.color_theme);
  } catch (_) {
    // State polling remains useful even if the optional settings endpoint is unavailable.
  }
}

function applyTheme(theme) {
  const requested = theme || "midnight";
  const selected = ["midnight", "forest", "moss", "ocean", "orchid"].includes(requested) ? requested : "midnight";
  $("theme").value = selected;
  document.body.dataset.theme = selected;
}

function positionTutorialDialog(rect) {
  const dialog = document.querySelector(".tutorial-dialog");
  const margin = 16;
  const gap = 26;
  const width = dialog.offsetWidth;
  const height = dialog.offsetHeight;
  let left = rect.right + gap;
  if (left + width > window.innerWidth - margin) left = rect.left - width - gap;
  if (left < margin || window.innerWidth < 860) left = margin;
  let top = rect.top + rect.height / 2 - height / 2;
  if (window.innerWidth < 860) top = window.innerHeight - height - margin;
  top = Math.max(margin, Math.min(window.innerHeight - height - margin, top));
  dialog.style.left = left + "px";
  dialog.style.top = top + "px";
}

function updateTutorialSpotlight() {
  if ($("tutorial").hidden) return;
  const target = document.querySelector(tutorialSteps[tutorialIndex][2]);
  if (!target) return;
  const rect = target.getBoundingClientRect();
  const x = Math.max(0, Math.min(window.innerWidth, rect.left + rect.width / 2));
  const y = Math.max(0, Math.min(window.innerHeight, rect.top + rect.height / 2));
  const radiusX = Math.max(105, rect.width / 2 + 24);
  const radiusY = Math.max(66, rect.height / 2 + 24);
  $("tutorial-backdrop").style.background = "radial-gradient(ellipse " + radiusX + "px " + radiusY + "px at " + x + "px " + y + "px, rgb(4 10 20 / 0) 0 72%, rgb(4 10 20 / 0.72) 100%)";
  positionTutorialDialog(rect);
}

function renderTutorial() {
  const [title, copy, selector] = tutorialSteps[tutorialIndex];
  $("tutorial-step").textContent = "Step " + (tutorialIndex + 1) + " of " + tutorialSteps.length;
  $("tutorial-title").textContent = title;
  $("tutorial-copy").textContent = copy;
  $("tutorial-example").hidden = tutorialIndex !== 1;
  const dots = $("tutorial-dots");
  dots.replaceChildren();
  tutorialSteps.forEach((_, index) => dots.append(create("span", "tutorial-dot" + (index === tutorialIndex ? " is-active" : ""))));
  $("tutorial-next").textContent = tutorialIndex === tutorialSteps.length - 1 ? "Finish" : "Next";
  const target = document.querySelector(selector);
  if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
  window.setTimeout(updateTutorialSpotlight, 220);
}

function openTutorial() {
  tutorialIndex = 0;
  $("tutorial").hidden = false;
  renderTutorial();
  $("tutorial-next").focus();
}

function closeTutorial() {
  $("tutorial").hidden = true;
  try { localStorage.setItem(TUTORIAL_STORAGE_KEY, "1"); } catch (_) {}
  $("help-button").focus();
}

function startRefresh() {
  if (!refreshTimer) refreshTimer = window.setInterval(refresh, REFRESH_MS);
}
function stopRefresh() {
  if (refreshTimer) { window.clearInterval(refreshTimer); refreshTimer = null; }
}
// Only poll while the tab is visible; refresh immediately on return.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopRefresh();
  } else {
    refresh();
    startRefresh();
  }
});

window.setInterval(tickCountdowns, 1000);   // smooth local countdown between fetches
refresh(true);
loadSettings();
if (!document.hidden) startRefresh();
try {
  if (!localStorage.getItem(TUTORIAL_STORAGE_KEY)) window.setTimeout(openTutorial, 300);
} catch (_) {}
</script>
</body>
</html>
"""
