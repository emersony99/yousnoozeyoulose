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
import re
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


# cmux only tags Claude panes with resume_binding.kind; a running Codex or Kimi
# session looks like a plain terminal (kind=None, cwd as title). These match the
# distinctive live UI each shows, so we can still recognise them by content.
_KIMI_SIG = re.compile(r"Welcome to Kimi Code|Kimi Code CLI|kimi-code/kimi", re.IGNORECASE)
# Kimi's current terminal UI can omit its product name entirely once an agent
# fails. Require all three provider-specific fragments so a generic 403 or an
# ordinary terminal discussion about quotas cannot be misclassified as Kimi.
_KIMI_BILLING_LIMIT_SIG = re.compile(
    r"(?=.*\[provider\.api_error\]\s*403)"
    r"(?=.*\breached your usage limit for this billing cycle\b)"
    r"(?=.*https?://(?:www\.)?kimi\.com/membership(?:/|\?|\b))",
    re.IGNORECASE | re.DOTALL,
)
_CODEX_SIG = re.compile(
    r"·\s*~?/[^\n·]+·\s*\S+\s*\[[^\]]*\]"      # "<model> · ~/cwd · Main [default]" footer
    r"|\bgpt-[0-9][\w.\-]*\b[^\n]*·"           # a gpt-* model status line
    r"|\bcodex\b\s+(?:resume|exec|--)",
    re.IGNORECASE,
)
_CLAUDE_SIG = re.compile(
    r"auto mode on \(shift\+tab|\bClaude Code\b|esc to interrupt", re.IGNORECASE
)

# Active-time = sum of gaps between consecutive session events that are short
# enough to count as continuous work (longer gaps are idle, not uptime).
_TS_MS_RE = re.compile(r'"(?:time|created_at|timestamp|ts)":\s*(\d{10,13})')


def _iso_epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _active_seconds(epochs, idle_cap: float) -> float:
    """Sum consecutive-event gaps that are <= idle_cap (i.e. active, not idle)."""
    points = sorted(e for e in epochs if e is not None)
    total = 0.0
    for earlier, later in zip(points, points[1:]):
        gap = later - earlier
        if 0 < gap <= idle_cap:
            total += gap
    return total


