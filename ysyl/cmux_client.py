"""Async wrapper around the cmux RPC subprocess."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from ysyl.models import SurfaceRef

logger = logging.getLogger(__name__)

# cmux drops the connection ("Broken pipe") when a client targets a stale/dead
# socket — e.g. a shell started under an older cmux instance whose
# CMUX_SOCKET_PATH now points at a socket the current app no longer serves.
# These stderr fragments mark failures worth retrying against the live socket.
_RETRYABLE_STDERR = (
    "broken pipe",
    "connection refused",
    "connection reset",
    "resource temporarily unavailable",
    "timed out",
    "timeout",
    "no such process",
    "socket is not connected",
)


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


class CmuxError(Exception):
    """Raised when a cmux RPC invocation fails or returns unexpected data."""

    def __init__(self, message: str, command: list[str] | None = None, stderr: str | None = None) -> None:
        super().__init__(message)
        self.command = command
        self.stderr = stderr

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.command is not None:
            parts.append(f"command: {' '.join(self.command)!r}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr!r}")
        return " | ".join(parts)


class CmuxClient:
    """Async wrapper for invoking ``cmux rpc`` commands."""

    # ``cmux`` is normally on PATH (the app installs a shim). Users can override
    # with an absolute path via Settings/config if it is not.
    DEFAULT_BIN = "cmux"

    def __init__(
        self,
        cmux_bin: str = DEFAULT_BIN,
        retries: int = 3,
        retry_delay: float = 0.4,
        password: str = "",
    ) -> None:
        self.cmux_bin = cmux_bin
        self.retries = max(1, retries)
        self.retry_delay = retry_delay
        self.password = password or ""

    def _base_args(self) -> list[str]:
        """cmux invocation prefix, including --password when configured."""
        args = [self.cmux_bin]
        if self.password:
            args += ["--password", self.password]
        return args

    async def __aenter__(self) -> CmuxClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    @staticmethod
    def live_socket_path() -> str | None:
        """The socket the *current* cmux app is serving.

        cmux records it in ``last-socket-path`` next to the socket files. We
        prefer that over a possibly-stale inherited ``CMUX_SOCKET_PATH`` so a
        shell left over from an older cmux instance still reaches the live app.
        """
        search: list[str] = []
        inherited = os.environ.get("CMUX_SOCKET_PATH")
        if inherited:
            search.append(os.path.dirname(inherited))
        search.append(os.path.expanduser("~/.local/state/cmux"))
        seen: set[str] = set()
        for directory in search:
            if directory in seen:
                continue
            seen.add(directory)
            record = os.path.join(directory, "last-socket-path")
            try:
                candidate = Path(record).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if candidate and _is_socket(candidate):
                return candidate
        return None

    def _child_env(self) -> dict[str, str]:
        """Environment for the cmux subprocess, pinned to the live socket."""
        env = dict(os.environ)
        live = self.live_socket_path()
        if live and env.get("CMUX_SOCKET_PATH") != live:
            logger.debug("Pinning CMUX_SOCKET_PATH to live socket %s", live)
            env["CMUX_SOCKET_PATH"] = live
        return env

    @staticmethod
    def _is_retryable(exc: CmuxError) -> bool:
        blob = f"{exc} {exc.stderr or ''}".lower()
        return any(marker in blob for marker in _RETRYABLE_STDERR)

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call ``cmux rpc``; retry transient socket drops against the live socket."""
        last_exc: CmuxError | None = None
        for attempt in range(self.retries):
            try:
                return await self._rpc_once(method, params)
            except CmuxError as exc:
                last_exc = exc
                if attempt == self.retries - 1 or not self._is_retryable(exc):
                    raise
                logger.debug(
                    "cmux rpc %s transient failure (attempt %d/%d): %s",
                    method, attempt + 1, self.retries, exc,
                )
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    async def _rpc_once(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One ``cmux rpc <method> '<json>'`` invocation; parse and return JSON."""
        payload = json.dumps(params or {})
        args = self._base_args() + ["rpc", method, payload]
        logger.debug("Running cmux RPC: %s rpc %s", self.cmux_bin, method)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
            )
        except OSError as exc:
            raise CmuxError(
                f"Could not launch cmux binary {self.cmux_bin!r}: {exc}",
                command=args,
                stderr=str(exc),
            ) from exc
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise CmuxError(
                f"cmux rpc {method} exited with code {proc.returncode}",
                command=args,
                stderr=stderr.decode("utf-8", errors="replace").strip(),
            )
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CmuxError(
                f"Failed to parse cmux response as JSON: {exc}",
                command=args,
                stderr=text[:500],
            ) from exc

    async def check(self) -> tuple[bool, str]:
        """Preflight connectivity test. Returns (ok, human-readable detail)."""
        try:
            surfaces = await self.list_surfaces()
        except CmuxError as exc:
            return False, str(exc)
        return True, f"reachable ({len(surfaces)} surface(s))"

    async def list_surfaces(self) -> list[SurfaceRef]:
        """Return all cmux surfaces.

        Real cmux returns ``{"surfaces": [{"id": ..., "ref": "surface:6",
        "type": "terminal", "resume_binding": {"kind": "claude", ...}, ...}]}``.
        We accept either that or a bare list, and key each surface by ``id``
        (falling back to ``surface_id``).
        """
        result = await self._rpc("surface.list")
        surfaces = result if isinstance(result, list) else result.get("surfaces", []) if isinstance(result, dict) else []
        out: list[SurfaceRef] = []
        for item in surfaces:
            if not isinstance(item, dict):
                continue
            binding = item.get("resume_binding")
            resume_kind = binding.get("kind") if isinstance(binding, dict) else None
            out.append(
                SurfaceRef(
                    surface_id=str(item.get("id") or item.get("surface_id") or ""),
                    ref=item.get("ref") or item.get("surface_ref"),
                    type=item.get("type"),
                    pane_ref=item.get("pane_ref"),
                    workspace_ref=item.get("workspace_ref"),
                    window_ref=item.get("window_ref"),
                    title=item.get("title"),
                    initial_command=item.get("initial_command"),
                    resume_kind=resume_kind,
                )
            )
        return out

    async def read_surface_text(self, surface_id: str) -> str:
        """Return the visible text of a surface, decoding base64 if needed."""
        result = await self._rpc("surface.read_text", {"surface_id": surface_id})
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if not isinstance(result, dict):
            raise CmuxError(f"Unexpected response type for surface.read_text: {type(result).__name__}")
        if "text" in result:
            return result["text"]
        if "data" in result:
            data = result["data"]
            if isinstance(data, str):
                try:
                    return base64.b64decode(data).decode("utf-8", errors="replace")
                except Exception as exc:
                    raise CmuxError(f"Failed to decode base64 surface text: {exc}") from exc
            return str(data)
        return ""

    async def send_key(self, surface_id: str, key: str) -> None:
        """Send a key to a surface."""
        await self._rpc("surface.send_key", {"surface_id": surface_id, "key": key})

    async def send_text(self, surface_id: str, text: str) -> None:
        """Send text to a surface."""
        await self._rpc("surface.send_text", {"surface_id": surface_id, "text": text})
