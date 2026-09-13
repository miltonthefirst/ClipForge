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

    # How long a clip stays in the bucket. Enforced by a Cloud Storage lifecycle
    # rule, NOT by anything here — see firebase/storage.lifecycle.json. This
    # value exists so the worker can record `Clip.playbackExpiresAt` at upload
    # and the UI can stop offering a video the bucket has since collected. The
    # two must agree; tools/storage-lifecycle.ps1 reads this one when it applies
    # the rule, so there is one number rather than two.
    clip_retention_days: int = Field(default=5, ge=1, le=365)

    # Per-request timeout for a clip upload. Generous on purpose: clips are tens
    # of megabytes and the uplink is whatever the machine has. The library's
    # 60-second default assumes a fast connection, and when it is wrong the
    # upload dies mid-file. Nothing is lost when it does — the clip is already
    # on disk — but re-uploading costs the bandwidth again.
    upload_timeout_seconds: float = Field(default=600.0, ge=30.0)

    # The worker serves its workspace read-only so the PWA can play clips when
    # opened on this machine. Keep the host at 127.0.0.1: this is a convenience,
    # not an authenticated surface.
    local_server_enabled: bool = True
    local_server_host: str = "127.0.0.1"
    local_server_port: int = Field(default=8770, ge=1024, le=65535)

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
    # A multimodal model, for the one call per clip that looks at the picture.
    # Separate from `ollama_model` because looking and reading are not the same
    # weights: this one is several times the size, does not fit in 6 GB, and is
    # worth its minute exactly once. Set it to "" to turn looking off entirely —
    # every caller has a path that works without it.
    vision_model: str = "mistral-small3.2:latest"
    vision_frames: int = 3
    vram_reserve_mb: int = Field(default=700, ge=0)

    # ── Media ────────────────────────────────────────────────────────────────
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    render_profile: str = "default"
    video_encoder: str = "h264_nvenc"
    loudness_target_lufs: float = -14.0

    # ── Speech ───────────────────────────────────────────────────────────────
    # Kokoro-82M on onnxruntime, for the REMAKE stage's narration. Deliberately
    # not part of the default install: it is ~330 MB of weights that a worker
    # which never re-voices a clip has no use for, and the adapter imports
    # lazily so its absence costs nothing until someone asks for a voice.
    # Fetch both with `clipforge-worker fetch-voices`.
    #
    # Under `~` rather than `./`, like the local-API token and the agent log,
    # and unlike the YouTube credentials beside them. The difference is who
    # chooses the path: those are files an operator places deliberately, these
    # are weights a command downloads. A CWD-relative default breaks the moment
    # the writer and the reader disagree about the working directory — and they
    # do: `fetch-voices` is documented as `uv run --project apps/worker`, which
    # leaves the CWD at the repo root, while the worker is launched with
    # `uv run --directory apps/worker` by both tools/worker.ps1 and the agent.
    # The download would land two directories away from where the worker looks
    # and report success.
    speech_model_path: Path = Path("~/.clipforge/kokoro-v1.0.onnx")
    speech_voices_path: Path = Path("~/.clipforge/voices-v1.0.bin")
    # The voice used when a remake asks for a language but names no voice.
    # Per-language defaults live in clipforge.media.speech; this is the last
    # resort for a language that has none.
    speech_default_voice: str = "af_heart"

    # ── Publishing ───────────────────────────────────────────────────────────
    # Off by default, and that default is the point rather than caution. Nothing
    # should reach a public platform because a config file was left at its
    # factory setting; turning this on is meant to be a decision someone made
    # after reading the rights guidance. See docs/PLAN.md Phase 8.
    publishing_enabled: bool = False

    # The OAuth client downloaded from the Google Cloud console, and where the
    # resulting refresh token is kept. Both are paths on this machine: the token
    # is never written to Firestore (D7).
    youtube_client_secrets: Path = Path("./.clipforge/youtube-client.json")
    youtube_token_store: Path = Path("./.clipforge/youtube-token.enc")

    # Unlisted, because publishing something to the world by accident is not
    # recoverable the way an unlisted upload is — the link may already have been
    # scraped by the time anyone notices.
    youtube_default_privacy: Literal["private", "unlisted", "public"] = "unlisted"

    # The port the local OAuth redirect listens on during `youtube-auth`. It must
    # match a redirect URI registered on the OAuth client, which is why it is
    # configurable rather than picked at random.
    youtube_auth_port: int = Field(default=8766, ge=1024, le=65535)

    # ── Local control API ────────────────────────────────────────────────────
    # A loopback-only surface the desktop app uses to hand the worker a YouTube
    # client secret, so it can be typed into a form instead of a terminal and
    # still never leave the machine. Bound to 127.0.0.1 unconditionally — unlike
    # the file server's host, this one is not configurable, because there is no
    # version of exposing it that is a supported mode.
    # See docs/adr/0011-local-control-api.md.
    local_api_enabled: bool = True
    local_api_port: int = Field(default=8767, ge=1024, le=65535)
    # In the home directory, not the working directory. The worker runs from the
    # repo root and the desktop app from its own build output, so a relative path
    # would resolve to two different files and the pairing would silently never
    # match. `~` is expanded at use.
    local_api_token_file: Path = Path("~/.clipforge/local-api-token")

    # ── The agent ────────────────────────────────────────────────────────────
    # A supervisor that starts with Windows and does what `agents/{workerId}`
    # says, so the worker can be started from a phone rather than only from this
    # machine. See docs/adr/0012-machine-agent.md.
    #
    # The two intervals are separate because they cost different things. The tick
    # is local — it notices the child process dying, and nothing external will
    # tell it — so it is cheap and frequent. The heartbeat is a Firestore write
    # and a read, so it is neither.
    agent_tick_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    agent_heartbeat_seconds: int = Field(default=60, ge=10)
    # How many lines of the worker's output travel with the report. Bounded
    # because this document is written every heartbeat and Firestore caps a
    # document at 1 MiB — and because the useful part of a failed start is its
    # last few lines, not its first thousand.
    agent_log_lines: int = Field(default=20, ge=0, le=200)
    # How many times a worker that will not stay up is restarted before the
    # agent stops trying. Retrying forever turns one broken install into an
    # unbounded log of identical failures; stopping leaves the reason on screen.
    agent_restart_limit: int = Field(default=5, ge=0)
    agent_restart_backoff_seconds: int = Field(default=15, ge=1)
    # How long a clean stop is waited for before the agent terminates a worker it
    # started. Matches the desktop shell's grace period, and both exist because
    # the worker's own shutdown asks stages to check-point first.
    agent_shutdown_grace_seconds: int = Field(default=15, ge=1)
    # Where the agent writes its own log. Started by Task Scheduler it has no
    # console, so without a file a failure to start is invisible — which is the
    # one failure it most needs to be able to explain.
    agent_log_file: Path = Path("~/.clipforge/agent.log")

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
                "CLIPFORGE_BLOB_STORE=firebase requires CLIPFORGE_FIREBASE_STORAGE_BUCKET, "
                "the bucket name from the Firebase console (Storage → Files), which looks "
                "like `your-project.firebasestorage.app`."
            )
        return self

    @model_validator(mode="after")
    def _require_credentials_when_publishing(self) -> Settings:
        """Publishing enabled with no OAuth client is a configuration that can
        only fail, and it would fail after a human had already approved a clip
        and expected it to go out. Caught at startup instead."""
        # `Path("")` normalises to `Path(".")`, so an unset env var arrives here
        # as the current directory rather than as something falsy. Checked by
        # value, not truthiness — the truthy version silently passed.
        if self.publishing_enabled and str(self.youtube_client_secrets) in ("", "."):
            raise ValueError(
                "CLIPFORGE_PUBLISHING_ENABLED=true requires "
                "CLIPFORGE_YOUTUBE_CLIENT_SECRETS to point at a Google OAuth client file"
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
