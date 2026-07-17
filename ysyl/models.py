"""Shared data models for YouSnoozeYouLose."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AgentKind(StrEnum):
    """Known agent kinds that the daemon can detect and resume."""

    CLAUDE = "claude"
    KIMI = "kimi"


class BlockStatus(StrEnum):
    """Lifecycle status of a detected block."""

    DETECTED = "detected"
    SLEEPING = "sleeping"
    RESUMED = "resumed"
    DISMISSED = "dismissed"


class SurfaceRef(BaseModel):
    """Reference to a cmux surface (pane/tab).

    Real cmux ``surface.list`` items key the surface by ``id`` and also expose a
    short ``ref`` (e.g. ``surface:6``), a ``type`` (``terminal``/``browser``),
    and, for wrapped agents, a ``resume_binding`` whose ``kind`` identifies the
    agent (``claude``/``kimi``). We surface ``resume_kind`` so the daemon can
    reliably tell agent panes from plain terminals.
    """

    surface_id: str = Field(..., description="Opaque cmux surface identifier (the cmux `id`).")
    ref: str | None = Field(default=None, description="Short surface ref, e.g. 'surface:6'.")
    type: str | None = Field(default=None, description="Surface type: 'terminal' or 'browser'.")
    pane_ref: str | None = Field(default=None, description="Pane reference within cmux.")
    workspace_ref: str | None = Field(default=None, description="Workspace reference.")
    window_ref: str | None = Field(default=None, description="Window reference.")
    title: str | None = Field(default=None, description="Surface title.")
    initial_command: str | None = Field(default=None, description="Command that started the pane.")
    resume_kind: str | None = Field(
        default=None,
        description="Agent kind from cmux resume_binding (e.g. 'claude'), if any.",
    )


class BlockState(BaseModel):
    """Persisted state of a detected agent block on a surface."""

    surface_id: str = Field(..., description="Surface where the block was detected.")
    agent_kind: Literal["claude", "kimi"] = Field(..., description="Agent kind detected.")
    detected_at: datetime = Field(..., description="UTC timestamp when the block was first detected.")
    reset_at: datetime | None = Field(default=None, description="UTC timestamp when the quota is expected to reset.")
    retry_count: int = Field(default=0, ge=0, description="Number of resume attempts after the block.")
    status: Literal["detected", "sleeping", "resumed", "dismissed"] = Field(default="detected")
    armed: bool = Field(
        default=True,
        description="Whether this surface is allowed to be auto-resumed. Toggled from the UI.",
    )
    title: str | None = Field(default=None, description="Last-seen surface title, for display.")
    ref: str | None = Field(default=None, description="Last-seen surface ref, for display.")
    preview: str | None = Field(default=None, description="Short tail of surface text, for display.")

    def to_state_dict(self) -> dict:
        """Serialize to a JSON-friendly dict with ISO timestamps."""
        return {
            "surface_id": self.surface_id,
            "agent_kind": self.agent_kind,
            "detected_at": self.detected_at.isoformat(),
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "retry_count": self.retry_count,
            "status": self.status,
            "armed": self.armed,
            "title": self.title,
            "ref": self.ref,
            "preview": self.preview,
        }

    @classmethod
    def from_state_dict(cls, data: dict) -> BlockState:
        """Deserialize from a state dict with ISO timestamps."""
        return cls(
            surface_id=data["surface_id"],
            agent_kind=data["agent_kind"],
            detected_at=datetime.fromisoformat(data["detected_at"]),
            reset_at=datetime.fromisoformat(data["reset_at"]) if data.get("reset_at") else None,
            retry_count=data.get("retry_count", 0),
            status=data.get("status", "detected"),
            armed=data.get("armed", True),
            title=data.get("title"),
            ref=data.get("ref"),
            preview=data.get("preview"),
        )
