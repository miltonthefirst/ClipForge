"""Worker configuration, loaded from the environment and `.env`.

Every Firebase identifier — project id, bucket, emulator hosts — arrives here and
nowhere else. Nothing in the codebase may hardcode one. That rule was adopted in
Phase 0, before any Firebase work existed, and it is what let ClipForge move
Firebase projects during Phase 1 as a one-line change.
See docs/adr/0004-dedicated-firebase-project.md.
"""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repository root, found relative to this file: apps/worker/clipforge/config.py
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Worker settings. Field names map to ``CLIPFORGE_``-prefixed env vars."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_",
        env_file=(_REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Firebase ─────────────────────────────────────────────────────────────
    firebase_project_id: str = "demo-clipforge"
    firebase_storage_bucket: str = ""
    google_application_credentials: str = ""

    # When true the worker talks to local emulators and needs no real project,
    # no credentials and no billing. This is the development default on purpose:
    # routine work must not touch — or cost anything on — the real project.
    use_emulators: bool = True
    firestore_emulator_host: str = "127.0.0.1:8080"
    storage_emulator_host: str = "127.0.0.1:9199"
    auth_emulator_host: str = "127.0.0.1:9099"

    # ── Identity and scheduling ──────────────────────────────────────────────
    worker_id: str = ""
    lease_seconds: int = Field(default=90, ge=5)
    heartbeat_seconds: int = Field(default=30, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    reaper_interval_seconds: int = Field(default=60, ge=5)

    # The GPU lane must stay at 1: Whisper and the LLM cannot be co-resident in
    # 6 GB of VRAM. See docs/PLAN.md §2.1.
    gpu_lane_depth: int = Field(default=1, ge=1, le=1)
    cpu_lane_depth: int = Field(default=3, ge=1)

    # ── Workspace ────────────────────────────────────────────────────────────
    workspace_dir: Path = Path("./workspace")
    workspace_max_gb: int = Field(default=60, ge=1)

    # ── Models ───────────────────────────────────────────────────────────────
    whisper_model: str = "large-v3-turbo"
    whisper_compute_type: str = "int8_float16"
    whisper_device: str = "cuda"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_num_ctx: int = 16384
    vram_reserve_mb: int = Field(default=700, ge=0)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "console"

    @model_validator(mode="after")
    def _default_worker_id(self) -> Settings:
        """A blank worker id becomes the hostname.

        Worker ids appear in heartbeats and in every job's `workerId`, so
        "which machine was that?" must be answerable from the document alone.
        A random id would be useless there.
        """
        if not self.worker_id:
            object.__setattr__(self, "worker_id", socket.gethostname().lower())
        return self

    @model_validator(mode="after")
    def _require_credentials_off_emulator(self) -> Settings:
        """Fail fast rather than at the first Firestore call.

        Without this, a worker pointed at a real project with no credentials
        starts happily, advertises itself as ONLINE, and then fails on its first
        write — by which time it has already told the PWA it is healthy.
        """
        if not self.use_emulators and not self.google_application_credentials:
            raise ValueError(
                "CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS is required when "
                "CLIPFORGE_USE_EMULATORS is false"
            )
        return self

    @property
    def lease_timedelta_seconds(self) -> int:
        return self.lease_seconds

    def heartbeat_fits_in_lease(self) -> bool:
        """A heartbeat interval at or above the lease length means the lease
        always lapses before it is renewed, and every job would be reaped
        mid-flight. Checked rather than assumed because the two are configured
        independently."""
        return self.heartbeat_seconds * 2 <= self.lease_seconds


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so `.env` is read once."""
    return Settings()
