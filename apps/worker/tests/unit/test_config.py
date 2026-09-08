"""Configuration, and the misconfigurations worth refusing at startup.

Each of these would otherwise surface much later and much less clearly — after a
render has already cost minutes of GPU time, or after the worker has already told
the PWA it is healthy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.config import Settings
from pydantic import ValidationError


@pytest.mark.unit
def test_the_defaults_run_against_the_emulator() -> None:
    """Routine development must not touch — or cost anything on — a real project."""
    settings = Settings()
    assert settings.use_emulators is True
    assert settings.blob_store == "local"


@pytest.mark.unit
def test_a_blank_worker_id_becomes_the_hostname() -> None:
    """Worker ids appear in every job's `workerId`, so "which machine was that?"
    must be answerable from the document alone."""
    assert Settings(worker_id="").worker_id


@pytest.mark.unit
def test_a_real_project_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        Settings(use_emulators=False, google_application_credentials="")


# ─────────────────────────────────────────────────────────────────────────────
# The free-tier storage posture. See docs/adr/0009-spark-tier-local-artefacts.md.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_local_blob_store_needs_no_bucket() -> None:
    """The whole point: ClipForge runs with no Blaze plan and no bucket."""
    assert Settings(blob_store="local", firebase_storage_bucket="").blob_store == "local"


@pytest.mark.unit
def test_the_firebase_blob_store_without_a_bucket_is_refused() -> None:
    """Otherwise this fails at the first upload — after the render has already
    cost minutes of GPU time."""
    with pytest.raises(ValidationError, match="requires CLIPFORGE_FIREBASE_STORAGE_BUCKET"):
        Settings(blob_store="firebase", firebase_storage_bucket="")


@pytest.mark.unit
def test_the_firebase_blob_store_with_a_bucket_is_accepted() -> None:
    """The Blaze upgrade, in one setting."""
    settings = Settings(
        blob_store="firebase", firebase_storage_bucket="bytepic-clipforge.appspot.com"
    )
    assert settings.blob_store == "firebase"


@pytest.mark.unit
def test_an_unknown_blob_store_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(blob_store="s3")


@pytest.mark.unit
def test_the_local_server_origin_is_what_the_pwa_probes() -> None:
    settings = Settings(local_server_host="127.0.0.1", local_server_port=8765)
    assert settings.local_server_origin == "http://127.0.0.1:8765"


@pytest.mark.unit
def test_the_local_server_defaults_to_loopback() -> None:
    """It serves the workspace read-only with no authentication, so binding it
    anywhere else would expose those files to the whole network."""
    assert Settings().local_server_host == "127.0.0.1"


# ─────────────────────────────────────────────────────────────────────────────
# Scheduling
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_heartbeat_slower_than_the_lease_is_detected() -> None:
    """They are configured independently, so this combination is reachable — and
    it would have every job reaped mid-flight."""
    assert Settings(heartbeat_seconds=30, lease_seconds=90).heartbeat_fits_in_lease()
    assert not Settings(heartbeat_seconds=60, lease_seconds=90).heartbeat_fits_in_lease()


@pytest.mark.unit
def test_the_gpu_lane_cannot_be_widened() -> None:
    """Whisper and the LLM cannot be co-resident in 6 GB. Depth 1 is the whole
    reason the architecture looks the way it does (docs/PLAN.md 2.1)."""
    with pytest.raises(ValidationError):
        Settings(gpu_lane_depth=2)


# ─────────────────────────────────────────────────────────────────────────────
# Publishing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_publishing_is_off_by_default() -> None:
    """The default is the point, not caution. Nothing should reach a public
    platform because a config file was left at its factory setting."""
    assert Settings().publishing_enabled is False


@pytest.mark.unit
def test_the_default_privacy_is_unlisted() -> None:
    """Publishing to the world by accident is not recoverable the way an
    unlisted upload is — the link may already have been scraped."""
    assert Settings().youtube_default_privacy == "unlisted"


@pytest.mark.unit
def test_an_invented_privacy_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(youtube_default_privacy="everyone")


@pytest.mark.unit
def test_publishing_enabled_without_client_secrets_fails_at_startup() -> None:
    """Otherwise the failure lands after a human approved a clip and expected
    it to go out."""
    with pytest.raises(ValidationError, match="CLIPFORGE_YOUTUBE_CLIENT_SECRETS"):
        # `Path("")` normalises to `Path(".")`, which is what an unset env var
        # actually produces — and what the truthiness check used to let through.
        Settings(publishing_enabled=True, youtube_client_secrets=Path())


@pytest.mark.unit
def test_a_configured_client_secrets_path_satisfies_the_check() -> None:
    """The path need not exist yet — `doctor` checks that. What the validator
    catches is the configuration that names nothing at all."""
    assert Settings(
        publishing_enabled=True, youtube_client_secrets=Path("./.clipforge/client.json")
    ).publishing_enabled
