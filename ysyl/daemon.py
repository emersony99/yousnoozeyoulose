"""Main daemon orchestration for YouSnoozeYouLose.

The daemon polls cmux surfaces, keeps only the ones that look like Claude/Kimi
agents, detects usage/rate-limit blocks in their text, and — when the quota is
due to reset — resumes the blocked agent with a single keystroke. It re-checks
that the limit actually cleared and backs off/retries when it did not.

Public surface is intentionally aligned with the CLI and the test-suite:
``config=``, ``_load_state``/``save_state``, ``_states``, ``list_states``,
``_poll_once``, ``_resume_surface``, ``_resume_due``, ``dismiss``, ``arm`` and
``_shutdown``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ysyl.cmux_client import CmuxClient, CmuxError
from ysyl.config import Settings
from ysyl.detectors import build_detectors, detect_all
from ysyl.models import BlockState
from ysyl.scheduler import WakeScheduler

logger = logging.getLogger("ysyl.daemon")


def _str_or_none(value) -> str | None:
    """Coerce a value to ``str`` only when it already is one (else ``None``)."""
    return value if isinstance(value, str) else None


def _preview(text: str, limit: int = 240) -> str:
    """A short, single-line tail of surface text for display in the UI."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    joined = " | ".join(lines[-6:])
    return joined[-limit:]


