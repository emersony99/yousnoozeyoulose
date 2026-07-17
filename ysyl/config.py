"""Configuration loading for YouSnoozeYouLose."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Common macOS install location, used as a last resort.
_CMUX_FALLBACK_PATH = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def _resolve_cmux_bin() -> str:
    """Find the cmux CLI without relying on the daemon's PATH.

    ``ysyl run`` may be launched from a shell whose PATH lacks the cmux bin dir,
    so we prefer the env vars cmux sets inside its own terminals, then PATH, then
    the known install path.
    """
    for env in ("CMUX_BUNDLED_CLI_PATH", "CMUX_CLAUDE_HOOK_CMUX_BIN"):
        candidate = os.environ.get(env)
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("cmux")
    if found:
        return found
    if os.path.isfile(_CMUX_FALLBACK_PATH) and os.access(_CMUX_FALLBACK_PATH, os.X_OK):
        return _CMUX_FALLBACK_PATH
    return "cmux"


class Settings(BaseSettings):
    """Application settings loaded from environment and optional TOML files.

    Priority (highest to lowest):
    1. Environment variables prefixed with ``YSYL_``.
    2. ``.ysyl.toml`` in the current working directory.
    3. ``.ysyl.toml`` in the user's home directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="YSYL_",
        extra="ignore",
        toml_file=[Path.cwd() / ".ysyl.toml", Path.home() / ".ysyl.toml"],
    )

    poll_interval_seconds: int = Field(default=10, ge=1, description="Seconds between surface polls.")
    sleep_tick_seconds: int = Field(default=30, ge=1, description="Scheduler sleep chunk size.")
    cmux_bin: str = Field(
        default_factory=_resolve_cmux_bin,
        description="Path to the cmux CLI binary. Auto-resolved from cmux env/PATH if unset.",
    )
    cmux_retries: int = Field(
        default=3, ge=1, description="Attempts per cmux RPC before failing (retries transient socket drops)."
    )
    cmux_password: str = Field(
        default_factory=lambda: os.environ.get("CMUX_SOCKET_PASSWORD", ""),
        description="Socket password (only needed if cmux socketControlMode='password' and "
        "ysyl runs outside a cmux surface). Defaults to the CMUX_SOCKET_PASSWORD env var.",
    )
    state_file: str = Field(default="~/.ysyl/state.json", description="Path to persistent state file.")
    log_level: str = Field(default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR).")
    agents: dict[str, bool] = Field(
        default_factory=lambda: {"claude": True, "kimi": True},
        description="Map of agent name to detection enabled flag.",
    )

    # --- Resume behaviour -------------------------------------------------
    auto_arm: bool = Field(
        default=True,
        description="Whether newly-detected agent blocks are armed for auto-resume by default.",
    )
    resume_action: str = Field(
        default="enter",
        description="How to resume a blocked agent: 'enter' (send Return) or 'text'.",
    )
    resume_text: str = Field(
        default="continue",
        description="Text sent when resume_action='text' (a trailing newline is added).",
    )
    max_retries: int = Field(
        default=5, ge=1, description="Resume attempts before a surface is auto-dismissed."
    )
    agent_title_patterns: list[str] = Field(
        default_factory=lambda: ["claude", "kimi"],
        description="Substrings (case-insensitive) that mark a surface title/command as an agent.",
    )

    # --- Detection tuning -------------------------------------------------
    tail_lines: int = Field(
        default=30,
        ge=1,
        description="Only the last N lines of a surface are scanned, so a limit "
        "banner in the live region is detected but stale scrollback is ignored.",
    )
    detector_banner_patterns: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Extra regex banner/indicator patterns per agent kind, appended to the built-ins.",
    )
    capture_dir: str = Field(
        default="~/.ysyl/captures",
        description="Directory where suspected-limit surface text is captured for tuning.",
    )
    capture_on_detect: bool = Field(
        default=False,
        description="If true, capture surface text on every detection (not just unparsed resets).",
    )

    # --- Web UI -----------------------------------------------------------
    ui_enabled: bool = Field(default=True, description="Serve the local dashboard while running.")
    ui_host: str = Field(default="127.0.0.1", description="Host to bind the dashboard (localhost only).")
    ui_port: int = Field(default=8765, ge=1, le=65535, description="Port for the dashboard.")

    @field_validator("resume_action", mode="before")
    @classmethod
    def _validate_resume_action(cls, value: str) -> str:
        normalized = str(value).lower()
        if normalized not in {"enter", "text"}:
            raise ValueError(f"resume_action must be 'enter' or 'text', got {value!r}")
        return normalized

    @field_validator("capture_dir", mode="before")
    @classmethod
    def _expand_capture_dir(cls, value: str) -> str:
        return str(Path(value).expanduser())

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = str(value).upper()
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {value!r}")
        return normalized

    @field_validator("state_file", mode="before")
    @classmethod
    def _expand_state_file(cls, value: str) -> str:
        return str(Path(value).expanduser())

    @classmethod
    def settings_customise_sources(
        cls,
        settings_sources: Any,
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple:
        # pydantic-settings 2.x exposes TomlConfigSettingsSource via the class method pattern.
        try:
            from pydantic_settings import TomlConfigSettingsSource

            return (
                init_settings,
                env_settings,
                TomlConfigSettingsSource(cls),
                file_secret_settings,
            )
        except Exception:  # pragma: no cover - fallback when TOML source unavailable
            return (
                init_settings,
                env_settings,
                file_secret_settings,
            )

    def setup_logging(self) -> None:
        """Configure the root logger."""
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
