"""Reading a reviewer's note with the real local model.

The GPU tier, because it needs Ollama and the pulled model. It is the only
place the shape of `LlmRemakeNote` can actually be judged: the schema is the way
it is because two simpler shapes were tried against qwen3.5:4b and each failed
in its own direction, and a test with a scripted model would have been perfectly
happy with all three.

* **Nullable optional fields** — the model writes a summary saying it chose
  TRACK and then emits null for the mode. Null always satisfies the schema, so
  it is always the easy path.
* **Every field required** — the model fills all of them, inventing a crop
  anchor and a language for a note about timing. Worse than the first failure,
  because a remake that silently reframes a clip nobody complained about is
  harder to notice than one that does nothing.
* **Topics first, then required fields** — what is here. The model declares what
  the note is about before answering, and anything outside those topics is
  discarded by `apply_interpretation`.

`test_a_note_about_one_thing_changes_only_that_thing` is the one that matters.
The rest could pass with a model that guesses well; that one fails if the
scoping gate is removed.
"""

from __future__ import annotations

import pytest
from clipforge.analysis.remake import apply_interpretation, interpret_note, translate
from clipforge.config import Settings
from clipforge.models.ollama import OllamaClient
from clipforge_contracts import (
    Framing,
    FramingMode,
    RemakeOptions,
    SpeechMode,
    VoiceCaptions,
    VoiceOptions,
)

pytestmark = pytest.mark.gpu


@pytest.fixture(scope="module")
def client() -> OllamaClient:
    settings = Settings(_env_file=None)
    client = OllamaClient(
        host=settings.ollama_host,
        model=settings.ollama_model,
        num_ctx=settings.ollama_num_ctx,
    )
    if not client.is_available():
        pytest.skip(f"Ollama not reachable at {settings.ollama_host}")
    return client


def read(client: OllamaClient, note: str, options: RemakeOptions | None = None) -> RemakeOptions:
    options = options or RemakeOptions(notes=note)
    answer, _ = interpret_note(
        client, note, options=options, clip_language="English", duration_sec=42
    )
    return apply_interpretation(options, answer).options


# ── Each note reaches the control it is about ────────────────────────────────


def test_losing_a_moving_subject_asks_for_tracking(client: OllamaClient) -> None:
    resolved = read(client, "it keeps losing the ball when play switches to the other wing")
    assert resolved.framing is not None
    assert resolved.framing.mode is FramingMode.TRACK


def test_things_cut_off_at_the_sides_asks_for_the_whole_frame(client: OllamaClient) -> None:
    resolved = read(
        client, "I can't see the whole pitch, everything important is cut off at the sides"
    )
    assert resolved.framing is not None
    assert resolved.framing.mode is FramingMode.FIT


def test_naming_a_side_sets_the_anchor(client: OllamaClient) -> None:
    resolved = read(
        client, "the goal is always on the left of the picture, keep the window over there"
    )
    assert resolved.framing is not None
    assert resolved.framing.crop == "left"


def test_asking_for_another_language_sets_one(client: OllamaClient) -> None:
    resolved = read(client, "can we have this in Spanish instead of the English commentary")
    assert resolved.voice is not None
    assert resolved.voice.language.lower().startswith("es")


def test_cutting_in_late_moves_the_start_earlier(client: OllamaClient) -> None:
    resolved = read(client, "it cuts in about three seconds too late, start earlier")
    assert resolved.start_delta_sec is not None
    assert resolved.start_delta_sec < 0


# ── The two rules that keep it honest ────────────────────────────────────────


def test_a_note_about_one_thing_changes_only_that_thing(client: OllamaClient) -> None:
    """The scoping gate, and the reason `topics` exists.

    Without it this note comes back carrying a framing mode and a crop anchor
    as well as a language, and the remake reframes a clip whose framing nobody
    complained about.
    """
    resolved = read(client, "can we have this in Spanish instead of the English commentary")
    assert resolved.voice is not None, "the language should have been read"
    assert resolved.framing is None, "a note about language must not reframe the clip"
    assert resolved.start_delta_sec == 0
    assert resolved.end_delta_sec == 0