class Daemon:
    """Poll cmux surfaces, detect blocks, and resume agents when quotas refresh."""

    def __init__(
        self,
        config: Settings,
        client: CmuxClient | None = None,
        scheduler: WakeScheduler | None = None,
    ) -> None:
        self.config = config
        self.client = client or CmuxClient(
            config.cmux_bin, retries=config.cmux_retries, password=config.cmux_password
        )
        self.scheduler = scheduler or WakeScheduler(config.sleep_tick_seconds)
        self.state_file = Path(config.state_file).expanduser()
        self._states: dict[str, BlockState] = {}
        self._shutdown = asyncio.Event()
        self._detectors = build_detectors(config.detector_banner_patterns)
        self._webui = None

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        """Load persisted block states. Accepts a bare list or ``{"blocks": [...]}``."""
        self._states = {}
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load state file %s: %s", self.state_file, exc)
            return

        if isinstance(raw, list):
            blocks = raw
        elif isinstance(raw, dict):
            blocks = raw.get("blocks", [])
        else:
            blocks = []

        for item in blocks:
            if not isinstance(item, dict):
                continue
            try:
                block = BlockState.from_state_dict(item)
            except Exception as exc:  # tolerate a corrupt entry
                logger.warning("Skipping unreadable state entry: %s", exc)
                continue
            self._states[block.surface_id] = block
        logger.info("Loaded %d persisted block state(s)", len(self._states))

    def save_state(self) -> None:
        """Persist current block states to disk atomically."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "blocks": [block.to_state_dict() for block in self._states.values()],
        }
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            logger.error("Failed to write state file %s: %s", self.state_file, exc)

    def list_states(self) -> list[BlockState]:
        """Return all tracked block states."""
        return list(self._states.values())

    # ----------------------------------------------------------- surface logic
    def _is_agent_surface(self, surface) -> bool:
        """Return True when a surface looks like a Claude/Kimi agent pane.

        Reliable signal: cmux tags wrapped agents with ``resume_binding.kind``
        (surfaced as ``resume_kind``). Fallback: configurable title/command
        substring match.
        """
        kind = getattr(surface, "resume_kind", None)
        if isinstance(kind, str) and kind.lower() in ("claude", "kimi"):
            return True
        haystack = " ".join(
            str(getattr(surface, attr, "") or "")
            for attr in ("title", "initial_command", "ref")
        ).lower()
        return any(pat.lower() in haystack for pat in self.config.agent_title_patterns)

    def _backoff(self, attempt: int) -> timedelta:
        """Exponential backoff (5 min → capped at 60 min) for retries/unknown resets."""
        minutes = min(5 * (2 ** max(attempt - 1, 0)), 60)
        return timedelta(minutes=minutes)

    def _capture(self, surface_id: str, text: str, reason: str) -> None:
        """Write raw surface text to the capture dir so patterns can be tuned."""
        try:
            directory = Path(self.config.capture_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            safe = surface_id.replace("/", "_")
            path = directory / f"{safe}-{stamp}.txt"
            header = f"# ysyl capture reason={reason} surface={surface_id} at={stamp}\n\n"
            path.write_text(header + text, encoding="utf-8")
            logger.info("Captured surface text for %s -> %s (%s)", surface_id, path, reason)
        except OSError as exc:
            logger.warning("Capture failed for %s: %s", surface_id, exc)

    def _detect_region(self, text: str) -> str:
        """The live tail of a surface — where a real limit banner appears.

        Scanning the whole scrollback yields false positives when the agent has
        merely *discussed* rate limits earlier in the session; the actual limit
        state is always at/near the bottom of the current view.
        """
        return "\n".join(text.splitlines()[-self.config.tail_lines:])

    def _observe(self, surface_id: str, title, ref, text: str, now: datetime) -> None:
        """Update tracked state for one agent surface from its current text."""
        title = _str_or_none(title)
        ref = _str_or_none(ref)
        existing = self._states.get(surface_id)
        if existing is not None and existing.status == "dismissed":
            return

        block = detect_all(
            self._detect_region(text),
            now,
            enabled=self.config.agents,
            detectors=self._detectors,
        )

        if block is None:
            # No limit banner visible. If we were tracking a block, it cleared.
            if existing is not None and existing.status in ("detected", "sleeping"):
                existing.status = "resumed"
                existing.preview = _preview(text)
                logger.info("Limit cleared on %s", ref or surface_id)
            return

        preview = _preview(text)

        if existing is None:
            block.surface_id = surface_id
            block.title = title
            block.ref = ref
            block.preview = preview
            block.armed = self.config.auto_arm
            if block.reset_at is None:
                self._capture(surface_id, text, reason="unparsed-reset")
                block.reset_at = now + self._backoff(1)
            elif self.config.capture_on_detect:
                self._capture(surface_id, text, reason="detect")
            block.status = "sleeping" if block.armed else "detected"
            self._states[surface_id] = block
            logger.info(
                "Detected %s limit on %s (reset=%s, armed=%s)",
                block.agent_kind,
                ref or surface_id,
                block.reset_at,
                block.armed,
            )
            return

        # Existing block still (or again) showing a limit banner.
        existing.title = title
        existing.ref = ref
        existing.preview = preview
        if block.reset_at is not None:
            existing.reset_at = block.reset_at

        if existing.status == "resumed":
            # Our resume attempt did not clear the limit — retry with backoff.
            existing.retry_count += 1
            if existing.retry_count >= self.config.max_retries:
                existing.status = "dismissed"
                logger.warning(
                    "Auto-dismissing %s after %d resume attempts",
                    ref or surface_id,
                    existing.retry_count,
                )
                return
            self._capture(surface_id, text, reason="resume-ineffective")
            if existing.reset_at is None or existing.reset_at <= now:
                existing.reset_at = now + self._backoff(existing.retry_count)
            existing.status = "sleeping" if existing.armed else "detected"
        elif existing.status == "detected":
            if existing.reset_at is None:
                existing.reset_at = now + self._backoff(1)
            existing.status = "sleeping" if existing.armed else "detected"
        # status == "sleeping": leave as-is (reset_at already refreshed above)

    def _earliest_reset(self, now: datetime) -> datetime | None:
        """Earliest future reset among armed, sleeping blocks."""
        candidates = [
            b.reset_at
            for b in self._states.values()
            if b.armed
            and b.status == "sleeping"
            and b.reset_at is not None
            and b.reset_at > now
        ]
        return min(candidates) if candidates else None

    async def _poll_once(self) -> None:
        """List surfaces, update state, then wait until the next reset is due."""
        now = datetime.now(timezone.utc)
        try:
            surfaces = await self.client.list_surfaces()
        except CmuxError as exc:
            logger.warning("Failed to list surfaces: %s", exc)
            await self._idle_wait()
            return

        for surface in surfaces:
            surface_id = getattr(surface, "surface_id", "") or ""
            if not surface_id or not self._is_agent_surface(surface):
                continue
            existing = self._states.get(surface_id)
            if existing is not None and existing.status == "dismissed":
                continue
            try:
                text = await self.client.read_surface_text(surface_id)
            except CmuxError as exc:
                logger.warning("Failed to read %s: %s", surface_id, exc)
                continue
            self._observe(
                surface_id,
                getattr(surface, "title", None),
                getattr(surface, "ref", None),
                text,
                now,
            )

        self.save_state()

        target = self._earliest_reset(now)
        if target is not None:
            logger.info("Waiting until %s for next reset", target.isoformat())
            await self._wait_for(target)
        else:
            await self._idle_wait()

    async def _wait_for(self, target: datetime) -> None:
        """Wait until ``target`` (sleep-resilient) or shutdown, whichever first."""
        sched = asyncio.create_task(
            self.scheduler.schedule(target, on_tick=self._on_tick)
        )
        stop = asyncio.create_task(self._shutdown.wait())
        done, pending = await asyncio.wait(
            {sched, stop}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if sched in done and not sched.cancelled():
            exc = sched.exception()
            if exc is not None:
                raise exc

    async def _idle_wait(self) -> None:
        """Sleep one poll interval, but return immediately on shutdown."""
        try:
            await asyncio.wait_for(
                self._shutdown.wait(), timeout=self.config.poll_interval_seconds
            )
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def _on_tick(self, now: datetime) -> None:
        logger.debug("Scheduler tick at %s", now.isoformat())

    # --------------------------------------------------------------- resuming
    async def _resume_due(self) -> None:
        """Resume every armed, sleeping block whose reset time has passed."""
        now = datetime.now(timezone.utc)
        for surface_id, block in list(self._states.items()):
            if block.status != "sleeping" or not block.armed:
                continue
            if block.reset_at is not None and now < block.reset_at:
                continue
            await self._resume_surface(surface_id)

    async def _resume_surface(self, surface_id: str) -> None:
        """Send the resume action to a blocked agent surface."""
        block = self._states.get(surface_id)
        if block is None:
            return
        try:
            if self.config.resume_action == "text":
                await self.client.send_text(block.surface_id, self.config.resume_text + "\n")
            else:
                await self.client.send_key(block.surface_id, "return")
            block.status = "resumed"
            block.retry_count += 1
            logger.info(
                "Resumed %s (%s), attempt %d",
                block.ref or surface_id,
                block.agent_kind,
                block.retry_count,
            )
        except CmuxError as exc:
            block.retry_count += 1
            logger.warning("Resume failed for %s: %s", block.ref or surface_id, exc)
            if block.retry_count >= self.config.max_retries:
                logger.warning("Dismissing %s after %d failures", surface_id, block.retry_count)
                block.status = "dismissed"
        self.save_state()

    # ----------------------------------------------------------- user actions
    def dismiss(self, surface_id: str) -> bool:
        """Mark a block state as dismissed (excluded from auto-resume)."""
        if surface_id not in self._states:
            return False
        self._states[surface_id].status = "dismissed"
        self.save_state()
        return True

    def arm(self, surface_id: str, armed: bool = True) -> bool:
        """Toggle whether a surface is allowed to auto-resume."""
        block = self._states.get(surface_id)
        if block is None:
            return False
        block.armed = armed
        if not armed and block.status == "sleeping":
            block.status = "detected"  # keep visible, but do not resume
        elif armed and block.status == "detected" and block.reset_at is not None:
            block.status = "sleeping"
        self.save_state()
        return True

    # ------------------------------------------------------------------- loop
    def _setup_signals(self) -> None:
        """Register SIGINT/SIGTERM handlers to request a clean shutdown."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Signals may be unavailable (e.g. non-main thread / Windows).
                pass

    async def _preflight(self) -> bool:
        """Check cmux is reachable once at startup and log an actionable error."""
        ok, detail = await self.client.check()
        if ok:
            logger.info("cmux %s", detail)
            return True
        sock = os.environ.get("CMUX_SOCKET_PATH", "<unset>")
        live = CmuxClient.live_socket_path() or "<not found>"
        logger.error("Cannot reach cmux: %s", detail)
        logger.error(
            "  CMUX_SOCKET_PATH=%s | live socket=%s | binary=%s",
            sock, live, self.config.cmux_bin,
        )
        logger.error(
            "  Fix: run `ysyl doctor`, and start ysyl from a terminal inside the "
            "current cmux app. The daemon will keep retrying."
        )
        return False

    async def _start_webui(self) -> None:
        if not self.config.ui_enabled:
            return
        from ysyl.webui import WebUI

        self._webui = WebUI(self, self.config.ui_host, self.config.ui_port)
        try:
            await self._webui.start()
            logger.info(
                "Dashboard: http://%s:%d", self.config.ui_host, self.config.ui_port
            )
        except OSError as exc:
            logger.warning(
                "Web UI failed to start on %s:%d: %s",
                self.config.ui_host,
                self.config.ui_port,
                exc,
            )
            self._webui = None

    async def _stop_webui(self) -> None:
        if self._webui is not None:
            try:
                await self._webui.stop()
            except Exception as exc:  # best-effort shutdown
                logger.debug("Web UI stop error: %s", exc)
            self._webui = None

    async def run(self) -> None:
        """Main daemon loop."""
        self._setup_signals()
        self._load_state()
        logger.info("YSYL daemon started")
        logger.info("Using cmux binary: %s", self.config.cmux_bin)

        if not self._shutdown.is_set():
            await self._preflight()
            await self._start_webui()

        while not self._shutdown.is_set():
            try:
                await self._poll_once()
                if self._shutdown.is_set():
                    break
                await self._resume_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the daemon alive through transient errors
                logger.exception("Poll loop error: %s", exc)
                await self._idle_wait()

        await self._stop_webui()
        logger.info("YSYL daemon shutting down")
        self.save_state()
