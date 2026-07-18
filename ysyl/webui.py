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
        return {"now": now.isoformat(), "surfaces": rows}

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
            allowed = {"history_window"}
            updates = {k: v for k, v in data.items() if k in allowed}
            try:
                self.daemon.save_ui_settings(updates)
            except ValueError as exc:
                return 400, "application/json", json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            return 200, "application/json", json.dumps({"ok": True, "settings": self.daemon.get_ui_settings()}).encode("utf-8")

        if method == "POST" and path in ("/api/arm", "/api/dismiss", "/api/resume_now"):
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return 400, "application/json", b'{"ok": false, "error": "bad json"}'
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
  .mix-segment--attention { background: var(--amber); }
  .mix-segment--ready { background: var(--green); }
  .mix-segment--quiet { background: var(--stone); }
  .mix-legend { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .legend-item { min-width: 0; }
  .legend-value { display: block; color: var(--ink); font: 700 18px/1.1 var(--mono); font-variant-numeric: tabular-nums; }
  .legend-label { display: block; overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-top: 28px; }
  .kpi-card { position: relative; min-height: 160px; overflow: hidden; padding: 19px; border-radius: 18px; }
  .kpi-card::after { position: absolute; right: -23px; bottom: -28px; width: 98px; height: 98px; border: 1px solid currentColor; border-radius: 50%; content: ""; opacity: 0.1; }
  .kpi-card--attention { color: var(--amber); border-color: color-mix(in srgb, var(--amber) 29%, var(--line)); background: color-mix(in srgb, var(--amber-soft) 54%, var(--surface)); }
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
  .surface-card--action { background: color-mix(in srgb, var(--amber-soft) 32%, transparent); }
  .surface-card--compact { grid-template-columns: minmax(0, 1fr) auto auto; }
  .surface-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .surface-title-row .surface-title { flex: 1 1 auto; }
  .surface-title { margin: 0; overflow: hidden; color: var(--ink); font: 700 16px/1.1 var(--display); letter-spacing: -0.018em; text-overflow: ellipsis; white-space: nowrap; }
  .surface-meta { display: flex; gap: 7px; align-items: center; overflow: hidden; margin-top: 6px; color: var(--muted); font: 11px/1.3 var(--mono); }
  .surface-meta span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .status-pill { display: inline-flex; align-items: center; width: fit-content; max-width: 100%; min-height: 23px; padding: 3px 8px; border-radius: 999px; font: 700 10px/1 var(--mono); letter-spacing: 0.035em; text-transform: uppercase; }
  .status-pill--attention { color: var(--amber); background: var(--amber-soft); }
  .status-pill--good { color: var(--green); background: var(--green-soft); }
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
  .meter--attention > span { background: var(--amber); }
  .meter--ready > span { background: var(--green); }
  .meter--quiet > span { background: var(--stone); }
  .group-label { margin: 15px 12px 6px; color: var(--muted); font: 700 10px/1.2 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; }
  .preview { overflow: hidden; margin-top: 8px; color: var(--ink-soft); font: 12px/1.35 var(--sans); text-overflow: ellipsis; white-space: nowrap; }
  .retry-risk { color: var(--red); }
  .toast { position: fixed; z-index: 10; right: 20px; bottom: 20px; max-width: min(360px, calc(100vw - 40px)); padding: 12px 15px; border: 1px solid color-mix(in srgb, var(--red) 40%, var(--line)); border-radius: 12px; color: var(--red); background: var(--surface-strong); box-shadow: var(--shadow); font-size: 12px; }
  .toast[hidden] { display: none; }
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

  /* Live heartbeat on the connection dot; stops (turns red) when offline. */
  .connection:not(.is-offline)::before { animation: ysPulseRing 2200ms ease-out infinite; }

  /* Amber inner ring breathes only while something needs attention. */
  .kpi-card--attention:not(.is-clear)::before {
    position: absolute;
    inset: 0;
    z-index: 0;
    content: "";
    border-radius: inherit;
    pointer-events: none;
    box-shadow: inset 0 0 0 1.5px color-mix(in srgb, var(--amber) 48%, transparent);
    animation: ysGlow 2400ms ease-in-out infinite;
  }
  .status-pill--attention { animation: ysBreathe 2000ms ease-in-out infinite; }

  /* Tactile press + snappy metric changes. */
  .button:active { transform: translateY(0) scale(0.97); }
  .switch input, .button { transition: transform 150ms ease, background 150ms ease, border-color 150ms ease; }
  .value-pop { animation: ysValuePop 460ms cubic-bezier(0.22, 0.61, 0.36, 1); }
  .toast:not([hidden]) { animation: ysToastIn 260ms cubic-bezier(0.22, 0.61, 0.36, 1); }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; }
  }
</style>
</head>
<body>
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
      <div class="settings">
        <label for="history">History</label>
        <select id="history" aria-label="Session history window">
          <option value="3d">3 days</option>
          <option value="1w">1 week</option>
          <option value="3w">3 weeks</option>
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

    <section class="dashboard-grid" aria-label="Recovery workspace">
      <section class="panel panel--actions" aria-labelledby="queue-title">
        <header class="panel-header">
          <div>
            <p class="eyebrow">Priority queue</p>
            <h2 class="panel-title" id="queue-title">Needs attention</h2>
          </div>
          <span class="panel-count" id="queue-count">--</span>
        </header>
        <div class="surface-list" id="action-queue"></div>
      </section>

      <aside class="panel panel--context" aria-labelledby="coverage-title">
        <header class="panel-header">
          <div>
            <p class="eyebrow">Recovery coverage</p>
            <h2 class="panel-title" id="coverage-title">Session posture</h2>
          </div>
        </header>
        <div class="context-body">
          <div class="context-callout">
            <strong id="coverage-headline">Establishing a live view</strong>
            <p id="coverage-detail">The dashboard updates every second while the daemon is available.</p>
          </div>
          <div class="breakdown" id="coverage-breakdown"></div>
        </div>
      </aside>
    </section>

    <section class="panel panel--watch" aria-labelledby="watch-title">
      <header class="panel-header">
        <div>
          <p class="eyebrow">Observation log</p>
          <h2 class="panel-title" id="watch-title">Watching and recent history</h2>
        </div>
        <span class="panel-count" id="watch-count">--</span>
      </header>
      <div class="surface-list" id="watch-list"></div>
    </section>
  </main>
