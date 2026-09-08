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
from typing import Literal

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

    # ── Artefact storage ─────────────────────────────────────────────────────
    # The one switch the Blaze upgrade flips. `local` writes clips to the
    # workspace and never uploads; `firebase` also uploads and populates
    # Clip.playbackUrl. Cloud Storage has required Blaze since February 2026, so
    # `local` is the only option that works on the free tier.
    # See docs/adr/0009-spark-tier-local-artefacts.md.
    blob_store: Literal["local", "firebase"] = "local"

    # The worker serves its workspace read-only so the PWA can play clips when
    # opened on this machine. Keep the host at 127.0.0.1: this is a convenience,
    # not an authenticated surface.
    local_server_enabled: bool = True
    local_server_host: str = "127.0.0.1"
    local_server_port: int = Field(default=8765, ge=1024, le=65535)

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

    # ── Workspace and ingestion ──────────────────────────────────────────────
    workspace_dir: Path = Path("./workspace")
    workspace_max_gb: int = Field(default=60, ge=1)

    # Refuse a video longer than this rather than discovering the problem after a
    # 2 GB download and a twenty-minute transcription. Four hours by default:
    # generous enough not to be a nuisance, short enough to catch a mistake.
    max_source_duration_sec: float = Field(default=4 * 60 * 60, gt=0)

    # ── Models ───────────────────────────────────────────────────────────────
    whisper_model: str = "large-v3-turbo"
    whisper_compute_type: str = "int8_float16"
    whisper_device: str = "cuda"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_num_ctx: int = 16384
    vram_reserve_mb: int = Field(default=700, ge=0)

    # ── Media ────────────────────────────────────────────────────────────────
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    render_profile: str = "default"
    video_encoder: str = "h264_nvenc"
    loudness_target_lufs: float = -14.0

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

    @model_validator(mode="after")
    def _require_bucket_for_firebase_blob_store(self) -> Settings:
        """Refuse a configuration that cannot possibly work.

        Selecting the `firebase` blob store without a bucket would fail at the
        first upload — after a render has already cost minutes of GPU time.
        """
        if self.blob_store == "firebase" and not self.firebase_storage_bucket:
            raise ValueError(
                "CLIPFORGE_BLOB_STORE=firebase requires CLIPFORGE_FIREBASE_STORAGE_BUCKET. "
                "On the Spark free tier there is no bucket: use CLIPFORGE_BLOB_STORE=local."
            )
        return self

    @property
    def local_server_origin(self) -> str:
        """Where the PWA looks for locally-stored clips."""
        return f"http://{self.local_server_host}:{self.local_server_port}"

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
