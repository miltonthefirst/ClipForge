"""The bin: moving files out of the way without destroying them.

Every test here is about the difference between *out of the way* and *gone*,
because that difference is the whole feature. A delete that cannot be undone is
easy to write and is not what was asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge.media.trash import Trash, TrashError
from clipforge.media.workspace import Workspace
from clipforge.scheduler.storage import StorageRoutes

pytestmark = pytest.mark.unit


def a_file(path: Path, *, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ── Putting things in ────────────────────────────────────────────────────────


def test_a_binned_file_leaves_its_original_place(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4")

    trash.put(clip, kind="clip")

    assert not clip.exists()


def test_a_binned_file_is_still_on_disk(tmp_path: Path) -> None:
    """The point of a bin. Nothing here deletes anything."""
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4", size=4096)

    item = trash.put(clip, kind="clip")

    assert trash.used_bytes() >= 4096
    assert item.size_bytes == 4096


def test_the_bin_remembers_where_a_file_came_from(tmp_path: Path) -> None:
    """Without the original path a bin is a place files go to be forgotten with
    extra steps."""
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4")

    item = trash.put(clip, kind="clip", record_id="abc")

    assert item.original_path == str(clip)
    assert item.kind == "clip"
    assert item.record_id == "abc"


def test_two_files_with_the_same_name_both_fit(tmp_path: Path) -> None:
    """Two users' clips can share a file name, and a flat bin would lose one."""
    trash = Trash(tmp_path / "trash")
    first = a_file(tmp_path / "clips" / "alice" / "clip.mp4", size=100)
    second = a_file(tmp_path / "clips" / "bob" / "clip.mp4", size=200)

    trash.put(first)
    trash.put(second)

    assert len(trash.items()) == 2
    assert {item.size_bytes for item in trash.items()} == {100, 200}


