"""Fixtures for the emulator-backed tier.

These tests need the Firestore emulator and nothing else — no GPU, no network, no
real Firebase project, no billing. Run them with::

    firebase emulators:exec --project demo-clipforge --only firestore \
        "uv run --project apps/worker pytest -m integration"

If the emulator is not reachable the whole tier skips with an explanatory
message rather than failing, so `pytest` on a fresh clone does something sensible.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
from clipforge.config import Settings
from clipforge.store.firestore import JobStore, WorkerStore, firestore_client
from google.cloud import firestore

# A `demo-` prefixed project puts the emulator in fully offline mode. The same
# id is used by the rules tests in firebase/tests.
PROJECT_ID = "demo-clipforge"
EMULATOR_HOST = "127.0.0.1:8080"


def _emulator_running() -> bool:
    host, _, port = EMULATOR_HOST.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def settings() -> Settings:
    if not _emulator_running():
        pytest.skip(
            f"Firestore emulator not reachable at {EMULATOR_HOST}. Start it with:\n"
            f"  firebase emulators:exec --project {PROJECT_ID} --only firestore "
            f'"uv run --project apps/worker pytest -m integration"'
        )
    return Settings(
        firebase_project_id=PROJECT_ID,
        use_emulators=True,
        firestore_emulator_host=EMULATOR_HOST,
        worker_id="worker-a",
        lease_seconds=90,
        max_attempts=3,
    )


@pytest.fixture(scope="session")
def client(settings: Settings) -> firestore.Client:
    return firestore_client(settings)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """Wipe the emulator between tests, when there is one.

    Deliberately does NOT depend on the `settings` fixture. Doing so would make
    every test in this tier require the emulator, including the ones that only
    need ffmpeg — the render tests would skip on a machine that could run them
    perfectly well. Tests that genuinely need Firestore request a store fixture
    and skip through `settings` on their own.

    The emulator's own REST endpoint is used rather than deleting documents one
    by one: it is atomic, and it cannot leave a half-cleared collection behind
    when a test fails mid-way.
    """
    if not _emulator_running():
        yield
        return
    _wipe()
    yield
    _wipe()


def _wipe() -> None:
    httpx.delete(
        f"http://{EMULATOR_HOST}/emulator/v1/projects/{PROJECT_ID}/databases/(default)/documents",
        timeout=10.0,
    )


@pytest.fixture
def jobs(client: firestore.Client, settings: Settings) -> JobStore:
    return JobStore(client, settings)


@pytest.fixture
def workers(client: firestore.Client, settings: Settings) -> WorkerStore:
    return WorkerStore(client, settings)


@pytest.fixture
def other_worker(client: firestore.Client, settings: Settings) -> JobStore:
    """A second, independent worker competing for the same queue."""
    return JobStore(client, settings.model_copy(update={"worker_id": "worker-b"}))
