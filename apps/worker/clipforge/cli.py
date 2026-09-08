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
    from clipforge.localserver import LocalFileServer
    from clipforge.media.workspace import Workspace
    from clipforge.scheduler.worker import Worker
    from clipforge.stages.pipeline import build_registry_factory
    from clipforge.store.blobs import build_blob_store
    from clipforge.store.firestore import (
        CandidateStore,
        ClipStore,
        JobStore,
        PublicationStore,
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
            publications=PublicationStore(client, settings),
            blobs=build_blob_store(settings),
        ),
    )
    worker.install_signal_handlers()

    # Playback branch 2: with no Cloud Storage there is no URL a phone can play,
    # but the PWA opened on THIS machine can play a clip if something serves it.
    # See docs/adr/0009-spark-tier-local-artefacts.md.
    file_server: LocalFileServer | None = None
    if settings.local_server_enabled:
        file_server = LocalFileServer(
            workspace.root,
            host=settings.local_server_host,
            port=settings.local_server_port,
        )
        try:
            file_server.start()
        except OSError as exc:
            # A port clash must not stop the worker doing its actual job.
            typer.echo(f"local file server could not start: {exc}", err=True)
            file_server = None

    if once:
        finished = worker.run_once()
        typer.echo(
            "no queued jobs" if finished is None else f"{finished.id} {finished.status.value}"
        )
        raise typer.Exit(code=0)

    try:
        worker.run_forever()
    finally:
        if file_server is not None:
            file_server.stop()


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
    from clipforge.stages.echo import new_echo_job
    from clipforge.stages.pipeline import new_clip_job
    from clipforge.store.firestore import JobStore, firestore_client

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    client = firestore_client(settings)

    if submission:
        # The full pipeline shape, from CLIP_PIPELINE. Building it from whichever
        # stage implementations happened to be constructible here is what used to
        # produce jobs that downloaded a video and then stopped.
        job = new_clip_job(uid=uid, submission=submission, job_id=job_id or None)
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


@app.command(name="youtube-auth")
def youtube_auth(
    force: bool = typer.Option(
        default=False, help="Re-authorise even if a token is already stored."
    ),
) -> None:
    """Authorise ClipForge to upload to your YouTube channel.

    Run once, then again whenever the refresh token expires. While the OAuth
    consent screen is in "Testing" mode Google expires refresh tokens after
    **7 days**, and the YouTube upload scope is *sensitive*, so leaving Testing
    needs Google verification. Re-running this is the supported path until then.
    """
    import secrets
    import webbrowser

    from clipforge.publish.credentials import TokenStore
    from clipforge.publish.oauth import authorization_url, exchange_code, listen_for_redirect
    from clipforge.publish.youtube import load_client_secrets

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    store = TokenStore(settings.youtube_token_store)
    if store.exists() and not force:
        typer.echo(f"already authorised ({store.path}). Re-run with --force to replace it.")
        raise typer.Exit(code=0)

    client_id, client_secret = load_client_secrets(settings.youtube_client_secrets)
    redirect_uri = f"http://127.0.0.1:{settings.youtube_auth_port}/"
    state = secrets.token_urlsafe(24)
    url, verifier = authorization_url(client_id=client_id, redirect_uri=redirect_uri, state=state)

    typer.echo("Opening your browser to authorise ClipForge.")
    typer.echo("If it does not open, visit this URL yourself:")
    typer.echo(f"\n  {url}\n")
    webbrowser.open(url)

    result = listen_for_redirect(settings.youtube_auth_port)
    if not secrets.compare_digest(result.state, state):
        # A mismatched state means the redirect did not come from the request we
        # made. Refusing is the entire point of sending one.
        typer.echo("the redirect did not match this request; nothing was stored", err=True)
        raise typer.Exit(code=1)

    tokens = exchange_code(
        code=result.code,
        verifier=verifier,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    store.save(tokens)

    typer.echo(f"authorised; token stored at {store.path}")
    typer.echo("This file is encrypted to this machine and this user, and is never")
    typer.echo("written to Firestore. Do not copy it into the repository or a backup.")


@app.command()
def publish(
    clip_id: str = typer.Argument(..., help="The approved clip to publish."),
    uid: str = typer.Option("local", help="Owning user id."),
    at: str = typer.Option("", help="ISO-8601 time to publish at; immediate when omitted."),
) -> None:
    """Queue an approved, attested clip for publishing.

    This only enqueues. The rights gate is checked by the worker when the job
    runs, not here — a check in the CLI would be advisory, since the worker is
    what actually holds the credentials and performs the upload.
    """
    from datetime import datetime as _datetime

    from clipforge.stages.pipeline import new_publish_job
    from clipforge.store.firestore import ClipStore, JobStore, firestore_client

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    client = firestore_client(settings)

    clip = ClipStore(client, settings).get(clip_id)
    if clip is None:
        typer.echo(f"no such clip: {clip_id}", err=True)
        raise typer.Exit(code=1)

    # Reported early as a courtesy, so a refusal is visible now rather than in a
    # failed job later. The worker checks again regardless.
    from clipforge.publish.rights import check_publishable

    refusal = check_publishable(clip, publishing_enabled=settings.publishing_enabled)
    if refusal is not None:
        typer.echo(f"{refusal.code}: {refusal.message}", err=True)
        raise typer.Exit(code=1)

    publish_at = _datetime.fromisoformat(at) if at else None
    job = new_publish_job(uid=uid, clip_id=clip_id, publish_at=publish_at)
    JobStore(client, settings).create(job)
    typer.echo(job.id)


@app.command()
def quota() -> None:
    """Report today's YouTube quota and how many uploads it still allows.

    The API offers no endpoint for remaining quota, so this reports what this
    worker has spent. It is the answer to "can I publish today?" — which
    otherwise only becomes clear when the seventh upload fails.
    """
    from clipforge.publish.credentials import TokenStore
    from clipforge.publish.youtube import (
        DAILY_QUOTA_UNITS,
        UPLOAD_QUOTA_UNITS,
        QuotaLedger,
    )

    settings = get_settings()
    ledger = QuotaLedger.today()

    typer.echo("publishing " + ("enabled" if settings.publishing_enabled else "DISABLED"))
    if TokenStore(settings.youtube_token_store).exists():
        typer.echo("  credentials present")
    else:
        typer.echo("  credentials missing. Run: clipforge-worker youtube-auth")
    typer.echo(f"  quota {ledger.used_units} of {DAILY_QUOTA_UNITS} units used today")
    typer.echo(f"  an upload costs {UPLOAD_QUOTA_UNITS} units")
    typer.echo(f"  about {ledger.uploads_remaining} uploads remaining today")


if __name__ == "__main__":
    app()
