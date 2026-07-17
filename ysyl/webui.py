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
<title>YouSnoozeYouLose</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f1115; color: #e6e6e6; }
  @media (prefers-color-scheme: light) { body { background: #f6f7f9; color: #16181d; } }
  header { padding: 16px 20px; border-bottom: 1px solid #2a2e37; display: flex;
           align-items: baseline; gap: 12px; flex-wrap: wrap; }
  @media (prefers-color-scheme: light) { header { border-color: #dcdfe5; } }
  h1 { font-size: 18px; margin: 0; }
  .sub { opacity: .6; font-size: 12px; }
  main { padding: 16px 20px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 720px; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #23262e; vertical-align: top; }
  @media (prefers-color-scheme: light) { th, td { border-color: #e6e8ec; } }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; opacity: .6; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .st-sleeping { background: #1e3a5f; color: #9ecbff; }
  .st-resumed  { background: #143d2b; color: #7ee2a8; }
  .st-detected { background: #4a3a12; color: #f0cf7a; }
  .st-dismissed{ background: #3a2030; color: #e79bc0; }
  .st-healthy  { background: #1f2a1f; color: #7ee2a8; }
  .count { font-variant-numeric: tabular-nums; font-weight: 600; }
  .muted { opacity: .55; }
  .preview { max-width: 380px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; opacity: .8; }
  button { font: inherit; padding: 4px 10px; border-radius: 6px; border: 1px solid #3a3f4b;
           background: transparent; color: inherit; cursor: pointer; }
  button:hover { background: #ffffff14; }
  .empty { opacity: .55; padding: 40px 0; text-align: center; }
  label.arm { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>💤 YouSnoozeYouLose</h1>
  <span class="sub" id="sub">connecting…</span>
</header>
<main>
  <table>
    <thead>
      <tr>
        <th>Surface</th><th>Agent</th><th>Status</th><th>Resumes in</th>
        <th>Tries</th><th>Preview</th><th>Auto-resume</th><th></th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>No agent limits detected. YSYL is watching…</div>
</main>
<script>
function fmt(s) {
  if (s === null || s === undefined) return "—";
  if (s <= 0) return "due";
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  if (h) return h+"h "+m+"m";
  if (m) return m+"m "+sec+"s";
  return sec+"s";
}
async function post(url, payload) {
  await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  refresh();
}
function esc(t){ const d=document.createElement("div"); d.textContent=t==null?"":t; return d.innerHTML; }
async function refresh() {
  let data;
  try { data = await (await fetch("/api/state")).json(); }
  catch(e){ document.getElementById("sub").textContent = "daemon unreachable"; return; }
  const rows = data.surfaces || [];
  const blocked = rows.filter(r => r.blocked);
  const healthy = rows.filter(r => !r.blocked);
  document.getElementById("sub").textContent =
    rows.length + " agent surface" + (rows.length===1?"":"s") + " tracked" +
    (healthy.length ? " (" + healthy.length + " healthy)" : "") +
    " · updated " + new Date(data.now).toLocaleTimeString();
  const tb = document.getElementById("rows");
  document.getElementById("empty").hidden = rows.length !== 0;
  tb.innerHTML = rows.map(r => {
    const name = esc(r.title || r.ref || r.surface_id);
    const st = esc(r.status);
    const checked = r.armed ? "checked" : "";
    const dismissed = r.status === "dismissed";
    const isHealthy = r.status === "healthy";
    const actions = isHealthy
      ? `<span class="muted">watching</span>`
      : `<button onclick="post('/api/resume_now',{surface_id:'${r.surface_id}'})">Resume now</button>
         <button onclick="post('/api/dismiss',{surface_id:'${r.surface_id}'}" ${dismissed?"disabled":""}>Dismiss</button>`;
    const armCell = isHealthy
      ? `<span class="muted">—</span>`
      : `<label class="arm"><input type="checkbox" ${checked} ${dismissed?"disabled":""}
           onchange="post('/api/arm',{surface_id:'${r.surface_id}',armed:this.checked})"> armed</label>`;
    return `<tr>
      <td><div>${name}</div><div class="muted">${esc(r.ref||"")}</div></td>
      <td>${esc(r.agent_kind || "—")}</td>
      <td><span class="badge st-${st}">${st}</span></td>
      <td class="count">${fmt(r.seconds_until_reset)}</td>
      <td>${r.retry_count}</td>
      <td class="preview" title="${esc(r.preview||"")}">${esc(r.preview||"")}</td>
      <td>${armCell}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""