def test_binning_something_that_is_not_there_says_so(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    with pytest.raises(TrashError, match="not a file"):
        trash.put(tmp_path / "clips" / "gone.mp4")


# ── Getting them back ────────────────────────────────────────────────────────


def test_a_binned_file_can_be_put_back(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4", size=512)
    item = trash.put(clip)

    restored = trash.restore(item.id)

    assert restored == clip
    assert clip.read_bytes() == b"x" * 512
    assert trash.items() == []


def test_restoring_refuses_to_overwrite_what_is_already_there(tmp_path: Path) -> None:
    """Not theoretical: re-running the job that made a clip writes the same path.

    The file in the bin and the file on disk are two different renders, and only
    the operator knows which they want.
    """
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4", size=10)
    item = trash.put(clip)
    a_file(tmp_path / "clips" / "abc.mp4", size=99)  # the job ran again

    with pytest.raises(TrashError, match="already back"):
        trash.restore(item.id)

    assert len(trash.items()) == 1, "a refused restore must not lose the binned copy"


def test_restoring_recreates_a_directory_that_was_cleaned_up(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "nested" / "abc.mp4")
    item = trash.put(clip)
    (tmp_path / "clips" / "nested").rmdir()

    assert trash.restore(item.id).is_file()


# ── Getting rid of them ──────────────────────────────────────────────────────


def test_purging_deletes_for_good(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    clip = a_file(tmp_path / "clips" / "abc.mp4", size=777)
    item = trash.put(clip)

    assert trash.purge(item.id) == 777
    assert trash.items() == []
    assert not clip.exists()


def test_emptying_takes_everything(tmp_path: Path) -> None:
    trash = Trash(tmp_path / "trash")
    for index in range(3):
        trash.put(a_file(tmp_path / "clips" / f"{index}.mp4", size=100))

    assert trash.empty() == 300
    assert trash.items() == []


def test_an_id_from_outside_the_bin_is_refused(tmp_path: Path) -> None:
    """`purge` is a recursive delete and its id arrives over HTTP.

    Without this, `../../..` in a path segment reaches anything the worker can
    write.
    """
    trash = Trash(tmp_path / "trash")
    a_file(tmp_path / "precious" / "keep.mp4")

    with pytest.raises(TrashError):
        trash.purge("../precious")

    assert (tmp_path / "precious" / "keep.mp4").is_file()


def test_an_item_with_no_manifest_does_not_break_the_listing(tmp_path: Path) -> None:
    """One unreadable item must not make the bin unopenable, which is precisely
    when somebody needs to open it."""
    trash = Trash(tmp_path / "trash")
    trash.put(a_file(tmp_path / "clips" / "good.mp4"))
    (trash.root / "orphan").mkdir()
    (trash.root / "orphan" / "mystery.mp4").write_bytes(b"x")

    assert len(trash.items()) == 1


# ── The budget ───────────────────────────────────────────────────────────────


def test_the_bin_is_outside_the_disk_budget(tmp_path: Path) -> None:
    """The collector evicts SOURCES.

    Counting binned bytes against the cap would have it delete live downloads to
    make room for deleted ones — live data evicted to house dead data.
    """
    workspace = Workspace(tmp_path / "ws", max_gb=1)
    a_file(workspace.clips_dir / "live.mp4", size=1000)
    before = workspace.used_bytes()

    Trash(workspace.trash_dir).put(a_file(workspace.clips_dir / "dead.mp4", size=5000))

    assert workspace.used_bytes() == before


# ── The routes ───────────────────────────────────────────────────────────────


def routes(tmp_path: Path) -> tuple[StorageRoutes, Workspace]:
    workspace = Workspace(tmp_path / "ws", max_gb=1)
    return StorageRoutes(workspace), workspace


def test_the_listing_shows_what_is_on_disk(tmp_path: Path) -> None:
    api, workspace = routes(tmp_path)
    a_file(workspace.clips_dir / "u1" / "clip.mp4", size=10)
    a_file(workspace.sources_dir / "src.mp4", size=20)

    report = api.listing({})

    assert [f["name"] for f in report["clips"]] == ["clip.mp4"]
    assert [f["name"] for f in report["sources"]] == ["src.mp4"]
    assert report["trashBytes"] == 0


def test_a_path_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    """The token guards the port. This guards the argument.

    A caller holding a valid token is still not entitled to name a file
    somewhere else and have the worker move it.
    """
    api, _ = routes(tmp_path)
    outside = a_file(tmp_path / "elsewhere" / "precious.mp4")

    result = api.to_trash({"paths": [str(outside)]})

    assert result["moved"] == []
    assert "not inside the workspace" in result["failed"][0]["error"]
    assert outside.is_file()


def test_traversal_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    api, workspace = routes(tmp_path)
    outside = a_file(tmp_path / "elsewhere" / "precious.mp4")
    sneaky = str(workspace.clips_dir / ".." / ".." / "elsewhere" / "precious.mp4")

    api.to_trash({"paths": [sneaky]})

    assert outside.is_file()


def test_something_already_in_the_bin_is_not_binned_again(tmp_path: Path) -> None:
    """A second manifest would point at a path inside the first, and restoring
    the pair in the wrong order loses the file."""
    api, workspace = routes(tmp_path)
    api.to_trash({"paths": [str(a_file(workspace.clips_dir / "clip.mp4"))]})
    binned = api.listing({})["trash"][0]
    inside = next((workspace.trash_dir / binned["id"]).glob("*.mp4"))

    result = api.to_trash({"paths": [str(inside)]})

    assert "already in the bin" in result["failed"][0]["error"]


def test_emptying_needs_the_flag_rather_than_an_empty_list(tmp_path: Path) -> None:
    """An empty list is what a buggy client sends by accident, and emptying the
    bin must never be the accident."""
    api, workspace = routes(tmp_path)
    api.to_trash({"paths": [str(a_file(workspace.clips_dir / "clip.mp4"))]})

    assert api.purge({"ids": []})["freedBytes"] == 0
    assert len(api.listing({})["trash"]) == 1

    assert api.purge({"all": True})["freedBytes"] > 0
    assert api.listing({})["trash"] == []


def test_a_round_trip_through_the_routes(tmp_path: Path) -> None:
    api, workspace = routes(tmp_path)
    clip = a_file(workspace.clips_dir / "clip.mp4", size=4096)

    api.to_trash({"paths": [str(clip)]})
    assert not clip.exists()

    binned = api.listing({})["trash"][0]
    api.restore({"ids": [binned["id"]]})

    assert clip.is_file()
    assert api.listing({})["trash"] == []
