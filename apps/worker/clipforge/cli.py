"""Worker command-line entrypoint."""

from __future__ import annotations

import json

import typer

from clipforge.config import get_settings
from clipforge.observability import configure_logging
from clipforge.version import __version__

app = typer.Typer(
    name="clipforge-worker",
    help="ClipForge local worker.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the worker version and exit."""
    typer.echo(__version__)


@app.command()
def run(
    once: bool = typer.Option(
        default=False,
        help="Claim and run a single job, then exit. Useful for smoke tests.",
    ),
) -> None:
    """Start the worker: claim jobs, run their stages, heartbeat, and reap.

    Stops cleanly on Ctrl-C — running stages are asked to checkpoint, their jobs
    are returned to the queue so another worker can take them immediately, and
    the heartbeat flips to OFFLINE.
    """
    from clipforge.media.workspace import Workspace
    from clipforge.scheduler.worker import Worker
    from clipforge.stages.pipeline import build_registry_factory
    from clipforge.store.blobs import build_blob_store
    from clipforge.store.firestore import (
        CandidateStore,
        ClipStore,
        JobStore,
        SourceStore,
        WorkerStore,
        firestore_client,
    )
    from clipforge.store.transcripts import TranscriptArchive, TranscriptStore

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    client = firestore_client(settings)
    workspace = Workspace(settings.workspace_dir, max_gb=settings.workspace_max_gb)
    # Anything left in tmp/ belongs to a run that is already over.
    workspace.clear_tmp()

    worker = Worker(
        settings=settings,
        jobs=JobStore(client, settings),
        workers=WorkerStore(client, settings),
        registry_factory=build_registry_factory(
            settings=settings,
            sources=SourceStore(client, settings),
            workspace=workspace,
            transcripts=TranscriptStore(client, settings),
            archive=TranscriptArchive(workspace.transcripts_dir),
            candidates=CandidateStore(client, settings),
            clips=ClipStore(client, settings),
            blobs=build_blob_store(settings),
        ),
    )
    worker.install_signal_handlers()

    if once:
        finished = worker.run_once()
        typer.echo(
            "no queued jobs" if finished is None else f"{finished.id} {finished.status.value}"
        )
        raise typer.Exit(code=0)

    worker.run_forever()


@app.command()
def submit(
    submission: str = typer.Argument(
        "", help="A YouTube URL or a local media file. Omit to enqueue an ECHO job."
    ),
    uid: str = typer.Option("local", help="Owning user id."),
    job_id: str = typer.Option("", help="Explicit job id; generated when omitted."),
) -> None:
    """Enqueue a job.

    With a submission, this is a real CLIP job. Without one it is an ECHO job —
    three no-op stages that exercise the scheduler without touching any media.
    """
    from clipforge.media.workspace import Workspace
    from clipforge.stages.echo import new_echo_job
    from clipforge.stages.pipeline import build_clip_stages, new_clip_job
    from clipforge.store.firestore import JobStore, SourceStore, firestore_client

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    client = firestore_client(settings)

    if submission:
        job = new_clip_job(
            uid=uid,
            submission=submission,
            job_id=job_id or None,
            stages=build_clip_stages(
                settings=settings,
                sources=SourceStore(client, settings),
                workspace=Workspace(settings.workspace_dir, max_gb=settings.workspace_max_gb),
            ),
        )
    else:
        job = new_echo_job(uid=uid, job_id=job_id or None)

    JobStore(client, settings).create(job)
    typer.echo(job.id)


@app.command()
def status(job_id: str = typer.Argument(..., help="Job id to inspect.")) -> None:
    """Print a job document and its event log as JSON."""
    from clipforge.store.firestore import JobStore, firestore_client

    settings = get_settings()
    store = JobStore(firestore_client(settings), settings)

    job = store.get(job_id)
    if job is None:
        typer.echo(f"no such job: {job_id}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        json.dumps(
            {
                "job": job.model_dump(by_alias=True, mode="json"),
                "events": [{k: str(v) for k, v in event.items()} for event in store.events(job_id)],
            },
            indent=2,
        )
    )


@app.command()
def workspace() -> None:
    """Report workspace usage against its cap.

    Clips never leave this machine on the free tier, so the disk budget is a
    first-class operational concern rather than an implementation detail.
    """
    from clipforge.media.workspace import Workspace as WorkspaceManager

    settings = get_settings()
    manager = WorkspaceManager(settings.workspace_dir, max_gb=settings.workspace_max_gb)
    used = manager.used_bytes()
    gb = 1024**3

    typer.echo(f"{manager.root}")
    typer.echo(f"  used   {used / gb:.2f} GB of {settings.workspace_max_gb} GB cap")
    typer.echo(f"  free   {manager.free_disk_bytes() / gb:.2f} GB on the volume")
    over = manager.over_budget_by()
    typer.echo(f"  over budget by {over / gb:.2f} GB" if over else "  within budget")


@app.command()
def gpu() -> None:
    """Report what is currently holding VRAM.

    Phase 0 found an unrelated Ollama session holding 4.8 GB of a 6 GB card. This
    is the one-command answer to "why did my job refuse to start?".
    """
    from clipforge.models.vram import probe_vram

    snapshot = probe_vram()
    if snapshot is None:
        typer.echo("no NVIDIA device detected")
        raise typer.Exit(code=0)

    settings = get_settings()
    typer.echo(f"{snapshot.device_name}")
    typer.echo(f"  total {snapshot.total_mb} MiB · free {snapshot.free_mb} MiB")
    typer.echo(
        f"  budget after {settings.vram_reserve_mb} MiB reserve: "
        f"{snapshot.budget_mb(settings.vram_reserve_mb)} MiB"
    )
    for process in snapshot.processes:
        typer.echo(f"  holding: {process.describe()}")


if __name__ == "__main__":
    app()
