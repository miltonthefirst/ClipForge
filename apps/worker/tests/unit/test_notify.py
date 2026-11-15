"""Notifications: who is told what, and when they are not.

The cases that matter are the ones where getting it wrong is noise or silence:
a person who switched a kind off still being pushed, an unapproved account
being told anything, the same job announced twice, a gone device kept forever,
and a failed send turning a finished job into a failed one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from clipforge.config import Settings
from clipforge.notify import Notifier
from clipforge.notify.jobs import on_job_end, wants_youtube_sign_in
from clipforge.notify.kinds import is_on
from clipforge.notify.watch import WAIT_BEFORE_TELLING, AgentNotices
from clipforge_contracts import AgentState, Job, NotificationKind

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


# ── A Firestore small enough to read ─────────────────────────────────────────


class Doc:
    def __init__(self, db: FakeDb, path: str) -> None:
        self._db, self.path, self.id = db, path, path.rsplit("/", 1)[-1]

    @property
    def exists(self) -> bool:
        return self.path in self._db.docs

    def get(self) -> Doc:
        return self

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._db.docs[self.path]) if self.exists else None

    def set(self, data: dict[str, Any]) -> None:
        self._db.docs[self.path] = dict(data)

    def delete(self) -> None:
        self._db.docs.pop(self.path, None)

    def collection(self, name: str) -> Collection:
        return Collection(self._db, f"{self.path}/{name}")


class Collection:
    def __init__(self, db: FakeDb, path: str) -> None:
        self._db, self.path = db, path

    def document(self, doc_id: str) -> Doc:
        return Doc(self._db, f"{self.path}/{doc_id}")

    def stream(self) -> list[Doc]:
        depth = self.path.count("/") + 1
        return [
            Doc(self._db, p)
            for p in sorted(self._db.docs)
            if p.startswith(self.path + "/") and p.count("/") == depth
        ]


class FakeDb:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> Collection:
        return Collection(self, name)

    def user(self, uid: str, *, role: str = "MEMBER", status: str = "APPROVED") -> None:
        self.docs[f"users/{uid}"] = {"uid": uid, "role": role, "status": status}

    def device(self, uid: str, device_id: str, token: str) -> None:
        self.docs[f"users/{uid}/devices/{device_id}"] = {"id": device_id, "token": token}

    def listed(self, uid: str) -> list[dict[str, Any]]:
        return [d.to_dict() or {} for d in self.collection(f"users/{uid}/notifications").stream()]


class Pushes:
    def __init__(self, gone: tuple[str, ...] = (), fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._gone, self._fail = gone, fail

    def __call__(
        self, tokens: list[str], title: str, body: str, link: str, tag: str | None
    ) -> list[str]:
        if self._fail:
            raise RuntimeError("Cloud Messaging is down")
        self.sent.append({"tokens": tokens, "title": title, "body": body, "link": link, "tag": tag})
        return [t for t in tokens if t in self._gone]


def notifier(db: FakeDb, push: Pushes) -> Notifier:
    return Notifier(Settings(use_emulators=True), db, push=push, clock=lambda: NOW)


# ── The notifier ──────────────────────────────────────────────────────────────


def test_an_approved_owner_is_listed_and_pushed_to() -> None:
    db, push = FakeDb(), Pushes()
    db.user("alice")
    db.device("alice", "d1", "tok-1")

    sent = notifier(db, push).notify(
        NotificationKind.JOB_FAILED,
        title="Clipping failed",
        body="no audio",
        link="/jobs/j1",
        to=["alice"],
    )

    assert sent == 1
    assert db.listed("alice")[0]["title"] == "Clipping failed"
    assert push.sent[0]["tokens"] == ["tok-1"]
    assert push.sent[0]["link"] == "/jobs/j1"


@pytest.mark.parametrize("status", ["PENDING", "DISABLED", None])
def test_nobody_unapproved_is_told_anything(status: str | None) -> None:
    db, push = FakeDb(), Pushes()
    if status:
        db.user("bob", status=status)
        db.device("bob", "d1", "tok")

    assert notifier(db, push).notify(NotificationKind.JOB_FAILED, title="t", to=["bob"]) == 0
    assert push.sent == []
    assert db.listed("bob") == []


def test_a_kind_switched_off_is_neither_pushed_nor_listed() -> None:
    db, push = FakeDb(), Pushes()
    db.user("alice")
    db.device("alice", "d1", "tok")
    db.docs["users/alice/settings/notifications"] = {"kinds": {"CLIPS_READY": False}}

    assert notifier(db, push).notify(NotificationKind.CLIPS_READY, title="t", to=["alice"]) == 0
    assert push.sent == []


def test_a_kind_nobody_touched_takes_its_default_and_a_test_is_always_on() -> None:
    assert is_on(NotificationKind.JOB_FAILED, None) is True
    assert is_on(NotificationKind.TEST, {"TEST": False}) is True


def test_an_admin_kind_goes_to_approved_admins_only() -> None:
    db, push = FakeDb(), Pushes()
    db.user("boss", role="ADMIN")
    db.user("gone", role="ADMIN", status="DISABLED")
    db.user("member")

    assert notifier(db, push).notify(NotificationKind.WORKER_DOWN, title="t", to=["member"]) == 1
    assert db.listed("boss") and not db.listed("member") and not db.listed("gone")


def test_a_device_cloud_messaging_has_forgotten_is_removed() -> None:
    db, push = FakeDb(), Pushes(gone=("tok-old",))
    db.user("alice")
    db.device("alice", "old", "tok-old")
    db.device("alice", "new", "tok-new")

    notifier(db, push).notify(NotificationKind.TEST, title="t", to=["alice"])

    assert "users/alice/devices/old" not in db.docs
    assert "users/alice/devices/new" in db.docs


def test_the_same_fold_is_one_entry_not_two() -> None:
    db, push = FakeDb(), Pushes()
    db.user("alice")
    n = notifier(db, push)

    n.notify(NotificationKind.JOB_FAILED, title="first", to=["alice"], fold="job-1")
    n.notify(NotificationKind.JOB_FAILED, title="again", to=["alice"], fold="job-1")

    assert [item["title"] for item in db.listed("alice")] == ["again"]


def test_a_push_that_fails_never_reaches_the_caller() -> None:
    db = FakeDb()
    db.user("alice")
    db.device("alice", "d1", "tok")

    assert (
        notifier(db, Pushes(fail=True)).notify(NotificationKind.TEST, title="t", to=["alice"]) == 0
    )


# ── What a finished job says ──────────────────────────────────────────────────


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def notify(self, kind: NotificationKind, **kwargs: Any) -> int:
        self.calls.append({"kind": kind, **kwargs})
        return 1


def job(job_type: str, status: str, **extra: Any) -> Job:
    return Job.model_validate(
        {
            "id": "j1",
            "uid": "alice",
            "type": job_type,
            "status": status,
            "stages": [{"name": "ECHO_ONE", "status": "DONE", "lane": "CPU"}],
            "attempts": 1,
            "maxAttempts": 3,
            "createdAt": NOW.isoformat(),
            "updatedAt": NOW.isoformat(),
            **extra,
        }
    )


def test_a_failed_job_tells_its_owner_why() -> None:
    record = Recorder()
    failed = job(
        "CLIP", "FAILED", error={"type": "X", "message": "no audio track", "retryable": False}
    )

    on_job_end(failed, record, lambda _id: 0)  # type: ignore[arg-type]

    assert [c["kind"] for c in record.calls] == [NotificationKind.JOB_FAILED]
    assert record.calls[0]["to"] == ["alice"]
    assert record.calls[0]["body"] == "no audio track"
    assert record.calls[0]["link"] == "/jobs/j1"


def test_a_publish_that_failed_on_sign_in_also_tells_the_admins() -> None:
    record = Recorder()
    failed = job(
        "PUBLISH",
        "FAILED",
        clipId="c1",
        error={
            "type": "X",
            "message": "invalid_grant: Token has been expired or revoked.",
            "retryable": False,
        },
    )
    assert wants_youtube_sign_in(failed)

    on_job_end(failed, record, lambda _id: 0)  # type: ignore[arg-type]

    assert [c["kind"] for c in record.calls] == [
        NotificationKind.JOB_FAILED,
        NotificationKind.YOUTUBE_SIGN_IN,
    ]


def test_clips_ready_says_how_many_and_nothing_when_there_are_none() -> None:
    record = Recorder()
    on_job_end(job("CLIP", "COMPLETED"), record, lambda _id: 3)  # type: ignore[arg-type]
    on_job_end(job("COMPOSE", "COMPLETED"), record, lambda _id: 0)  # type: ignore[arg-type]

    assert len(record.calls) == 1
    assert record.calls[0]["title"] == "3 clips are ready to review"


def test_a_published_clip_says_so() -> None:
    record = Recorder()
    on_job_end(job("PUBLISH", "COMPLETED", clipId="c9"), record, lambda _id: 0)  # type: ignore[arg-type]
    assert record.calls[0]["kind"] is NotificationKind.PUBLISHED
    assert record.calls[0]["link"] == "/publish/c9"


# ── What the agent notices ────────────────────────────────────────────────────


def notices(tmp_path: Path, record: Recorder, clock: list[datetime]) -> AgentNotices:
    return AgentNotices(
        record,  # type: ignore[arg-type]
        None,
        state_path=tmp_path / "notify-state.json",
        clock=lambda: clock[0],
    )


def test_a_job_waiting_with_no_worker_is_told_once_after_ten_minutes(tmp_path: Path) -> None:
    record, clock = Recorder(), [NOW]
    watch = notices(tmp_path, record, clock)
    watch.record_queued("j1", "alice", "CLIP", NOW)

    watch.step(AgentState.STOPPED, None)
    clock[0] = NOW + WAIT_BEFORE_TELLING + timedelta(seconds=1)
    watch.step(AgentState.STOPPED, None)
    watch.step(AgentState.STOPPED, None)

    assert [c["kind"] for c in record.calls] == [NotificationKind.JOB_WAITING]
    assert record.calls[0]["to"] == ["alice"]


def test_a_job_waiting_for_a_running_worker_is_not_news(tmp_path: Path) -> None:
    record, clock = Recorder(), [NOW + timedelta(hours=1)]
    watch = notices(tmp_path, record, clock)
    watch.record_queued("j1", "alice", "CLIP", NOW)

    watch.step(AgentState.RUNNING, None)

    assert record.calls == []


def test_a_worker_that_gives_up_is_told_once(tmp_path: Path) -> None:
    record = Recorder()
    watch = notices(tmp_path, record, [NOW])

    watch.step(AgentState.RUNNING, None)
    watch.step(AgentState.FAILED, "exit code 1, five times")
    watch.step(AgentState.FAILED, "exit code 1, five times")

    assert [c["kind"] for c in record.calls] == [NotificationKind.WORKER_DOWN]


def test_a_test_request_is_answered_once_even_across_a_restart(tmp_path: Path) -> None:
    request = {"notifyTestRequestedBy": "alice", "notifyTestRequestedAt": NOW.isoformat()}
    record = Recorder()
    notices(tmp_path, record, [NOW]).take(request)
    notices(tmp_path, record, [NOW]).take(request)

    assert [c["kind"] for c in record.calls] == [NotificationKind.TEST]
    assert record.calls[0]["to"] == ["alice"]


def test_a_sign_up_is_announced_once_even_across_a_restart(tmp_path: Path) -> None:
    class Snap:
        id = "newbie"

        def to_dict(self) -> dict[str, Any]:
            return {"email": "newbie@example.com", "status": "PENDING"}

    record = Recorder()
    notices(tmp_path, record, [NOW])._on_pending([Snap()], None, None)
    notices(tmp_path, record, [NOW])._on_pending([Snap()], None, None)

    assert [c["kind"] for c in record.calls] == [NotificationKind.SIGNUP]
    assert "newbie@example.com" in record.calls[0]["body"]
