"""A song from a pasted brief (docs/MUSIC-PLAN.md, M6): its lyrics as they are, the rest read."""

from __future__ import annotations

from typing import Any

import pytest
from clipforge.stages.song import LyricsStage, SongError, takes_for
from clipforge.synth.lyrics import SongDraft
from clipforge.synth.song_brief import BriefSpec, find_lyrics, language_code, voice_caption
from clipforge_contracts import GenerateMusicOptions

from tests.unit.test_song import LYRICS, context_for, draft

pytestmark = pytest.mark.unit

CHAT_ANSWER = """Here's a full song concept for you!

**Title:** Sunday Light

**Style of Music:** Amapiano, log drums, soft piano, female vocals, 112 BPM

**Lyrics:**

**[Intro]**
*(soft piano)*

**[Verse 1]**
Mama's cooking, windows wide
Kids are laughing, side by side
The week was heavy, now it's done
We slow it down beneath the sun

**[Chorus]**
Sunday light, Sunday light
Hold me close, it feels so right

**Verse 2:**
Grandpa's stories, same old jokes
Break the chains of Monday's call

*Chorus x2*

[Outro]
(Sunday light)

**Visuals:** A golden sunset over a township street, warm windows glowing.

Let me know if you'd like a different version or a slower tempo!
"""


def options(**overrides: Any) -> GenerateMusicOptions:
    values: dict[str, Any] = {"lyricsFrom": "BRIEF", "brief": CHAT_ANSWER}
    values.update(overrides)
    return GenerateMusicOptions.model_validate(values)


def spec(**overrides: Any) -> BriefSpec:
    values: dict[str, Any] = {
        "title": "Sunday Light",
        "about": "a slow Sunday with family after a hard week",
        "style": "amapiano, deep log drum, soft piano, soulful",
        "vocal": "female",
        "language": "English",
        "mood": "warm, grateful",
        "bpm": 112,
        "key": "",
        "seconds": 0,
        "scene": "a township street at sunset, warm windows glowing",
        "art_style": "digital painting",
    }
    values.update(overrides)
    return BriefSpec(**values)


class Client:
    """Answers by what is asked for: the brief's form, or a song."""

    model = "qwen3.5:4b"

    def __init__(self, brief: BriefSpec, *songs: SongDraft) -> None:
        self.brief = brief
        self.songs = list(songs)
        self.asked: list[tuple[str, str]] = []

        class Stats:
            def summary(self) -> dict[str, Any]:
                return {"calls": 1}

        self.stats = Stats()

    def is_available(self) -> bool:
        return True

    def generate_structured(self, **kwargs: Any) -> Any:
        model = kwargs["schema_model"]
        self.asked.append((model.__name__, kwargs["prompt"]))
        return self.brief if model is BriefSpec else self.songs.pop(0)


def run(opts: GenerateMusicOptions, client: Client) -> dict[str, Any]:
    stage = LyricsStage(trends=None, client_factory=lambda _c: client)  # type: ignore[arg-type, return-value]
    checkpoint = stage.run(context_for(opts)).checkpoint
    assert checkpoint is not None
    return checkpoint


# ── The lyrics, by rule ──────────────────────────────────────────────────────


def test_a_chat_answers_lyrics_are_found_word_for_word() -> None:
    lyrics = find_lyrics(CHAT_ANSWER)
    assert lyrics is not None
    assert lyrics.startswith("[Intro]\n\n[Verse 1]\nMama's cooking, windows wide")
    # A sung line that begins like a section name stays sung.
    assert "Break the chains of Monday's call" in lyrics
    assert "[Break" not in lyrics
    # "Chorus x2" sings the chorus again; the direction and the chat around it go.
    assert lyrics.count("Hold me close, it feels so right") == 2
    assert "soft piano" not in lyrics
    assert "Visuals" not in lyrics and "Let me know" not in lyrics and "Title" not in lyrics


def test_plain_headings_and_a_repeat_line_become_sections() -> None:
    text = (
        "Verse 1\nI wake up early, coffee black\nNo looking back\n\n"
        "Chorus:\nWe rise, we rise\nUp into the skies\n\n"
        "Verse 2 - softer\nDrop it low and let it go\nSteady flow\n\n"
        "Repeat Chorus\n\nStyle: drill, dark 808s"
    )
    assert find_lyrics(text) == (
        "[Verse 1]\nI wake up early, coffee black\nNo looking back\n\n"
        "[Chorus]\nWe rise, we rise\nUp into the skies\n\n"
        "[Verse 2 - softer]\nDrop it low and let it go\nSteady flow\n\n"
        "[Chorus]\nWe rise, we rise\nUp into the skies"
    )


