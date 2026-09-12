"""The Python half of the contracts check.

These tests deliberately read ``schemas/clipforge.json`` and assert the generated
Pydantic models against it, rather than restating the schema in Python. A test
that hardcoded the field names would pass happily while the generated models went
stale â€” which is the exact failure this package exists to prevent.

The TypeScript half lives in apps/web/src/app/contracts.spec.ts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clipforge_contracts
import pytest
from clipforge_contracts import (
    Clip,
    ClipLocation,
    ClipPreview,
    GpuInfo,
    Job,
    JobStatus,
    JobType,
    Lane,
    LlmClipResponse,
    ReviewState,
    Stage,
    StageName,
    StageStatus,
    SubScores,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerStatus,
)
from pydantic import ValidationError

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "packages" / "contracts" / "schemas" / "clipforge.json"
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _now() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _minimal_job(**overrides: Any) -> Job:
    defaults: dict[str, Any] = {
        "id": "job-1",
        "uid": "user-1",
        "type": JobType.ECHO,
        "status": JobStatus.QUEUED,
        "stages": [Stage(name=StageName.ECHO_ONE, lane=Lane.CPU, status=StageStatus.PENDING)],
        "attempts": 0,
        "max_attempts": 3,
        "created_at": _now(),
        "updated_at": _now(),
    }
    return Job(**{**defaults, **overrides})


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The generated models must actually cover the schema.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_every_schema_definition_is_exported(schema: dict[str, Any]) -> None:
    """A definition added to the schema but not regenerated would fail here."""
    missing = [name for name in schema["definitions"] if not hasattr(clipforge_contracts, name)]
    assert missing == [], f"schema definitions with no generated model: {missing}"


@pytest.mark.unit
def test_enum_members_match_the_schema(schema: dict[str, Any]) -> None:
    """Every string enum in the schema must have identical members in Python."""
    checked = 0
    for name, definition in schema["definitions"].items():
        if definition.get("type") != "string" or "enum" not in definition:
            continue
        generated = getattr(clipforge_contracts, name)
        assert sorted(m.value for m in generated) == sorted(definition["enum"]), name
        checked += 1
    assert checked >= 8, "expected the schema to define several string enums"


@pytest.mark.unit
def test_required_fields_match_the_schema(schema: dict[str, Any]) -> None:
    """Guards against a `required` edit that never reached the generated models."""
    for model, name in ((Job, "Job"), (Stage, "Stage"), (WorkerHeartbeat, "WorkerHeartbeat")):
        wire_required = set(schema["definitions"][name]["required"])
        generated_required = {
            (field.alias or field_name)
            for field_name, field in model.model_fields.items()
            if field.is_required()
        }
        assert generated_required == wire_required, name


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Wire format. Python is snake_case; Firestore is camelCase.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_wire_form_is_camel_case() -> None:
    wire = _minimal_job().model_dump(by_alias=True, mode="json")
    assert "maxAttempts" in wire
    assert "max_attempts" not in wire
    assert "leaseExpiresAt" in wire


@pytest.mark.unit
def test_round_trip_through_wire_form_is_lossless() -> None:
    job = _minimal_job(source_id="src-1", worker_id="worker-1")
    wire = job.model_dump(by_alias=True, mode="json")
    assert Job.model_validate(wire) == job


@pytest.mark.unit
def test_models_are_constructible_by_python_field_name() -> None:
    """`populate_by_name` â€” without it, worker code would have to say `maxAttempts`."""
    job = _minimal_job()
    assert job.max_attempts == 3


@pytest.mark.unit
def test_timestamps_are_timezone_aware() -> None:
    """Naive datetimes compare incorrectly against Firestore values, so they are rejected."""
    with pytest.raises(ValidationError):
        _minimal_job(created_at=datetime(2026, 9, 8, 12, 0, 0))


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Constraints. These are the ones that would corrupt the pipeline if unenforced.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_unknown_fields_are_rejected() -> None:
    """extra=forbid. A typo'd field name must fail loudly, not vanish silently."""
    wire = _minimal_job().model_dump(by_alias=True, mode="json")
    wire["totallyNotAField"] = 1
    with pytest.raises(ValidationError):
        Job.model_validate(wire)


@pytest.mark.unit
def test_a_job_must_have_at_least_one_stage() -> None:
    with pytest.raises(ValidationError):
        _minimal_job(stages=[])


@pytest.mark.unit
def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _minimal_job(max_attempts=0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "over_limit"),
    [
        ("hook", 26),
        ("curiosity", 21),
        ("standalone", 21),
        ("emotion", 16),
        ("pacing", 11),
        ("shareability", 11),
    ],
)
def test_sub_scores_are_bounded_by_the_rubric(field: str, over_limit: int) -> None:
    values = {
        "hook": 1,
        "curiosity": 1,
        "standalone": 1,
        "emotion": 1,
        "pacing": 1,
        "shareability": 1,
    }
    values[field] = over_limit
    with pytest.raises(ValidationError):
        SubScores(**values)


