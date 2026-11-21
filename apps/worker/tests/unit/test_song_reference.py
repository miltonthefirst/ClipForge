"""An original in the style of a song someone likes (docs/MUSIC-PLAN.md, M7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from clipforge.media.reference import Reference
from clipforge.media.sources import IngestError
from clipforge.stages.song import LyricsStage, SongError, takes_for
from clipforge.synth.lyrics import shared_phrases
from clipforge.synth.song_brief import BriefSpec
from clipforge_contracts import GenerateMusicOptions, IngestErrorCode

from tests.unit.test_song import LYRICS, Workspace, context_for, draft
from tests.unit.test_song_brief import Client, spec

pytestmark = pytest.mark.unit

URL = "https://youtu.be/Msdsdts3NlI"
HEARD = "\n".join(
    [
        "I keep your letters in a shoebox by the door",
        "Golden river carry me home",
        "Golden river carry me home tonight",
        "the city lights are calling out my name again",
    ]
    * 3
)


def options(**overrides: Any) -> GenerateMusicOptions:
    values: dict[str, Any] = {"lyricsFrom": "REFERENCE", "referenceUrl": URL}
    values.update(overrides)
    return GenerateMusicOptions.model_validate(values)


class Fetch:
    def __init__(self, error: IngestError | None = None) -> None:
        self.error = error
        self.folders: list[Path] = []

    def __call__(self, url: str, folder: Path) -> Reference:
        self.folders.append(folder)
        if self.error:
            raise self.error
        folder.mkdir(parents=True, exist_ok=True)
        audio = folder / "reference.webm"
        audio.write_bytes(b"not really audio")
        return Reference(
            url=url,
            audio=audio,
            title="Mbali Zondo - Golden River (Official Video)",
            channel="Mbali Zondo",
            duration_s=214.0,
            tags=("amapiano", "golden river"),
        )


def stage(
    tmp: Path, client: Client, fetch: Fetch, heard: str = HEARD, tempo: float | None = 113.2
) -> LyricsStage:
    return LyricsStage(
        trends=None,  # type: ignore[arg-type]
        workspace=Workspace(tmp),  # type: ignore[arg-type]
        client_factory=lambda _c: client,  # type: ignore[arg-type, return-value]
        fetch=fetch,
        tempo_of=lambda _path, _ffmpeg: tempo,
        listen=lambda _c, _audio, _folder: (heard, "en"),
    )


def run(tmp: Path, client: Client, fetch: Fetch | None = None, **kw: Any) -> dict[str, Any]:
    checkpoint = stage(tmp, client, fetch or Fetch(), **kw).run(context_for(options())).checkpoint
    assert checkpoint is not None
    return checkpoint


def described(**overrides: Any) -> BriefSpec:
    return spec(**{"title": "Paper Boats", "about": "missing a home far away", **overrides})


# ── Its words stay its own ───────────────────────────────────────────────────


def test_five_words_in_a_row_from_the_original_are_found() -> None:
    mine = "[Verse 1]\nI keep your letters in a shoebox by the stairs\nsomething else entirely"
    assert shared_phrases(mine, HEARD) == [
        "i keep your letters in",
        "keep your letters in a",
        "your letters in a shoebox",
        "letters in a shoebox by",
    ]


def test_a_whole_short_line_from_the_original_is_found() -> None:
    assert shared_phrases("[Chorus]\nGolden river, carry me home!", HEARD) == [
        "golden river carry me home"
    ]


def test_glue_words_and_new_lines_are_not_copying() -> None:
    mine = "[Verse 1]\nand I know that you are\npaper boats on a quiet street"
    assert shared_phrases(mine, "and I know that you are the one") == []
    assert shared_phrases(LYRICS, HEARD) == []


# ── The job ──────────────────────────────────────────────────────────────────


def test_a_liked_song_becomes_an_original_in_its_style(tmp_path: Path) -> None:
    client = Client(described(), draft(title="Paper Boats"))
    fetch = Fetch()
    checkpoint = run(tmp_path, client, fetch)

    # Studied, then gone: nothing of the original is kept on the PC.
    assert fetch.folders and not fetch.folders[0].exists()
    described_prompt = client.asked[0][1]
    assert "Measured tempo: 113 bpm" in described_prompt and "shoebox" in described_prompt
    written = client.asked[1][1]
    assert "ORIGINAL song in the style" in written and "missing a home far away" in written
    # The words heard are never handed to the writer, only checked against.
    assert "shoebox" not in written

    assert checkpoint["title"] == "Paper Boats" and checkpoint["lyrics"] == LYRICS
    assert checkpoint["bpm"] == 113 and checkpoint["language"] == "en"
    assert checkpoint["inspiration"].startswith("In the style of: Mbali Zondo")
    assert checkpoint["reference"]["wordsHeard"] == len(HEARD.split())
    takes = takes_for(context_for(options()), options(takes=1), checkpoint)
    assert takes[0]["caption"].startswith("amapiano, deep log drum")


def test_a_draft_that_borrows_a_line_is_asked_for_again_then_refused(tmp_path: Path) -> None:
    borrowing = draft(lyrics=LYRICS + "\nGolden river carry me home")
    client = Client(described(), borrowing, draft())
    run(tmp_path, client)
    assert "borrows 'golden river carry me home'" in client.asked[2][1]

    with pytest.raises(SongError, match="borrows"):
        run(tmp_path, Client(described(), borrowing, borrowing))


def test_a_draft_named_after_the_original_is_asked_for_again(tmp_path: Path) -> None:
    client = Client(described(), draft(title="Golden Days"), draft())
    checkpoint = run(tmp_path, client)
    assert "'Golden'" in client.asked[2][1] and "Do not use" in client.asked[2][1]
    assert checkpoint["title"] == "Sunday Light"


def test_a_song_with_no_words_heard_stays_an_instrumental(tmp_path: Path) -> None:
    client = Client(described(vocal="instrumental", title="Golden Hour"))
    checkpoint = run(tmp_path, client, heard="oh oh")
    assert [name for name, _ in client.asked] == ["BriefSpec"]
    assert checkpoint["lyrics"] is None and checkpoint["seconds"] == 214
    # "Golden" is in the original title, so the new one is the series'.
    assert checkpoint["title"] == "Amapiano instrumental"


def test_a_link_that_cannot_be_fetched_says_why_and_keeps_nothing(tmp_path: Path) -> None:
    fetch = Fetch(IngestError(IngestErrorCode.PRIVATE, "This video is private."))
    with pytest.raises(SongError, match="private") as raised:
        run(tmp_path, Client(described()), fetch)
    assert raised.value.code == "REFERENCE"
    assert not fetch.folders[0].exists()


def test_a_reference_song_needs_its_link(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="needs its link"):
        stage(tmp_path, Client(described()), Fetch()).run(context_for(options(referenceUrl="  ")))