def test_lyrics_under_a_heading_with_no_markers_get_them() -> None:
    text = "Lyrics:\nfirst line here\nsecond line here\n\nhook line\nhook again\n\nthird line\nfourth\n\nhook line\nhook again"
    lyrics = find_lyrics(text)
    assert lyrics is not None
    assert lyrics.startswith("[Verse 1]\nfirst line here")
    assert lyrics.count("[Chorus]") == 2 and "[Verse 2]\nthird line" in lyrics


def test_a_brief_without_lyrics_has_none() -> None:
    assert (
        find_lyrics("An upbeat afrobeats song about Lagos traffic, male voice, 3 minutes") is None
    )
    assert find_lyrics("[Verse 1]\nonly one line") is None


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ("English", "en"),
        ("sw", "sw"),
        ("Swahili and English", "sw"),
        ("isiZulu", "unknown"),
        ("", "en"),
    ],
)
def test_the_language_is_one_the_music_model_knows_or_unknown(given: str, code: str) -> None:
    assert language_code(given) == code


def test_a_duet_and_a_choir_are_said_and_any_lets_takes_differ() -> None:
    assert voice_caption("duet") == "male and female duet vocals"
    assert voice_caption("any") is None


# ── The job ──────────────────────────────────────────────────────────────────


def test_a_brief_with_lyrics_is_sung_as_written_and_sounds_as_it_asks() -> None:
    client = Client(spec(language="sw"))
    checkpoint = run(options(), client)
    assert [name for name, _ in client.asked] == ["BriefSpec"]  # nothing written
    assert checkpoint["lyrics"] == find_lyrics(CHAT_ANSWER) and checkpoint["lyricsGiven"]
    assert checkpoint["title"] == "Sunday Light" and checkpoint["series"] == "Amapiano"
    assert checkpoint["scene"].startswith("a township street")
    # The sung lines are not read twice: the model sees the rest of the brief.
    assert "Mama's cooking" not in client.asked[0][1].split("It comes with lyrics")[0]

    takes = takes_for(context_for(options()), options(takes=2), checkpoint)
    assert all(t["caption"].startswith("amapiano, deep log drum") for t in takes)
    parts = [t["caption"].split(", ") for t in takes]
    assert all("female vocal" in p and "male vocal" not in p for p in parts)
    assert all(t["vocalLanguage"] == "sw" and t["bpm"] == 112 for t in takes)


def test_given_lyrics_are_sung_in_full_and_a_longer_ask_is_filled() -> None:
    short = run(options(), Client(spec()))
    assert short["seconds"] < 180
    longer = run(options(), Client(spec(seconds=200)))
    assert longer["seconds"] == 200


def test_a_brief_without_lyrics_is_written_from_the_brief() -> None:
    brief = "An upbeat afrobeats song about chasing dreams in Lagos traffic, male voice, 3 minutes"
    client = Client(spec(title="Traffic Dreams", vocal="male", seconds=180, bpm=0), draft())
    checkpoint = run(options(brief=brief), client)
    assert [name for name, _ in client.asked] == ["BriefSpec", "SongDraft"]
    written = client.asked[1][1]
    assert "Write a song from this brief" in written and "Lagos traffic" in written
    assert "The music: amapiano, deep log drum" in written and "3 min 0 s" in written
    # The brief named no title, so the one written with the words is kept.
    assert checkpoint["title"] == "Sunday Light" and checkpoint["lyrics"] == LYRICS
    assert checkpoint["bpm"] == 112 and checkpoint["language"] == "en"
    assert checkpoint["voice"] == "male vocal" and not checkpoint["lyricsGiven"]


def test_a_brief_for_an_instrumental_writes_nothing() -> None:
    client = Client(spec(vocal="instrumental", seconds=90))
    checkpoint = run(options(brief="a calm lo-fi beat to study to, no vocals, 90 seconds"), client)
    assert checkpoint["lyrics"] is None and checkpoint["seconds"] == 90
    takes = takes_for(context_for(options()), options(takes=1), checkpoint)
    assert takes[0]["instrumental"] and takes[0]["caption"].endswith("instrumental")


def test_a_style_chosen_on_the_form_wins_over_the_brief() -> None:
    checkpoint = run(options(style="gospel, choir, organ"), Client(spec()))
    assert checkpoint["style"] == "gospel, choir, organ" and checkpoint["series"] == "Gospel"


def test_an_empty_brief_is_refused() -> None:
    with pytest.raises(SongError, match="needs the brief"):
        run(options(brief="   "), Client(spec()))