def test_a_stated_setting_survives_the_model_disagreeing_with_it(client: OllamaClient) -> None:
    """A framing the reviewer chose is not up for reinterpretation.

    The note argues for TRACK and the framing says FIT. FIT wins, because the
    reviewer picked a framing mode deliberately and the note adds nothing the
    form does not already say.

    Language is the deliberate exception, covered below: a note that names a
    language beats the form, because a dropdown cannot tell a choice from a
    default and a note saying "in English" beside a form left on Spanish is not
    a real disagreement.
    """
    stated = RemakeOptions(
        notes="it keeps losing the ball, track it",
        framing=Framing(mode=FramingMode.FIT),
        voice=VoiceOptions(
            mode=SpeechMode.BED, voice="af_heart", language="en-gb", captions=VoiceCaptions.KEEP
        ),
    )
    resolved = read(client, stated.notes or "", stated)
    assert resolved.framing is not None
    assert resolved.framing.mode is FramingMode.FIT
    assert resolved.voice is not None
    assert resolved.voice.language == "en-gb", "the note said nothing about language"


def test_a_note_naming_a_language_beats_the_form(client: OllamaClient) -> None:
    """The failure this rule was changed for, verbatim.

    A reviewer wrote *"Let's change commentary voice to English and also try to
    follow the ball"* against a form whose language dropdown was still on its
    default, and the clip came back in Spanish. The old rule treated an
    untouched control as a deliberate choice; it cannot be, and the note is both
    more specific and more recent.
    """
    stated = RemakeOptions(
        notes="Let's change commentary voice to English and also try to follow the ball",
        voice=VoiceOptions(
            mode=SpeechMode.REPLACE,
            voice="ef_dora",
            language="es",
            captions=VoiceCaptions.REBUILD,
        ),
    )
    answer, _ = interpret_note(client, stated.notes or "", options=stated, duration_sec=17)
    applied = apply_interpretation(stated, answer)

    assert applied.options.voice is not None
    assert applied.options.voice.language.lower().startswith("en"), (
        "the note asked for English twice"
    )
    assert applied.conflicts, "the disagreement must be recorded, not resolved quietly"


def test_an_unreadable_note_does_not_stop_the_remake(client: OllamaClient) -> None:
    """A note nobody could parse is not a reason to refuse fully-specified work."""
    stated = RemakeOptions(notes="hmm", framing=Framing(mode=FramingMode.FIT))
    answer, interpretation = interpret_note(client, "hmm", options=stated)
    resolved = apply_interpretation(stated, answer).options
    assert resolved.framing is not None
    assert resolved.framing.mode is FramingMode.FIT
    assert interpretation.summary


# ── Translation ──────────────────────────────────────────────────────────────


def test_a_translation_comes_back_without_a_preface(client: OllamaClient) -> None:
    """A preface would be read aloud by the synthesiser.

    Constraining the response shape is cheaper and more reliable than stripping
    "Sure, here is the Spanish:" off the front afterwards.
    """
    result = translate(
        client,
        "What a strike from thirty yards, the keeper had no chance.",
        target_language="Spanish",
    )
    assert result
    assert not result.lower().startswith(("sure", "here", "translation"))
    assert '"' not in result[:1]
    # Recognisably Spanish rather than the English echoed back.
    assert any(word in result.lower() for word in ("portero", "tiro", "disparo", "remate"))


def test_empty_text_translates_to_nothing_without_calling_the_model() -> None:
    class Unusable:
        model = "should-not-be-called"

        def generate_structured(self, **kwargs: object) -> object:
            raise AssertionError("empty text should not reach the model")

    assert translate(Unusable(), "   ", target_language="Spanish") == ""  # type: ignore[arg-type]
