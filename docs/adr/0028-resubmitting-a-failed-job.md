# ADR-0028: Resubmitting a failed job

**Status.** Accepted, 2026-09-26.

**Context.** A worker started by the desktop app outlived the app and lost its output pipe. Every
thread in it died at its next log line (fixed separately: the worker now falls back to
`~/.clipforge/worker.log`). Two jobs were stranded, and once a healthy worker came back the reaper
did what it should: it failed both, because each had spent its attempts. Neither had done any work.
One was a REMAKE with a single attempt, and the other was a PUBLISH that never reached YouTube.

Getting them back meant starting over. The reviewer had to find the clip, reopen the remake panel
and type the notes again, or find the approved clip and press Publish again. For a CLIP job that
failed at RENDER, starting over also means downloading and transcribing again: twenty minutes of
work that the failed job had already checkpointed. The operator asked for a way to resubmit a failed
job instead of creating a new one.

**Decision.**

*The same job goes back to the queue.* No new job, no copy. `lease.resubmit` and the PWA's
`resubmission()` make the same write: `status` FAILED → QUEUED, `attempts` to 0, and `error`,
`startedAt` and `endedAt` cleared. `resubmittedAt` and `updatedAt` are set to now. The options are
kept because a resubmission is the request that failed, not a new one. The stages are kept because
that is the point: a stage that is DONE is never re-executed (§3.3), so the job resumes at the stage
that died. A stage left FAILED, or RUNNING by a reaped attempt, needs no reset: the runner already
re-runs anything that is not DONE or SKIPPED, which is how a retryable failure has always worked.

*The attempt budget starts again.* The budget exists so a job whose worker keeps dying is not retried
forever without anyone deciding to. A person pressing Resubmit is that decision.

*FAILED only, not CANCELLED.* A worker releases a job on its way to FAILED, since both `fail_stage`
and `reap` clear `workerId` and the lease, so a failed job is never held. Cancelling is different.
It is a client write that touches only `status`, and a worker running the job notices only at its
next checkpoint. It then writes the document back with `batch.set`. Resubmitting a job cancelled
mid-stage could put it in the queue while the original run was still finishing. Cancelled jobs can
be allowed later for the case that is safe, cancelled while QUEUED (`workerId == null`), if that
proves wanted.

*A client-driven transition, like cancelling.* `firestore.rules` gains a second allowed update on
`jobs/{jobId}`, `resubmissionOk()`. It checks:

- the job is FAILED before the write and QUEUED after it;
- it touches only the seven fields above;
- `attempts` is 0, and the error and both timestamps are null;
- no worker or lease is claimed;
- the clip is re-checked where one is named. It must still exist for MUSIC, UPLOAD and REMAKE, and
  for PUBLISH it must still be approved.

The options need no re-validation because they cannot change. `stages` is not writable: a client
that could write it could mark as DONE a stage that never ran.

*The event log records it through the worker.* Clients cannot write the event log, so a resubmitted
job would otherwise show FAILED followed by CLAIMED with nothing to explain how. `resubmit` clears
`startedAt` and every claim sets it. So `claim` treats a job that has a `resubmittedAt` and no
`startedAt` as the first claim since a resubmission, and says so: *CLAIMED · resubmitted after it
failed*. The same line appears whether the job was resubmitted from the phone or from
`clipforge-worker resubmit`.

*Where it is offered.* On the Jobs page, a Resubmit button on every failed card, next to Delete.
On a job's own page, a button under the diagnosis that says which stage it will pick up at. From the
command line, `clipforge-worker resubmit JOB_ID`, which runs the same transition in a transaction.
There is no confirmation: resubmitting loses nothing, and a mistaken one can be cancelled.

**Consequences.**

- `Job` gains `resubmittedAt`. Workers from before this change ignore it
  (`test_forward_compatibility.py`), and older PWA copies ignore it too, including the one embedded
  in the desktop app. The rules change only adds permissions, so an old client loses nothing. The
  desktop app shows the button only after its next rebuild.
- Resubmitting a PUBLISH can publish. That is what the button is for, and the PUBLISH stage was
  already idempotent against its own earlier attempts. It finds an existing publication, resumes a
  checkpointed upload session, and asks YouTube whether an earlier attempt landed. The rule refuses
  a clip that has been un-approved since.
- A job that failed for a reason that has not changed fails again. Resubmit does not know why a job
  failed, and the diagnosis on the job page says whether retrying could help (non-retryable
  failures are named there).
