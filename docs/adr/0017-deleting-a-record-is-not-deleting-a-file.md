# ADR-0017 — Deleting a record is not deleting a file

- **Status:** Accepted
- **Date:** 2026-09-14
- **Phase:** 8f

## Context

The database accumulates. Every harvest leaves candidates nobody looked at,
every correction leaves an older version, every run leaves a job. None of it was
deletable from anywhere: `clips`, `sources` and `candidates` were all
`allow delete: if false`, on the reasonable principle that a pipeline's own
output is not the client's to destroy.

That principle protected the wrong thing. What actually needs protecting is the
**media** — a source is a download that may no longer be available and a clip is
a render that cost GPU time — and the records were being guarded as though they
were the expensive part.

## Decision

**Two deletes, on two surfaces, that never imply each other.**

### Records go from the web app

`clips`, `sources` and `candidates` become deletable by any approved member, and
`jobs` already were. Deleting a source cascades to the candidates and clips cut
from it, in batches, from the client — rules see one document at a time and
cannot express "and its children", so the cascade lives in `deleteSource` where
it can be, and the worker tolerates an orphan either way.

The confirmation counts first. *"Delete this source"* and *"delete this source,
eleven clips and forty candidates"* are different decisions, and only one of
them was on the button.

**Publications are never deleted.** They record what was actually posted and
under what rights. An audit trail that disappears when somebody tidies their
queue is not an audit trail.

**Preferences are still never deleted**, unchanged from ADR-0014: a rejection is
itself the thing worth remembering, and removing the row would let the same
suggestion come back on the next correction.

### Files go from the machine that holds them

A new loopback surface — `GET /storage`, `POST /storage/trash`, `.../restore`,
`.../purge` — because nothing in Firestore knows what is on a particular disk.
The worker is the only thing that does.

It follows that this surface can list files whose records are already gone, and
that is the point rather than a side effect: deleting a record leaves a file
nobody is tracking, and a storage screen built from Firestore could never show
it. The listing walks the disk, so what it shows is what is there.

It also follows that the screen only works where the files are. The local API
answers on loopback and hands out its token through Tauri, so **Settings ▸
Storage** is functional in the desktop app and inert in a browser on a phone.
That is correct rather than a limitation: a phone cannot free space on a
computer it is not.

### Removing a file puts it in a bin

`workspace/trash/<id>/` holds the file under its own name beside an `item.json`
recording where it came from. A directory per item because two users' clips can
share a file name, and because a sidecar beside the file cannot drift away from
it. The directory listing IS the index — no database — so a bin survives the
worker being killed mid-move.

Nothing empties it on a schedule. It is emptied when somebody empties it.

## The bin is outside the disk budget, and that is the interesting decision

`Workspace.used_bytes` skips the trash directory. The opposite is the obvious
choice — the bytes are on the disk, so count them — and it is catastrophic in
practice.

`Workspace.collect` evicts **sources**. If binned bytes counted toward the cap,
a full bin would push the workspace over budget and the collector would respond
by deleting live downloads to make room for deleted ones. Live data must never
be evicted to house dead data.

The price is real and is paid deliberately: the bin can fill a disk while the
workspace reports itself comfortable. So its size is reported separately by
`GET /storage` and shown on the Storage screen next to the button that empties
it, which is the only defence a design like this can offer.

## Guards

The token and the origin check in `localapi.py` protect the *port*. These
protect the *argument*, which is a separate job — a caller holding a valid token
is still not entitled to name `C:/Windows/System32` and have the worker move it.

- Every path is resolved and refused unless it lands inside the workspace, so
  `..` in a segment reaches nothing.
- A trash id is resolved and refused unless its parent is the bin itself.
  `purge` is a recursive delete and its id arrives over HTTP.
- Something already in the bin cannot be re-binned: a second manifest would
  point at a path inside the first, and restoring the pair in the wrong order
  loses the file.
- `restore` refuses to overwrite. Re-running the job that made a clip writes the
  same path, so the file in the bin and the file on disk are two different
  renders and only the operator knows which they want.
- Emptying the bin takes an explicit `all: true`. An empty id list is what a
  buggy client sends by accident, and emptying must never be the accident.

## Consequences

**A deleted record leaves an orphaned file, on purpose.** That is the shape that
was asked for, and the Storage screen is where it becomes visible — it lists what
is on disk, not what Firestore remembers, so an orphan shows up as an ordinary
row.

**The workspace collector still runs.** A source nobody deleted is still evicted
when the cap demands it, unchanged. The bin is for files somebody chose to
remove; the collector is for files nobody did.

**Nothing published is lost.** A clip's record can go while its publications
stay, which reads oddly in a raw database dump and is the correct trade: the
question "what did I post, and was I allowed to" must remain answerable after
the queue has been tidied.

## Alternatives considered

**Send files to the Windows Recycle Bin.** Literally what "system trash" means,
and it would let Explorer restore them. Rejected because the app then cannot
list or purge its own items without shell APIs, and because the Recycle Bin
auto-purges on size — which contradicts "keep them forever" precisely when the
bin is large enough for that to matter.

**Delete the file with the record.** Simplest, and wrong for the reason at the
top: the two decisions have different costs and different frequencies, and
tying them makes the cheap one carry the expensive one's risk.

**A `DELETE` job on the worker.** Would let a phone free space on the desktop,
and would make every tidy-up wait on a queue that only moves when the worker
runs. Deleting something should not be able to sit QUEUED.
