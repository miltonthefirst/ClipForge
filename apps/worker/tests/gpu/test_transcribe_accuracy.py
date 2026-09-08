"""Real transcription on real hardware, scored against a known transcript.

Phase 4, exit criteria 1, 2, 4 and 5. Opt-in (`-m gpu`), because it needs an
NVIDIA device, the CUDA runtime and a downloaded Whisper model — none of which CI
has, and none of which should be faked to pretend otherwise.

**The reference is not Whisper's own output.** It is Poe's published text,
verified by ear against a public-domain LibriVox reading. A regression test
scored against your own output proves only that you are consistent, which is not
the property anyone wants. See `assets/fixtures/speech/SOURCES.md`.

If you have dropped a recording of your own voice into
`assets/fixtures/speech/custom/`, it is picked up automatically and scored too —
the pipeline will ultimately process your speech, not a 19th-century essay.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest
from clipforge.config import Settings
from clipforge.models.broker import ModelBroker
from clipforge.models.vram import probe_vram
from clipforge.models.whisper import WhisperTranscriber
from clipforge.text import word_error_rate

pytestmark = pytest.mark.gpu

SPEECH = Path(__file__).resolve().parents[2] / "assets" / "fixtures" / "speech"

# Deliberately loose. This is a *regression* gate, not an accuracy benchmark: the
# question is "did a model or settings change make this materially worse", and a
# threshold tight enough to catch a one-word difference would fail on the
# nondeterminism of beam search instead.
MAX_WER = 0.15


@dataclass(frozen=True)
class SpeechFixture:
    name: str
    audio: Path
    reference: str


def _discover() -> list[SpeechFixture]:
    fixtures: list[SpeechFixture] = []

    committed_audio = SPEECH / "librivox-poe-philosophy.mp3"
    committed_text = SPEECH / "librivox-poe-philosophy.txt"
    if committed_audio.is_file() and committed_text.is_file():
        fixtures.append(
            SpeechFixture(
                name="librivox-poe",
                audio=committed_audio,
                reference=committed_text.read_text(encoding="utf-8"),
            )
        )

    # Your own voice, if you added one. Gitignored on purpose.
    custom_text = SPEECH / "custom" / "voice.txt"
    if custom_text.is_file():
        for extension in (".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg"):
            audio = SPEECH / "custom" / f"voice{extension}"
            if audio.is_file():
                fixtures.append(
                    SpeechFixture(
                        name="custom-voice",
                        audio=audio,
                        reference=custom_text.read_text(encoding="utf-8"),
                    )
                )
                break

    return fixtures


FIXTURES = _discover()


@pytest.fixture(scope="module")
def transcriber() -> WhisperTranscriber:
    settings = Settings()
    return WhisperTranscriber(
        model=settings.whisper_model,
        compute_type=settings.whisper_compute_type,
        device=settings.whisper_device,
        broker=ModelBroker(reserve_mb=settings.vram_reserve_mb),
    )


@pytest.mark.skipif(not FIXTURES, reason="no speech fixtures present")
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_transcription_stays_within_the_error_budget(
    fixture: SpeechFixture, transcriber: WhisperTranscriber
) -> None:
    """Phase 4, exit criterion 4."""
    result = transcriber.transcribe(fixture.audio, source_id="fixture")
    hypothesis = " ".join(segment.text for segment in result.transcript.segments)

    wer = word_error_rate(fixture.reference, hypothesis)
    assert wer.rate <= MAX_WER, (
        f"{fixture.name}: {wer}\n"
        f"  reference:  {fixture.reference.strip()[:300]}\n"
        f"  hypothesis: {hypothesis.strip()[:300]}"
    )


@pytest.mark.skipif(not FIXTURES, reason="no speech fixtures present")
def test_every_segment_carries_word_level_timestamps(
    transcriber: WhisperTranscriber,
) -> None:
    """Phase 4, exit criterion 1.

    Not optional: Phase 5 snaps clip boundaries to word ends and Phase 6 builds
    karaoke captions from them. A transcript without these is unusable
    downstream.
    """
    result = transcriber.transcribe(FIXTURES[0].audio, source_id="fixture")

    assert result.transcript.segments
    for segment in result.transcript.segments:
        assert segment.words, f"segment {segment.index} has no words"
        for word in segment.words:
            assert word.end_sec >= word.start_sec
            assert word.text.strip()

    # Word timings must advance monotonically, or boundary snapping picks a cut
    # point earlier than the word it thought it was after.
    flat = [w for s in result.transcript.segments for w in (s.words or [])]
    assert all(a.start_sec <= b.start_sec for a, b in itertools.pairwise(flat))


@pytest.mark.skipif(not FIXTURES, reason="no speech fixtures present")
def test_vram_returns_to_baseline_after_the_stage(transcriber: WhisperTranscriber) -> None:
    """Phase 4, exit criterion 2.

    There is no allocator to interrogate — CTranslate2 frees on refcount drop —
    so this measures the card directly through NVML before and after.
    """
    before = probe_vram()
    if before is None:
        pytest.skip("no NVIDIA device")

    transcriber.transcribe(FIXTURES[0].audio, source_id="fixture")

    after = probe_vram()
    assert after is not None
    leaked_mb = before.free_mb - after.free_mb
    assert leaked_mb < 100, f"{leaked_mb} MiB was not released after transcription"


@pytest.mark.skipif(not FIXTURES, reason="no speech fixtures present")
def test_throughput_is_recorded_as_a_realtime_factor(
    transcriber: WhisperTranscriber,
) -> None:
    """Phase 4, exit criterion 5 — so a later model swap is comparable against a
    measurement rather than a remembered impression."""
    result = transcriber.transcribe(FIXTURES[0].audio, source_id="fixture")
    assert result.realtime_factor > 0