</div>
<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
<script>
const $ = (id) => document.getElementById(id);
let previousStructure = "";
let refreshInFlight = false;

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
  if (!row.live) return "closed";
  return String(row.status || "unknown").replaceAll("_", " ");
}

function statusTone(row) {
  const status = String(row.status || "").toLowerCase();
  if (!row.live || status === "dismissed") return "neutral";
  if (status === "healthy" || status === "resumed") return "good";
  return "attention";
}

function isActionable(row) {
  if (!row.live) return false;
  const status = String(row.status || "").toLowerCase();
  return status === "sleeping" || status === "detected";
}

function classify(rows) {
  const live = rows.filter((row) => row.live);
  const attention = live.filter(isActionable);
  const armed = attention.filter((row) => row.armed);
  const ready = live.filter((row) => !isActionable(row) && (row.status === "healthy" || row.status === "resumed"));
  const quiet = live.filter((row) => !attention.includes(row) && !ready.includes(row));
  const history = rows.filter((row) => !row.live);
  const candidates = attention
    .filter((row) => row.seconds_until_reset !== null && row.seconds_until_reset !== undefined)
    .sort((left, right) => left.seconds_until_reset - right.seconds_until_reset);
  return { live, attention, armed, ready, quiet, history, next: candidates[0] || null };
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
  setMetric("attention-value", groups.attention.length);
  $("attention-detail").textContent = groups.attention.length
    ? plural(groups.attention.length, "surface") + " waiting on a recovery decision"
    : "All live surfaces are clear";
  $("attention-card").classList.toggle("is-clear", groups.attention.length === 0);
  setMetric("armed-value", groups.armed.length);
  $("armed-detail").textContent = groups.armed.length
    ? plural(groups.armed.length, "recovery") + " will resume automatically"
    : "No active recoveries are armed";
  setMetric("live-value", groups.live.length);
  $("live-detail").textContent = groups.ready.length
    ? plural(groups.ready.length, "surface") + " currently ready or healthy"
    : "No healthy live surfaces reported";
  setMetric("next-value", groups.next ? formatDuration(groups.next.seconds_until_reset) : "--");
  $("next-detail").textContent = groups.next
    ? "From " + displayName(groups.next)
    : "No active recovery window";
  $("queue-count").textContent = plural(groups.attention.length, "item");
  $("watch-count").textContent = plural(rows.length - groups.attention.length, "surface");
  return groups;
}

function renderMix(groups) {
  const total = Math.max(groups.live.length, 1);
  const segments = [
    { key: "attention", label: "attention", count: groups.attention.length },
    { key: "ready", label: "ready", count: groups.ready.length },
    { key: "quiet", label: "other", count: groups.quiet.length },
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
  input.checked = Boolean(row.armed);
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
    { label: "Needs attention", value: groups.attention.length, tone: "attention" },
    { label: "Ready or resumed", value: groups.ready.length, tone: "ready" },
    { label: "Other live state", value: groups.quiet.length, tone: "quiet" },
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
  } else if (groups.attention.length) {
    $("coverage-headline").textContent = plural(groups.attention.length, "live surface") + " needs review";
    $("coverage-detail").textContent = groups.armed.length
      ? plural(groups.armed.length, "recovery") + " remains armed for automatic resume."
      : "No active recovery is armed yet.";
  } else {
    $("coverage-headline").textContent = "Live sessions are in a clear posture";
    $("coverage-detail").textContent = "Keep monitoring active while YSYL watches for new limit banners.";
  }
}

function updateCountdowns(rows) {
  const secondsById = new Map(rows.map((row) => [String(row.surface_id), row.seconds_until_reset]));
  document.querySelectorAll(".js-countdown").forEach((node) => {
    node.textContent = formatDuration(secondsById.get(node.dataset.surfaceId));
  });
}

function structureSignature(rows) {
  return JSON.stringify(rows.map((row) => [
    row.surface_id, row.title, row.ref, row.agent_kind, row.status, row.armed,
    row.retry_count, row.preview, row.blocked, row.live,
  ]));
}

function renderDashboard(rows, groups) {
  renderMix(groups);
  renderActionQueue(groups.attention);
  renderCoverage(groups);
  renderWatchList(rows, groups);
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

async function saveSettings(value) {
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_window: value }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "Could not save history preference.");
    $("history").value = data.settings.history_window;
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
    const signature = structureSignature(rows);
    if (forceRender || signature !== previousStructure) {
      previousStructure = signature;
      renderDashboard(rows, groups);
    }
    updateCountdowns(rows);
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
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "history") saveSettings(target.value);
  if (target.matches("input[data-action='arm']")) {
    postAction("/api/arm", { surface_id: target.dataset.surfaceId, armed: target.checked });
  }
});

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const settings = await response.json();
    if (settings.history_window) $("history").value = settings.history_window;
  } catch (_) {
    // State polling remains useful even if the optional settings endpoint is unavailable.
  }
}

refresh(true);
loadSettings();
window.setInterval(refresh, 1000);
</script>
</body>
</html>
"""
