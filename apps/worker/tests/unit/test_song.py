"""A song written by the local model and sung by the music model (docs/MUSIC-PLAN.md, M2 and M5)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.notify.jobs import on_job_end
from clipforge.stages import song as song_module
from clipforge.stages.pipeline import GENERATE_MUSIC_PIPELINE, new_generate_music_job
from clipforge.stages.song import LyricsStage, SingStage, SongError, takes_for
from clipforge.synth.lyrics import (
    LyricsError,
    SongDraft,
    brief_for,
    names_in,
    normalise_key,
    series_for,
    tidy_lyrics,
    write_song,
)
from clipforge_contracts import GenerateMusicOptions, Job, NotificationKind, StageName, Track, Trend

pytestmark = pytest.mark.unit

LYRICS = "\n".join(
    [
        "[Verse 1]",
        *["a line that scans well"] * 4,
        "",
        "[Chorus]",
        *["the hook comes round again"] * 4,
    ]
)


def options(**overrides: Any) -> GenerateMusicOptions:
    values: dict[str, Any] = {
        "lyricsFrom": "IDEA",
        "style": "amapiano, log drum, warm piano",
        "idea": "Sunday",
    }
    values.update(overrides)
    return GenerateMusicOptions.model_validate(values)


def trend(**overrides: Any) -> Trend:
    values: dict[str, Any] = {
        "id": "t1",
        "uid": "alice",
        "jobId": "r1",
        "topic": "Senator Jane Doakes calls on Acme Corp to explain the outage",
        "rank": 1,
        "score": 50,
        "signals": [],
        "videos": [],
        "status": "NEW",
        "createdAt": "2026-11-17T00:00:00Z",
    }
    values.update(overrides)
    return Trend.model_validate(values)


def draft(**overrides: Any) -> SongDraft:
    values: dict[str, Any] = {
        "title": "Sunday Light",
        "theme": "a slow evening",
        "mood": "warm, joyful",
        "bpm": 112,
        "key": "a minor",
        "lyrics": LYRICS,
    }
    values.update(overrides)
    return SongDraft(**values)


class Client:
    model = "qwen3.5:4b"

    def __init__(self, *answers: SongDraft, available: bool = True) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.available = available

        class Stats:
            def summary(self) -> dict[str, Any]:
                return {"calls": 1}

        self.stats = Stats()

    def is_available(self) -> bool:
        return self.available

    def generate_structured(self, **kwargs: Any) -> SongDraft:
        self.prompts.append(kwargs["prompt"])
        return self.answers.pop(0)


# ── The words ────────────────────────────────────────────────────────────────


def test_names_in_a_headline_are_found_and_ordinary_words_are_not() -> None:
    found = names_in("Senator Jane Doakes calls on Acme Corp to explain the outage")
    assert {"Senator", "Jane", "Doakes", "Acme", "Corp"} <= set(found)
    assert "calls" not in found and "the" not in found and "outage" not in found


def test_a_trend_song_is_told_the_feeling_and_never_the_names() -> None:
    brief = brief_for(options(lyricsFrom="TREND", trendId="t1"), trend())
    assert brief.inspiration.startswith("Trend: Senator")
    assert "Doakes" in brief.avoid


def test_a_song_that_keeps_a_name_is_asked_for_again_then_refused() -> None:
    brief = brief_for(options(lyricsFrom="TREND", trendId="t1"), trend())
    named = draft(lyrics=LYRICS + "\nand Doakes was there")
    client = Client(named, draft())
    assert write_song(client, brief, options()).title == "Sunday Light"  # type: ignore[arg-type]
    assert "Doakes" in client.prompts[1] and "Do not use" in client.prompts[1]

    with pytest.raises(LyricsError, match="Doakes"):
        write_song(Client(named, named), brief, options())  # type: ignore[arg-type]


def test_a_song_without_a_chorus_is_refused() -> None:
    verses = "[Verse 1]\n" + "\n".join(["words that go on"] * 10)
    brief = brief_for(options(), None)
    with pytest.raises(LyricsError, match="Chorus"):
        write_song(Client(draft(lyrics=verses), draft(lyrics=verses)), brief, options())  # type: ignore[arg-type]


def test_notes_and_ideas_need_their_words() -> None:
    with pytest.raises(LyricsError):
        brief_for(options(lyricsFrom="NOTES", notes="  "), None)
    with pytest.raises(LyricsError):
        brief_for(options(lyricsFrom="TREND"), None)
    brief = brief_for(options(lyricsFrom="NOTES", notes="\nPhotosynthesis\nlight to sugar"), None)
    assert brief.inspiration == "Your notes: Photosynthesis"


def test_lines_that_are_not_words_are_dropped() -> None:
    assert tidy_lyrics("[Outro]\n}\n(oh oh)\n...") == "[Outro]\n(oh oh)"


def test_a_song_that_sings_its_own_key_is_asked_for_again() -> None:
    sung_key = draft(lyrics=LYRICS + "\nC major, upbeat tempo")
    client = Client(sung_key, draft())
    assert write_song(client, brief_for(options(), None), options()).title == "Sunday Light"  # type: ignore[arg-type]
    assert "refused because it sings the tempo" in client.prompts[1]


def test_long_lines_pass_the_second_time() -> None:
    prose = LYRICS.replace(
        "a line that scans well",
        "a line that goes on and on far longer than any singer can breathe",
    )
    client: Any = Client(draft(lyrics=prose), draft(lyrics=prose))
    assert write_song(client, brief_for(options(), None), options()).lyrics == prose
    assert "too many lines" in client.prompts[1]


def test_lyrics_are_tidied_into_sections() -> None:
    assert tidy_lyrics("[Verse]\nhello  \n\n\n[Chorus]\nhey\n") == "[Verse]\nhello\n\n[Chorus]\nhey"
    assert tidy_lyrics("[Verse]\nhello\n[Chorus]\nhey") == "[Verse]\nhello\n\n[Chorus]\nhey"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("a minor", "A minor"),
        ("F#m", "F# minor"),
        ("Bb major", "Bb major"),
        ("C", "C major"),
        ("dorian", None),
    ],
)
def test_keys_are_written_the_way_the_music_model_reads_them(
    given: str, expected: str | None
) -> None:
    assert normalise_key(given) == expected


def test_the_series_is_asked_for_or_named_from_the_style() -> None:
    assert series_for(options()) == "Amapiano"
    assert series_for(options(series="Sunday sessions")) == "Sunday sessions"
    assert series_for(options(lyricsFrom="NOTES", notes="x")) == "Study songs"


# ── The job ──────────────────────────────────────────────────────────────────


class Lease:
    def observe(self) -> int:
        return 0


class Broker:
    def __init__(self) -> None:
        self.models: list[str] = []

    @contextmanager
    def acquire(self, model: str, _mb: int) -> Any:
        self.models.append(model)
        yield Lease()


def context_for(
    opts: GenerateMusicOptions, lyrics: dict[str, Any] | None = None, tmp: Path | None = None
) -> Any:
    job = new_generate_music_job(uid="alice", options=opts, job_id="job1")
    if lyrics is not None:
        job.stages[0].checkpoint = lyrics

    class Context:
        settings = Settings(use_emulators=True, comfyui_url="")

        def __init__(self) -> None:
            self.job = job
            self.broker = Broker()
            self.notes: list[str] = []

        def progress(self, note: str) -> None:
            self.notes.append(note)

        def stopping(self) -> bool:
            return False

    return Context()


def test_the_job_writes_then_sings_both_on_the_gpu() -> None:
    job = new_generate_music_job(uid="alice", options=options())
    assert [(s.name, s.lane) for s in job.stages] == list(GENERATE_MUSIC_PIPELINE)
    assert job.generate_music_options is not None


def test_lyrics_are_written_under_the_llm_lease() -> None:
    context = context_for(options())
    stage = LyricsStage(trends=None, client_factory=lambda _c: Client(draft()))  # type: ignore[arg-type, return-value]
    checkpoint = stage.run(context).checkpoint
    assert checkpoint is not None
    assert checkpoint["title"] == "Sunday Light" and checkpoint["key"] == "A minor"
    assert checkpoint["inspiration"] == "Idea: Sunday" and checkpoint["series"] == "Amapiano"
    assert context.broker.models == ["ollama:qwen3.5:4b"]


def test_an_instrumental_needs_no_model() -> None:
    context = context_for(options(lyricsFrom="NONE"))
    checkpoint = LyricsStage(trends=None).run(context).checkpoint  # type: ignore[arg-type]
    assert checkpoint is not None and checkpoint["lyrics"] is None
    assert context.broker.models == []


def test_no_model_running_is_worth_a_retry() -> None:
    stage = LyricsStage(trends=None, client_factory=lambda _c: Client(available=False))  # type: ignore[arg-type, return-value]
    with pytest.raises(SongError) as raised:
        stage.run(context_for(options()))
    assert raised.value.retryable


def test_each_take_is_the_same_song_with_its_own_seed_and_voice() -> None:
    song = {
        "title": "Sunday Light",
        "lyrics": LYRICS,
        "mood": "warm",
        "bpm": 112,
        "key": "A minor",
        "series": "Amapiano",
    }
    takes = takes_for(context_for(options(seed=7)), options(seed=7, takes=2), song)
    assert [t["seed"] for t in takes] == [7, 8]
    assert [t["title"] for t in takes] == ["Sunday Light", "Sunday Light (take 2)"]
    assert "female vocal" in takes[0]["caption"] and "male vocal" in takes[1]["caption"]
    assert all(t["lyrics"] == LYRICS and t["keyscale"] == "A minor" for t in takes)

    one_voice = takes_for(context_for(options()), options(vocal="MALE", takes=2), song)
    assert all("female" not in t["caption"] for t in one_voice)

    instrumental = takes_for(
        context_for(options()), options(lyricsFrom="NONE", takes=1), {"title": "x"}
    )
    assert instrumental[0]["instrumental"] and instrumental[0]["caption"].endswith("instrumental")


class Tracks:
    def __init__(self) -> None:
        self.saved: list[Track] = []

    def upsert(self, track: Track) -> bool:
        self.saved.append(track)
        return True


class Workspace:
    def __init__(self, root: Path) -> None:
        self.tmp_dir = root / "tmp"
        self.music_dir = root / "music"


def test_sung_takes_join_the_library_with_their_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    song = {
        "title": "Sunday Light",
        "lyrics": LYRICS,
        "mood": "warm",
        "bpm": 112,
        "key": "A minor",
        "series": "Amapiano",
        "inspiration": "Idea: Sunday",
    }
    context = context_for(options(takes=2), lyrics=song)
    requests: list[dict[str, Any]] = []

    def fake_run(_settings: Settings, request: dict[str, Any], out: Path, **_kw: Any) -> Path:
        requests.append(request)
        out.mkdir(parents=True, exist_ok=True)
        entries = []
        for n, take in enumerate(request["tracks"], start=1):
            (out / f"{n}.wav").write_bytes(f"audio {n}".encode())
            entries.append(
                {
                    "file": f"{n}.wav",
                    "title": take["title"],
                    "prompt": take["caption"],
                    "lyrics": take["lyrics"],
                    "inspiration": take["inspiration"],
                    "series": take["series"],
                    "seconds": 150,
                    "instrumental": False,
                    "model": "ACE-Step",
                    "licence": "MIT",
                }
            )
        (out / "manifest.json").write_text(json.dumps({"tracks": entries}), encoding="utf-8")
        return out / "manifest.json"

    monkeypatch.setattr(song_module, "run_ngoma", fake_run)
    tracks = Tracks()
    stage = SingStage(tracks=tracks, workspace=Workspace(tmp_path))  # type: ignore[arg-type]
    checkpoint = stage.run(context).checkpoint

    assert checkpoint is not None and checkpoint["made"] == 2
    assert context.broker.models == ["ngoma:ace-step-1.5"]
    assert len(requests[0]["tracks"]) == 2
    assert [t.lyrics for t in tracks.saved] == [LYRICS, LYRICS]
    assert {t.job_id for t in tracks.saved} == {"job1"}
    assert tracks.saved[0].inspiration == "Idea: Sunday"
    assert not (tmp_path / "tmp" / "song-job1").exists()


def test_a_finished_song_says_how_many_takes_to_listen_to() -> None:
    finished = Job.model_validate(
        {
            "id": "j1",
            "uid": "alice",
            "type": "GENERATE_MUSIC",
            "status": "COMPLETED",
            "stages": [
                {
                    "name": "LYRICS",
                    "lane": "GPU",
                    "status": "DONE",
                    "checkpoint": {"title": "Sunday Light"},
                },
                {
                    "name": "SING",
                    "lane": "GPU",
                    "status": "DONE",
                    "checkpoint": {"trackIds": ["a", "b"]},
                },
            ],
            "attempts": 1,
            "maxAttempts": 2,
            "createdAt": "2026-11-17T00:00:00Z",
            "updatedAt": "2026-11-17T00:00:00Z",
        }
    )
    calls: list[dict[str, Any]] = []

    class Recorder:
        def notify(self, kind: NotificationKind, **kwargs: Any) -> int:
            calls.append({"kind": kind, **kwargs})
            return 1

    on_job_end(finished, Recorder(), lambda _id: 0)  # type: ignore[arg-type]
    assert calls[0]["kind"] is NotificationKind.CLIPS_READY
    assert calls[0]["title"] == "2 new takes are ready to listen to"
    assert calls[0]["link"] == "/music" and "Sunday Light" in calls[0]["body"]
    assert StageName.SING.value == "SING"
