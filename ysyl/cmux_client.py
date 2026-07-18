"""Async wrapper around the cmux RPC subprocess."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
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

    @staticmethod
    def _extract_surfaces(result: Any) -> list:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("surfaces", [])
        return []

    @staticmethod
    def _parse_surface(item: dict) -> SurfaceRef:
        binding = item.get("resume_binding")
        resume_kind = binding.get("kind") if isinstance(binding, dict) else None
        checkpoint_id = binding.get("checkpoint_id") if isinstance(binding, dict) else None
        cwd = binding.get("cwd") if isinstance(binding, dict) else None
        return SurfaceRef(
            surface_id=str(item.get("id") or item.get("surface_id") or ""),
            ref=item.get("ref") or item.get("surface_ref"),
            type=item.get("type"),
            pane_ref=item.get("pane_ref"),
            workspace_ref=item.get("workspace_ref"),
            window_ref=item.get("window_ref"),
            title=item.get("title"),
            initial_command=item.get("initial_command"),
            resume_kind=resume_kind,
            checkpoint_id=checkpoint_id,
            cwd=cwd or item.get("requested_working_directory"),
        )

    async def _list_workspace_ids(self) -> list[str]:
        """All workspace ids in the window (cmux ``surface.list`` is per-workspace)."""
        try:
            result = await self._rpc("workspace.list")
        except CmuxError:
            return []
        workspaces = result.get("workspaces", []) if isinstance(result, dict) else []
        return [str(w["id"]) for w in workspaces if isinstance(w, dict) and w.get("id")]

    async def list_surfaces(self) -> list[SurfaceRef]:
        """Return cmux surfaces across **all** workspaces in the window.

        ``surface.list`` only returns the currently-selected workspace, so we
        enumerate every workspace and aggregate (deduping by surface id). Falls
        back to the plain call if workspace enumeration is unavailable.
        """
        raw: list[dict] = []
        seen: set[str] = set()
        for wid in await self._list_workspace_ids():
            try:
                result = await self._rpc("surface.list", {"workspace_id": wid})
            except CmuxError:
                continue
            for item in self._extract_surfaces(result):
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("id") or item.get("surface_id") or "")
                if sid and sid not in seen:
                    seen.add(sid)
                    raw.append(item)

        if not raw:  # fallback: current workspace only
            raw = [i for i in self._extract_surfaces(await self._rpc("surface.list")) if isinstance(i, dict)]

        return [self._parse_surface(i) for i in raw]

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

    async def open_saved_session(self, provider: str, session_id: str, cwd: str | None = None) -> bool:
        """Open a terminal pane and resume the exact saved Claude/Codex session."""
        if provider not in {"claude", "codex"}:
            return False
        existing_ids = {surface.surface_id for surface in await self.list_surfaces()}
        command = (
            f"claude --resume {shlex.quote(session_id)}"
            if provider == "claude"
            else f"codex resume {shlex.quote(session_id)}"
        )
        launch = f"cd {shlex.quote(cwd)} && exec {command}" if cwd else "exec " + command
        args = self._base_args() + ["new-pane", "--type", "terminal", "--focus", "true"]
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
                "cmux new-pane exited with code " + str(proc.returncode),
                command=args,
                stderr=stderr.decode("utf-8", errors="replace").strip(),
            )
        surface_id = self._created_surface_id(stdout.decode("utf-8", errors="replace"))
        if not surface_id:
            # ``new-pane`` sometimes returns only a pane id. Resolve its newly
            # created terminal surface instead of accidentally sending input to
            # that pane container.
            for _ in range(10):
                await asyncio.sleep(0.1)
                created = [
                    surface.surface_id
                    for surface in await self.list_surfaces()
                    if surface.surface_id not in existing_ids
                ]
                if len(created) == 1:
                    surface_id = created[0]
                    break
        if not surface_id:
            raise CmuxError("cmux new-pane did not return a surface id", command=args)
        # A newly-created terminal can acknowledge its id before the shell is
        # attached. Wait until it renders something before typing; otherwise
        # cmux silently drops the first paste on some terminals.
        for _ in range(10):
            await asyncio.sleep(0.2)
            try:
                if (await self.read_surface_text(surface_id)).strip():
                    break
            except CmuxError:
                continue
        await self.send_text(surface_id, launch + "\n")
        return True

    @staticmethod
    def _created_surface_id(output: str) -> str | None:
        """Extract the terminal id returned by cmux ``new-pane``."""
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return None
        if isinstance(result, dict):
            for key in ("surface_id", "surface_ref"):
                value = result.get(key)
                if value:
                    return str(value)
            surface = result.get("surface")
            if isinstance(surface, dict):
                value = surface.get("id") or surface.get("surface_id")
                if value:
                    return str(value)
        return None