def _idle_seconds(epochs, active_cap: float, present_cap: float) -> float:
    """Sum gaps that are idle-but-present: longer than active_cap, up to present_cap.

    Gaps beyond present_cap are treated as 'away' (the session was left running) and
    are not counted as downtime.
    """
    points = sorted(e for e in epochs if e is not None)
    total = 0.0
    for earlier, later in zip(points, points[1:]):
        gap = later - earlier
        if active_cap < gap <= present_cap:
            total += gap
    return total


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
        self._watched: dict[str, SurfaceRef] = {}
        self._activity: dict[str, str] = {}  # surface_id -> live tail preview (all agents)
        self._kinds: dict[str, str] = {}     # surface_id -> agent kind (incl. content-detected)
        self._ledger: dict[str, dict] = {}   # session id -> every agent session ever seen
        self._activity_changed_at: dict[str, datetime] = {}
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

    # ----------------------------------------------------------- UI settings
    def load_ui_settings(self) -> dict:
        """Load runtime UI-editable settings (e.g. history_window)."""
        path = Path(self.config.settings_file).expanduser()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load UI settings %s: %s", path, exc)
            return {}

    def save_ui_settings(self, updates: dict) -> None:
        """Merge updates into the UI settings file and apply to config."""
        if "history_window" in updates:
            window = str(updates["history_window"]).lower()
            if window not in {"3d", "1w", "3w"}:
                raise ValueError("history_window must be one of ['3d', '1w', '3w']")
        if "color_theme" in updates:
            theme = str(updates["color_theme"]).lower()
            if theme == "system":  # migrate the former automatic palette to Forest
                theme = "forest"
            if theme not in {"forest", "midnight", "moss", "ocean", "orchid"}:
                raise ValueError(
                    "color_theme must be one of ['forest', 'midnight', 'moss', 'ocean', 'orchid']"
                )
            updates["color_theme"] = theme
        path = Path(self.config.settings_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        current = self.load_ui_settings()
        current.update(updates)
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to write UI settings %s: %s", path, exc)
            return

        # Apply known UI settings to the in-memory config.
        if "history_window" in updates:
            window = str(updates["history_window"]).lower()
            mapping = {"3d": 72, "1w": 168, "3w": 504}
            self.config.history_window = window
            self.config.prune_resumed_after_hours = mapping[window]
            self.config.prune_dismissed_after_hours = mapping[window]

    def get_ui_settings(self) -> dict:
        """Return current UI-editable settings."""
        stored = self.load_ui_settings()
        stored_theme = str(stored.get("color_theme", "midnight")).lower()
        theme = stored_theme if stored_theme in {"forest", "midnight", "moss", "ocean", "orchid"} else "midnight"
        return {
            "history_window": self.config.history_window,
            "color_theme": theme,
        }

    def list_states(self) -> list[BlockState]:
        """Return all tracked block states."""
        return list(self._states.values())

    def _is_recently_working(self, surface_id: str, now: datetime) -> bool:
        """Return whether a surface's visible output changed within two polls."""
        changed_at = self._activity_changed_at.get(surface_id)
        if changed_at is None:
            return False
        window_seconds = max(self.config.poll_interval_seconds * 2, 15)
        return now - changed_at <= timedelta(seconds=window_seconds)

    def list_watched(self) -> list[dict]:
        """Return every agent surface being watched, with optional block state.

        Also includes recently persisted block states for surfaces that are no
        longer live in cmux (closed tabs), so the dashboard keeps a week's view.
        """
        now = datetime.now(timezone.utc)
        rows: list[dict] = []
        seen: set[str] = set()

        for surface_id, surface in self._watched.items():
            seen.add(surface_id)
            block = self._states.get(surface_id)
            seconds = None
            if block and block.reset_at is not None:
                seconds = int((block.reset_at - now).total_seconds())
            rows.append(
                {
                    "surface_id": surface_id,
                    "session_id": _str_or_none(getattr(surface, "checkpoint_id", None)) or surface_id,
                    "ref": getattr(surface, "ref", None),
                    "title": getattr(surface, "title", None),
                    "agent_kind": (block.agent_kind if block
                                   else self._kinds.get(surface_id) or self._infer_agent_kind(surface)),
                    "status": block.status if block else "healthy",
                    "armed": block.armed if block else True,
                    "reset_at": block.reset_at.isoformat() if block and block.reset_at else None,
                    "seconds_until_reset": seconds,
                    "retry_count": block.retry_count if block else 0,
                    "preview": block.preview if block else None,
                    "resumed_by_ysyl": block.resumed_by_ysyl if block else False,
                    "activity": self._activity.get(surface_id),
                    "last_message": self._last_message_for(surface_id, surface),
                    "is_working": self._is_recently_working(surface_id, now),
                    "blocked": bool(block and block.status in ("detected", "sleeping")),
                    "live": True,
                }
            )

        # Historical sessions from the past week that are no longer open.
        for surface_id, block in self._states.items():
            if surface_id in seen:
                continue
            seconds = None
            if block.reset_at is not None:
                seconds = int((block.reset_at - now).total_seconds())
            rows.append(
                {
                    "surface_id": surface_id,
                    "ref": block.ref,
                    "title": block.title,
                    "agent_kind": block.agent_kind,
                    "status": block.status,
                    "armed": block.armed,
                    "reset_at": block.reset_at.isoformat() if block.reset_at else None,
                    "seconds_until_reset": seconds,
                    "retry_count": block.retry_count,
                    "preview": block.preview,
                    "resumed_by_ysyl": block.resumed_by_ysyl,
                    "is_working": False,
                    "blocked": block.status in ("detected", "sleeping"),
                    "live": False,
                }
            )

        return rows

    def help_summary(self, now: datetime | None = None) -> dict:
        """Summarise the recovery work YSYL has recorded in the recent window."""
        now = now or datetime.now(timezone.utc)
        days = 14
        local_now = now.astimezone()
        start = local_now.date() - timedelta(days=days - 1)
        heatmap = {
            (start + timedelta(days=offset)).isoformat(): [
                {"interventions": 0, "prompts": 0, "resumes": {"claude": 0, "codex": 0, "kimi": 0}}
                for _ in range(24)
            ]
            for offset in range(days)
        }
        recent = []
        for block in self._states.values():
            detected = block.detected_at.astimezone()
            day = detected.date().isoformat()
            if day in heatmap:
                heatmap[day][detected.hour]["interventions"] += 1
            if detected.date() >= start:
                recent.append(block)
            if block.resumed_at:
                resumed = block.resumed_at.astimezone()
                resumed_day = resumed.date().isoformat()
                if resumed_day in heatmap:
                    heatmap[resumed_day][resumed.hour]["resumes"][block.agent_kind] += 1
        prompt_count = 0
        seen_prompts: set[tuple[str, str]] = set()
        for entry in self._ledger.values():
            for raw_time in entry.get("prompt_times") or []:
                key = (str(entry.get("id", "")), str(raw_time))
                if key in seen_prompts:
                    continue
                seen_prompts.add(key)
                prompt = self._parse_iso(raw_time)
                if prompt is None:
                    continue
                if prompt.tzinfo is None:
                    prompt = prompt.replace(tzinfo=timezone.utc)
                local_prompt = prompt.astimezone()
                prompt_day = local_prompt.date().isoformat()
                if prompt_day in heatmap:
                    heatmap[prompt_day][local_prompt.hour]["prompts"] += 1
                    prompt_count += 1
        return {
            "detected": len(recent),
            "recovered": sum(block.status == "resumed" for block in recent),
            "waiting": sum(block.status in ("detected", "sleeping") for block in recent),
            "prompts": prompt_count,
            "heatmap": [
                {"date": day, "hours": hours}
                for day, hours in heatmap.items()
            ],
        }

    async def open_session(self, session_id: str) -> bool:
        """Open a focused terminal pane resuming the exact ledger session."""
        session = self._ledger.get(session_id)
        if not session:
            return False
        try:
            opened = await self.client.open_saved_session(
                session.get("kind", ""), session_id, session.get("cwd")
            )
        except CmuxError as exc:
            logger.warning("Could not open cmux pane for session %s: %s", session_id, exc)
            return False
        if not opened:
            return False

        # The old source pane may keep its historical quota banner on screen
        # after its session has been restored elsewhere. Mark that tracked
        # surface as recovered instead of letting stale text turn it red again.
        matching_ids = {session_id}
        matching_ids.update(
            surface_id
            for surface_id, surface in self._watched.items()
            if _str_or_none(getattr(surface, "checkpoint_id", None)) == session_id
        )
        changed = False
        for surface_id in matching_ids:
            block = self._states.get(surface_id)
            if block and block.status in ("detected", "sleeping"):
                block.status = "resumed"
                block.resumed_by_ysyl = True
                block.resumed_at = datetime.now(timezone.utc)
                block.preview = "Resumed by YSYL in a new cmux pane"
                changed = True
        if changed:
            self.save_state()
        return True

    def _last_message_for(self, surface_id: str, surface) -> str | None:
        """Return the newest user message associated with a live surface.

        Claude exposes its checkpoint id in cmux, so that is an exact ledger
        lookup. Other agent CLIs do not always expose a session id; for those,
        use the newest session with the same kind and working directory.
        """
        checkpoint_id = _str_or_none(getattr(surface, "checkpoint_id", None))
        direct = self._ledger.get(checkpoint_id or surface_id)
        if direct and direct.get("last_message"):
            return str(direct["last_message"])

        kind = self._kinds.get(surface_id) or self._infer_agent_kind(surface)
        cwd = _str_or_none(getattr(surface, "cwd", None))
        candidates = [
            entry for entry in self._ledger.values()
            if entry.get("last_message")
            and entry.get("kind") == kind
            and (not cwd or entry.get("cwd") == cwd)
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda entry: entry.get("last_active") or entry.get("_mtime") or "")
        return str(newest["last_message"])

    # --------------------------------------------------------- session ledger
    _WINDOW_HOURS = {"3d": 72, "1w": 168, "3w": 504}

    def _window_hours(self) -> int:
        """History window in hours, from the UI-selected history_window."""
        return self._WINDOW_HOURS.get(
            getattr(self.config, "history_window", "1w"), 168
        )

    def _load_ledger(self) -> None:
        """Load the persistent session ledger."""
        self._ledger = {}
        path = Path(self.config.sessions_file).expanduser()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load session ledger %s: %s", path, exc)
            return
        entries = raw.get("sessions", []) if isinstance(raw, dict) else raw
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("id"):
                self._ledger[str(e["id"])] = e
        logger.info("Loaded %d session ledger entrie(s)", len(self._ledger))

    def save_ledger(self) -> None:
        """Persist the session ledger atomically."""
        path = Path(self.config.sessions_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "sessions": list(self._ledger.values())}
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to write session ledger %s: %s", path, exc)

    def _update_ledger(self, now: datetime, watched: dict, activity: dict, kinds: dict | None = None) -> None:
        """Record every currently-watched agent session; mark the rest closed."""
        kinds = kinds or {}
        iso = now.isoformat()
        live_keys: set[str] = set()
        for surface_id, surface in watched.items():
            checkpoint_id = _str_or_none(getattr(surface, "checkpoint_id", None))
            key = checkpoint_id or surface_id
            live_keys.add(key)
            block = self._states.get(surface_id)
            kind = (kinds.get(surface_id) or self._infer_agent_kind(surface)
                    or (block.agent_kind if block else None) or "agent")
            entry = self._ledger.get(key) or {"id": key, "first_seen": iso, "source": "live"}
            entry.update(
                {
                    "surface_id": surface_id,
                    "checkpoint_id": checkpoint_id,
                    "kind": kind,
                    "title": _str_or_none(getattr(surface, "title", None)) or entry.get("title"),
                    "ref": _str_or_none(getattr(surface, "ref", None)),
                    "cwd": _str_or_none(getattr(surface, "cwd", None)) or entry.get("cwd"),
                    "activity": activity.get(surface_id) or entry.get("activity"),
                    "last_message": entry.get("last_message"),
                    "last_active": iso,
                    "open": True,
                    "blocked": bool(block and block.status in ("detected", "sleeping")),
                }
            )
            entry["source"] = "both" if entry.get("source") == "claude-history" else "live"
            self._ledger[key] = entry
        for key, entry in self._ledger.items():
            if key not in live_keys and entry.get("open"):
                entry["open"] = False

    @staticmethod
    def _read_claude_meta(path: Path) -> dict:
        """First and latest real prompts plus timestamps from a Claude session."""
        title = last_message = first_ts = cwd = None
        epochs: list[float] = []
        prompt_times: list[str] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    first_ts = first_ts or d.get("timestamp")
                    ep = _iso_epoch(d.get("timestamp"))
                    if ep is not None:
                        epochs.append(ep)
                    cwd = cwd or d.get("cwd")
                    if d.get("type") == "user":
                        content = (d.get("message") or {}).get("content")
                        if isinstance(content, list):
                            content = " ".join(
                                p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                        text = str(content or "").strip().replace("\n", " ")
                        if text and not text.startswith(("<local-command", "<command-", "Caveat:", "[SYSTEM")):
                            title = title or text[:120]
                            last_message = text[:240]
                            if d.get("timestamp"):
                                prompt_times.append(str(d["timestamp"]))
        except OSError:
            pass
        return {"title": title, "last_message": last_message, "first_ts": first_ts,
                "cwd": cwd, "epochs": epochs, "prompt_times": prompt_times}

    def _merge_history_entry(self, sid, kind, *, title, last_message, prompt_times, cwd, first_iso, last_iso,
                             mtime_iso, file_key=None, active_seconds=0.0, idle_seconds=0.0) -> None:
        """Insert/update a closed history session in the ledger.

        ``active_seconds`` is the active (non-idle) time for the file identified by
        ``file_key``; a session's total active time is the sum across its files (a
        Codex conversation spans many rollout files), so we track per-file and sum.
        """
        entry = self._ledger.get(sid)
        if entry is None:
            entry = {"id": sid, "source": f"{kind}-history", "open": False, "blocked": False}
        entry.setdefault("kind", kind)
        entry["title"] = entry.get("title") or title or f"{kind.title()} session"
        entry["last_message"] = last_message or entry.get("last_message")
        entry["cwd"] = entry.get("cwd") or cwd
        entry["first_seen"] = entry.get("first_seen") or first_iso or mtime_iso
        entry["last_active"] = max(x for x in (entry.get("last_active"), last_iso, mtime_iso) if x)
        entry["_mtime"] = mtime_iso
        key = file_key if file_key is not None else sid
        by_file = entry.setdefault("_active_by_file", {})
        by_file[key] = active_seconds
        entry["active_seconds"] = round(sum(by_file.values()))
        idle_by_file = entry.setdefault("_idle_by_file", {})
        idle_by_file[key] = idle_seconds
        entry["idle_seconds"] = round(sum(idle_by_file.values()))
        prompts_by_file = entry.setdefault("_prompts_by_file", {})
        prompts_by_file[file_key if file_key is not None else sid] = list(prompt_times or [])
        entry["prompt_times"] = sorted({
            timestamp for values in prompts_by_file.values() for timestamp in values
        })
        if entry.get("source") == "live":
            entry["source"] = "both"
        self._ledger[sid] = entry

    def _backfill_history(self, now: datetime) -> None:
        """Merge past Claude / Codex / Kimi sessions found on disk into the ledger."""
        if self.config.backfill_claude_history:
            self._backfill_claude_sessions(now)
        if self.config.backfill_codex_history:
            self._backfill_codex_sessions(now)
        if self.config.backfill_kimi_history:
            self._backfill_kimi_sessions(now)

    def _backfill_claude_sessions(self, now: datetime) -> None:
        """Claude Code history: one ~/.claude/projects/<cwd>/<id>.jsonl per session."""
        base = Path(self.config.claude_projects_dir).expanduser()
        if not base.is_dir():
            return
        cutoff = now - timedelta(hours=self._window_hours())
        for proj in base.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                if (e := self._ledger.get(f.stem)) and e.get("_mtime") == mtime.isoformat():
                    continue
                meta = self._read_claude_meta(f)
                self._merge_history_entry(
                    f.stem, "claude", title=meta["title"], last_message=meta["last_message"], prompt_times=meta["prompt_times"], cwd=meta["cwd"],
                    first_iso=meta["first_ts"], last_iso=None, mtime_iso=mtime.isoformat(),
                    file_key=str(f),
                    active_seconds=_active_seconds(meta["epochs"], self.config.active_idle_gap_seconds),
                    idle_seconds=_idle_seconds(meta["epochs"], self.config.active_idle_gap_seconds, self.config.present_idle_gap_seconds),
                )

    def _backfill_codex_sessions(self, now: datetime) -> None:
        """Codex history: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (scan by day)."""
        base = Path(self.config.codex_sessions_dir).expanduser()
        if not base.is_dir():
            return
        cutoff = now - timedelta(hours=self._window_hours())
        day = cutoff.date()
        while day <= now.date():
            ddir = base / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            day += timedelta(days=1)
            if not ddir.is_dir():
                continue
            for f in ddir.glob("rollout-*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                meta = self._read_codex_meta(f)
                sid = meta["id"] or f.stem
                if (e := self._ledger.get(sid)) and e.get("_mtime") == mtime.isoformat():
                    continue
                self._merge_history_entry(
                    sid, "codex", title=meta["title"], last_message=meta["last_message"], prompt_times=meta["prompt_times"], cwd=meta["cwd"],
                    first_iso=meta["first_ts"], last_iso=None, mtime_iso=mtime.isoformat(),
                    file_key=str(f),
                    active_seconds=_active_seconds(meta["epochs"], self.config.active_idle_gap_seconds),
                    idle_seconds=_idle_seconds(meta["epochs"], self.config.active_idle_gap_seconds, self.config.present_idle_gap_seconds),
                )

    @staticmethod
    def _read_codex_meta(path: Path) -> dict:
        """Session id / cwd / time plus first and latest Codex prompts."""
        cid = cwd = first_ts = title = last_message = None
        epochs: list[float] = []
        prompt_times: list[str] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ep = _iso_epoch(d.get("timestamp"))
                    if ep is not None:
                        epochs.append(ep)
                    if d.get("type") == "session_meta":
                        p = d.get("payload", {}) or {}
                        # Prefer the root session id so the many rollout files Codex
                        # writes per conversation collapse into one session.
                        cid = cid or p.get("session_id") or p.get("parent_thread_id") or p.get("id")
                        cwd = cwd or p.get("cwd")
                        first_ts = first_ts or p.get("timestamp") or d.get("timestamp")
                    elif d.get("type") == "event_msg":
                        p = d.get("payload", {}) or {}
                        if p.get("type") == "user_message":
                            text = str(p.get("message") or "").strip().replace("\n", " ")
                            if text and not text.startswith("<"):
                                title = title or text[:120]
                                last_message = text[:240]
                                timestamp = d.get("timestamp") or p.get("timestamp")
                                if timestamp:
                                    prompt_times.append(str(timestamp))
        except OSError:
            pass
        return {"id": cid, "cwd": cwd, "first_ts": first_ts, "title": title,
                "last_message": last_message, "epochs": epochs, "prompt_times": prompt_times}

    def _backfill_kimi_sessions(self, now: datetime) -> None:
        """Kimi history: session_index.jsonl -> each sessionDir/state.json."""
        index = Path(self.config.kimi_index_file).expanduser()
        if not index.is_file():
            return
        cutoff = now - timedelta(hours=self._window_hours())
        try:
            lines = index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid, sdir = rec.get("sessionId"), rec.get("sessionDir")
            if not sid or not sdir:
                continue
            state = Path(sdir).expanduser() / "state.json"
            try:
                mtime = datetime.fromtimestamp(state.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if (e := self._ledger.get(sid)) and e.get("_mtime") == mtime.isoformat():
                continue
            meta = self._read_kimi_meta(state, rec.get("workDir"))
            self._merge_history_entry(
                sid, "kimi", title=meta["title"], last_message=meta["last_message"], prompt_times=meta["prompt_times"], cwd=meta["cwd"],
                first_iso=meta["first_ts"], last_iso=meta["last_ts"], mtime_iso=mtime.isoformat(),
                file_key=str(state),
                active_seconds=_active_seconds(meta["epochs"], self.config.active_idle_gap_seconds),
                idle_seconds=_idle_seconds(meta["epochs"], self.config.active_idle_gap_seconds, self.config.present_idle_gap_seconds),
            )

    @staticmethod
    def _read_kimi_meta(state_path: Path, work_dir) -> dict:
        """Title / cwd / timestamps from a Kimi session ``state.json`` (+ wire log)."""
        title = cwd = first_ts = last_ts = None
        try:
            d = json.loads(state_path.read_text(encoding="utf-8"))
            candidate = str(d.get("title") or "").strip()
            if candidate and candidate.lower() != "new session":
                title = candidate[:120]
            cwd = d.get("workDir") or work_dir
            first_ts = d.get("createdAt")
            last_ts = d.get("updatedAt")
        except (OSError, json.JSONDecodeError):
            cwd = work_dir
        # state.json has only created/updated; the per-event timestamps that give
        # real active time live in the main agent's wire log (epoch millis).
        epochs: list[float] = []
        try:
            wire = state_path.parent / "agents" / "main" / "wire.jsonl"
            for m in _TS_MS_RE.finditer(wire.read_text(encoding="utf-8", errors="replace")):
                value = float(m.group(1))
                epochs.append(value / 1000 if value > 1e11 else value)
        except OSError:
            pass
        return {"title": title, "last_message": title, "cwd": cwd,
                "first_ts": first_ts, "last_ts": last_ts, "epochs": epochs, "prompt_times": []}

    def _prune_ledger(self, now: datetime) -> None:
        """Drop ledger entries older than the widest history window.

        Pruning always keeps up to the widest selectable window (3w) so that
        widening ``history_window`` in the UI immediately surfaces older
        sessions that are still retained; ``list_sessions`` applies the
        currently-selected (possibly narrower) window at read time.
        """
        cutoff = now - timedelta(hours=max(self._WINDOW_HOURS.values()))
        for key in [
            k for k, e in self._ledger.items()
            if not e.get("open") and self._parse_iso(e.get("last_active")) and self._parse_iso(e["last_active"]) < cutoff
        ]:
            self._ledger.pop(key, None)

    @staticmethod
    def _parse_iso(value):
        try:
            return datetime.fromisoformat(value) if value else None
        except (ValueError, TypeError):
            return None

    def list_sessions(self) -> list[dict]:
        """Agent sessions within the current history window (open first, then most-recent).

        The window is applied at read time so changing ``history_window`` in the UI
        takes effect immediately, rather than waiting for the next prune pass.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._window_hours())
        rows = [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in self._ledger.values()
            if e.get("open")
            or not self._parse_iso(e.get("last_active"))
            or self._parse_iso(e["last_active"]) >= cutoff
        ]
        # Most-recent first, then a stable pass to float open sessions to the top.
        rows.sort(key=lambda r: r.get("last_active") or "", reverse=True)
        rows.sort(key=lambda r: 0 if r.get("open") else 1)
        return rows

    # ----------------------------------------------------------- surface logic
    def _haystack(self, surface) -> str:
        return " ".join(
            str(getattr(surface, attr, "") or "")
            for attr in ("title", "initial_command", "ref")
        ).lower()

    def _matches_title_patterns(self, surface) -> bool:
        haystack = self._haystack(surface)
        return any(pat.lower() in haystack for pat in self.config.agent_title_patterns)

    def _infer_agent_kind(self, surface) -> str | None:
        """Best-effort agent kind from cmux surface metadata.

        Priority:
        1. cmux ``resume_binding.kind`` (the most reliable signal).
        2. Title / initial_command / ref substring heuristics that map to a
           known agent kind ("claude" or "kimi"). Ambiguous patterns such as
           "role:" only tell us the surface is an agent session, not which one,
           so they return ``None`` and both detectors are tried.
        """
        kind = getattr(surface, "resume_kind", None)
        if isinstance(kind, str) and kind.lower() in ("claude", "codex", "kimi"):
            return kind.lower()

        haystack = self._haystack(surface)
        for pat in self.config.agent_title_patterns:
            if pat.lower() not in haystack:
                continue
            lowered = pat.lower()
            if lowered in ("claude", "codex", "kimi"):
                return lowered
            # Ambiguous pattern (e.g. "role:"): surface is an agent, but we
            # need to read the text to know which one.
            return None
        return None

    def _is_agent_surface(self, surface) -> bool:
        """Return True when a surface looks like a Claude/Codex/Kimi agent pane."""
        kind = getattr(surface, "resume_kind", None)
        if isinstance(kind, str) and kind.lower() in ("claude", "codex", "kimi"):
            return True
        return self._matches_title_patterns(surface)

    def _content_agent_kind(self, text: str) -> str | None:
        """Recognise an agent from its live terminal UI when metadata can't.

        cmux only tags Claude panes; a Codex or Kimi session shows up as a plain
        terminal, so we sniff the visible tail for each tool's distinctive UI.
        """
        tail = "\n".join(text.splitlines()[-self.config.tail_lines:])
        if _KIMI_SIG.search(tail) or _KIMI_BILLING_LIMIT_SIG.search(tail):
            return "kimi"
        if _CODEX_SIG.search(tail):
            return "codex"
        if _CLAUDE_SIG.search(tail):
            return "claude"
        return None

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

    def _debug_snapshot(self, surface_id: str, text: str, kind: str | None) -> None:
        """Overwrite a single latest-snapshot file for a surface (debug mode only).

        This avoids filling disk with a new file on every poll.
        """
        try:
            directory = Path(self.config.capture_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            safe = surface_id.replace("/", "_")
            path = directory / f"{safe}-latest.txt"
            stamp = datetime.now(timezone.utc).isoformat()
            header = f"# ysyl debug snapshot kind={kind} surface={surface_id} at={stamp}\n\n"
            path.write_text(header + text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Debug snapshot failed for %s: %s", surface_id, exc)

    def _detect_region(self, text: str) -> str:
        """The live tail of a surface — where a real limit banner appears.

        Scanning the whole scrollback yields false positives when the agent has
        merely *discussed* rate limits earlier in the session; the actual limit
        state is always at/near the bottom of the current view.
        """
        return "\n".join(text.splitlines()[-self.config.tail_lines:])

    def _observe(
        self,
        surface_id: str,
        title,
        ref,
        text: str,
        now: datetime,
        preferred_kind: str | None = None,
    ) -> None:
        """Update tracked state for one agent surface from its current text."""
        title = _str_or_none(title)
        ref = _str_or_none(ref)
        existing = self._states.get(surface_id)
        if existing is not None and existing.status == "dismissed":
            return

        region = self._detect_region(text)
        block = detect_all(
            region,
            now,
            enabled=self.config.agents,
            detectors=self._detectors,
            preferred_kind=preferred_kind,
        )

        if self.config.debug_mode:
            logger.debug(
                "Scanned %s (kind=%s): detector=%s",
                ref or surface_id,
                preferred_kind,
                block.agent_kind if block else None,
            )
            self._debug_snapshot(surface_id, text, preferred_kind)

        if block is None:
            # No limit banner visible. If we were tracking a block, it cleared.
            if existing is not None and existing.status in ("detected", "sleeping"):
                existing.status = "resumed"
                existing.preview = _preview(text)
                logger.info("Limit cleared on %s", ref or surface_id)
            return

        preview = _preview(text)

        if existing is not None and existing.status == "resumed" and existing.resumed_by_ysyl:
            # This pane is the old, blocked view. Its banner remains in
            # scrollback while the resumed session runs in a new terminal.
            return

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
        if block.agent_kind != existing.agent_kind:
            logger.info(
                "Updating agent kind for %s: %s -> %s",
                ref or surface_id,
                existing.agent_kind,
                block.agent_kind,
            )
            existing.agent_kind = block.agent_kind
            # A reclassification is effectively a fresh detection; clear stale
            # retry state so the correct detector's reset time is honored.
            existing.retry_count = 0
            existing.status = "sleeping" if existing.armed else "detected"
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

    def _prune_stale_states(self, now: datetime, live_surface_ids: set[str]) -> None:
        """Remove blocks that exceed the configured history window.

        Closed surfaces are kept in state until they age out so the dashboard
        and status command can show a full week's history.
        """
        to_remove: list[str] = []
        for surface_id, block in self._states.items():
            is_live = surface_id in live_surface_ids
            age = now - block.detected_at

            if block.status == "sleeping" and block.armed and not is_live:
                # Keep tracking a sleeping block even if the surface briefly
                # disappears from the list (cmux list can be momentarily stale).
                continue

            limit_hours = None
            if block.status == "resumed":
                limit_hours = self.config.prune_resumed_after_hours
            elif block.status == "dismissed":
                limit_hours = self.config.prune_dismissed_after_hours
            elif not is_live:
                # Other states on a closed surface age out with the resumed window.
                limit_hours = self.config.prune_resumed_after_hours

            if limit_hours and limit_hours > 0 and age >= timedelta(hours=limit_hours):
                to_remove.append(surface_id)

        for surface_id in to_remove:
            logger.debug("Pruning stale state for %s", surface_id)
            self._states.pop(surface_id, None)

    async def _poll_once(self) -> None:
        """List surfaces, update state, then wait until the next reset is due."""
        now = datetime.now(timezone.utc)
        try:
            surfaces = await self.client.list_surfaces()
        except CmuxError as exc:
            logger.warning("Failed to list surfaces: %s", exc)
            await self._idle_wait()
            return

        live_surface_ids: set[str] = set()
        watched: dict[str, SurfaceRef] = {}
        activity: dict[str, str] = {}
        kinds: dict[str, str] = {}
        for surface in surfaces:
            surface_id = getattr(surface, "surface_id", "") or ""
            if not surface_id:
                continue
            meta_agent = self._is_agent_surface(surface)
            surface_type = getattr(surface, "type", None) or "terminal"
            # cmux only labels Claude panes, so probe every terminal's content to
            # catch an unlabelled Codex/Kimi session. Non-terminals can't be agents.
            if not meta_agent and surface_type != "terminal":
                continue
            try:
                text = await self.client.read_surface_text(surface_id)
            except CmuxError as exc:
                if meta_agent:
                    logger.warning("Failed to read %s: %s", surface_id, exc)
                continue
            kind = self._infer_agent_kind(surface) if meta_agent else None
            if kind is None:
                kind = self._content_agent_kind(text)
            if kind is None and not meta_agent:
                continue  # a plain terminal, not an agent session
            live_surface_ids.add(surface_id)
            watched[surface_id] = surface
            kinds[surface_id] = kind
            existing = self._states.get(surface_id)
            if existing is not None and existing.status == "dismissed":
                continue
            current_activity = _preview(text)
            activity[surface_id] = current_activity
            previous_activity = self._activity.get(surface_id)
            if previous_activity is not None and current_activity != previous_activity:
                self._activity_changed_at[surface_id] = now
            self._observe(
                surface_id,
                getattr(surface, "title", None),
                getattr(surface, "ref", None),
                text,
                now,
                preferred_kind=kind,
            )

        self._watched = watched
        self._activity = activity
        self._kinds = kinds
        self._activity_changed_at = {
            surface_id: changed_at
            for surface_id, changed_at in self._activity_changed_at.items()
            if surface_id in activity
        }
        self._update_ledger(now, watched, activity, kinds)
        self._backfill_history(now)
        self._prune_ledger(now)
        self._prune_stale_states(now, live_surface_ids)
        self.save_state()
        self.save_ledger()

        target = self._earliest_reset(now)
        if target is not None:
            # Never park the whole loop on a single far-off reset: cap the wait so
            # we keep re-polling (catch new blocks on other surfaces, let a stale
            # or false-positive block self-clear). When the reset is near, we still
            # wait exactly until it so the resume fires promptly.
            seconds = (target - now).total_seconds()
            capped = min(seconds, self.config.max_sleep_seconds)
            wait_target = now + timedelta(seconds=max(capped, 1))
            if capped >= seconds:
                logger.info("Waiting until %s for next reset", target.isoformat())
            else:
                logger.debug(
                    "Next reset %s is far; re-polling in %ss",
                    target.isoformat(), self.config.max_sleep_seconds,
                )
            await self._wait_for(wait_target)
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

    async def _resume_surface(self, surface_id: str) -> bool:
        """Send the resume action to a blocked agent surface and verify it sent.

        Returns True if the keystroke/text was delivered. A delivery failure is
        treated as a retry-worthy error; the caller observes the surface again on
        the next poll to decide whether the limit actually cleared.
        """
        block = self._states.get(surface_id)
        if block is None:
            logger.warning("Resume requested for unknown surface %s", surface_id)
            return False

        spec = self.config.agent_resume_actions.get(
            block.agent_kind,
            f"{self.config.resume_action}:{self.config.resume_text}",
        )
        spec = spec.lower()
        if spec == "enter":
            action, detail = "enter", "return"
        elif spec.startswith("text:"):
            action, detail = "text", spec.split(":", 1)[1]
        else:
            # Should be caught by config validation; fall back to Enter.
            action, detail = "enter", "return"

        logger.info(
            "Resuming %s (%s) with %s=%r",
            block.ref or surface_id,
            block.agent_kind,
            action,
            detail,
        )

        try:
            if action == "text":
                await self.client.send_text(block.surface_id, detail + "\n")
            else:
                await self.client.send_key(block.surface_id, "return")
        except CmuxError as exc:
            block.retry_count += 1
            logger.warning("Resume failed for %s: %s", block.ref or surface_id, exc)
            self._capture(surface_id, "", reason=f"resume-delivery-error-{exc}")
            if block.retry_count >= self.config.max_retries:
                logger.warning("Dismissing %s after %d failures", surface_id, block.retry_count)
                block.status = "dismissed"
            self.save_state()
            return False

        block.status = "resumed"
        block.resumed_at = datetime.now(timezone.utc)
        block.retry_count += 1
        self.save_state()
        logger.info(
            "Resumed %s (%s), attempt %d",
            block.ref or surface_id,
            block.agent_kind,
            block.retry_count,
        )

        if self.config.resume_verify_delay_seconds > 0:
            try:
                await asyncio.sleep(self.config.resume_verify_delay_seconds)
            except asyncio.CancelledError:
                pass

        return True

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
        self._load_ledger()
        ui_settings = self.load_ui_settings()
        if "history_window" in ui_settings:
            try:
                self.config.history_window = ui_settings["history_window"]
                logger.info("UI history_window: %s", self.config.history_window)
            except ValueError as exc:
                logger.warning("Ignored invalid saved history_window: %s", exc)
        self._backfill_history(datetime.now(timezone.utc))
        self._prune_ledger(datetime.now(timezone.utc))
        self.save_ledger()
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