@pytest.mark.unit
def test_the_rubric_maxima_sum_to_one_hundred(schema: dict[str, Any]) -> None:
    """The scoring rubric in docs/PLAN.md totals 100. If it stops doing so, the
    `total` field's 0-100 bound becomes unreachable or exceedable."""
    props = schema["definitions"]["SubScores"]["properties"]
    assert sum(p["maximum"] for p in props.values()) == 100


@pytest.mark.unit
def test_llm_response_carries_no_total() -> None:
    """Decision D5: the model returns sub-scores, Python computes the total. A
    `total` on the LLM proposal would mean the model was doing the arithmetic."""
    proposal_props = LlmClipResponse.model_json_schema()["$defs"]["LlmClipProposal"]["properties"]
    assert "total" not in proposal_props
    assert "subScores" in proposal_props


@pytest.mark.unit
def test_llm_response_parses_a_realistic_model_reply() -> None:
    reply = {
        "clips": [
            {
                "startSec": 142.4,
                "endSec": 181.7,
                "subScores": {
                    "hook": 22,
                    "curiosity": 18,
                    "standalone": 19,
                    "emotion": 11,
                    "pacing": 8,
                    "shareability": 9,
                },
                "hook": "Most developers don't realise...",
                "reason": "Strong hook and a clear payoff inside forty seconds.",
            }
        ]
    }
    parsed = LlmClipResponse.model_validate(reply)
    assert len(parsed.clips) == 1
    assert parsed.clips[0].sub_scores.hook == 22


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Heartbeat.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.unit
def test_heartbeat_round_trips_with_gpu_detail() -> None:
    heartbeat = WorkerHeartbeat(
        worker_id="worker-1",
        uid="user-1",
        status=WorkerStatus.ONLINE,
        capabilities=WorkerCapabilities(whisper=True, llm=True, render=True, publish=False),
        gpu=GpuInfo(name="NVIDIA GeForce RTX 3050", vram_total_mb=6144, vram_free_mb=2386),
        version="0.0.1",
        last_seen_at=_now(),
    )
    wire = heartbeat.model_dump(by_alias=True, mode="json")
    assert wire["gpu"]["vramTotalMb"] == 6144
    assert WorkerHeartbeat.model_validate(wire) == heartbeat


@pytest.mark.unit
def test_heartbeat_gpu_is_optional_for_a_cpu_only_worker() -> None:
    heartbeat = WorkerHeartbeat(
        worker_id="worker-1",
        uid="user-1",
        status=WorkerStatus.ONLINE,
        capabilities=WorkerCapabilities(whisper=False, llm=False, render=True, publish=False),
        version="0.0.1",
        last_seen_at=_now(),
    )
    assert heartbeat.gpu is None


# ─────────────────────────────────────────────────────────────────────────────
# The free-tier storage posture. See docs/adr/0009-spark-tier-local-artefacts.md.
# ─────────────────────────────────────────────────────────────────────────────


def _local_clip(**overrides: Any) -> Clip:
    defaults: dict[str, Any] = {
        "id": "clip-1",
        "uid": "user-1",
        "candidate_id": "cand-1",
        "location": ClipLocation.LOCAL,
        "local_path": "P:/workspace/clips/clip-1.mp4",
        "review": ReviewState.PENDING,
        "created_at": _now(),
    }
    return Clip(**{**defaults, **overrides})


@pytest.mark.unit
def test_a_clip_always_knows_where_its_file_is() -> None:
    """`localPath` is required even once a remote copy exists: the worker still
    needs the file to publish (decision D7)."""
    with pytest.raises(ValidationError):
        _local_clip(local_path=None)


@pytest.mark.unit
def test_a_clip_needs_no_remote_url() -> None:
    """The free-tier default. Cloud Storage requires Blaze, so on Spark there is
    no bucket at all and a clip is complete without one."""
    clip = _local_clip()
    assert clip.playback_url is None
    assert clip.storage_path is None
    assert clip.location is ClipLocation.LOCAL


@pytest.mark.unit
def test_a_clip_can_carry_both_locations_at_once() -> None:
    """What the Blaze upgrade populates. Both states are first-class in the
    schema now, so enabling Storage fills a field rather than migrating a
    model."""
    clip = _local_clip(
        location=ClipLocation.REMOTE,
        playback_url="https://storage.example/clip-1.mp4",
        storage_path="users/user-1/clips/clip-1.mp4",
    )
    wire = clip.model_dump(by_alias=True, mode="json")
    assert wire["localPath"]
    assert wire["playbackUrl"]
    assert Clip.model_validate(wire) == clip


@pytest.mark.unit
def test_a_preview_stays_well_inside_the_firestore_document_limit() -> None:
    """The poster is what a phone sees when the file is unreachable. Firestore
    caps a document at 1 MiB; this asserts the budget the render stage must hit."""
    poster = "A" * 60_000
    preview = ClipPreview(
        clip_id="clip-1",
        poster_base64=poster,
        filmstrip_base64=poster,
        width_px=1080,
        height_px=1920,
        created_at=_now(),
    )
    encoded = json.dumps(preview.model_dump(by_alias=True, mode="json")).encode()
    assert len(encoded) < 1_048_576 // 2, "a preview should sit well under half the 1 MiB limit"
