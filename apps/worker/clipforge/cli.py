"""Worker command-line entrypoint."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

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
    from clipforge.localapi import LocalControlApi, read_or_create_token
    from clipforge.localserver import LocalFileServer
    from clipforge.media.trash import Trash
    from clipforge.media.workspace import Workspace
    from clipforge.publish.channels import ChannelRoutes
    from clipforge.scheduler.control import WorkerRoutes
    from clipforge.scheduler.storage import StorageRoutes
    from clipforge.scheduler.worker import Worker
    from clipforge.stages.pipeline import build_registry_factory
    from clipforge.store.blobs import build_blob_store
    from clipforge.store.channels import ChannelStore
    from clipforge.store.firestore import (
        CandidateStore,
        ClipStore,
        JobStore,
        PreferenceStore,
        PublicationStore,
        SourceStore,
        TrendStore,
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
        # Housekeeping: collects the clips a review decision has already
        # settled, on the reaper's cadence. Both stores rather than one, because
        # the record and the file are removed separately and deliberately.
        clips=ClipStore(client, settings),
        trash=Trash(workspace.trash_dir),
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
            # Where a RESEARCH run writes its ranked list, and what CURATE reads
            # back to explain it.
            trends=TrendStore(client, settings),
            # Read by the PUBLISH stage for a channel's standing defaults. The
            # same store the local API writes them through, so what the settings
            # page saved is what the next publish uses.
            channels=ChannelStore(client, settings),
            # What this reviewer has already taught. Read by REMAKE before it
            # decides anything, and written to afterwards — as proposals only,
            # which do nothing until a human accepts them.
            preferences=PreferenceStore(client, settings),
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

    # The desktop app's way of handing over a YouTube client secret without it
    # ever reaching Firestore, and of stopping this worker cleanly. Loopback
    # only, bearer-token authenticated.
    # See docs/adr/0011-local-control-api.md.
    control: LocalControlApi | None = None
    if settings.local_api_enabled:
        routes = {
            **ChannelRoutes(settings, ChannelStore(client, settings)).table(),
            **WorkerRoutes(worker, pid=os.getpid()).table(),
            # Seeing and clearing what is on THIS disk. Firestore knows which
            # clips exist; only the worker knows which files do, and a record
            # deleted last week leaves a file nothing else can even list.
            **StorageRoutes(workspace).table(),
        }
        control = LocalControlApi(
            routes,
            token=read_or_create_token(settings.local_api_token_file),
            port=settings.local_api_port,
        )
        try:
            control.start()
        except OSError as exc:
            # A port clash must not stop the worker doing its actual job, the
            # same reasoning as the file server above.
            typer.echo(f"local control API could not start: {exc}", err=True)
            control = None

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
        if control is not None:
            control.stop()


@app.command()
def agent(
    live: bool = typer.Option(
        default=False,
        help="Watch the real Firebase project rather than the local emulators.",
    ),
    log_file: str = typer.Option(
        default="",
        help="Append this process's log here. Defaults to CLIPFORGE_AGENT_LOG_FILE when "
        "there is no console to write to.",
    ),
) -> None:
    """Start and stop the worker on this machine on request, from anywhere.

    The half of ClipForge that lets a phone do more than ask. It holds a listener
    on `agents/{workerId}`, starts a worker when that document says RUNNING,
    stops it cleanly when it says STOPPED, and restarts one that dies while it is
    still wanted. Nothing here runs a job — that is still the worker's business.

    Runs until stopped. Install it to start with Windows with:

        powershell -File tools/agent.ps1 -Install -Live
    """
    # Before `get_settings()`, which is cached: a real environment variable beats
    # `.env` in pydantic-settings, and `.env` deliberately pins the emulator so
    # routine development cannot touch the real project. Going live is therefore
    # an argument here, exactly as it is in tools/worker.ps1.
    if live:
        os.environ["CLIPFORGE_USE_EMULATORS"] = "false"

    from clipforge.agent import (
        Agent,
        Supervisor,
        WorkerProcess,
        probe_worker,
        redirect_output,
    )
    from clipforge.store.firestore import AgentStore, firestore_client

    settings = get_settings()

    # A console when there is one, a file when there is not. `sys.stdout` is
    # None under pythonw.exe, which is how Task Scheduler starts this, and
    # configure_logging would fail on it a line later.
    if log_file or sys.stdout is None:
        written_to = redirect_output(Path(log_file) if log_file else settings.agent_log_file)
    else:
        written_to = None

    configure_logging(level=settings.log_level, fmt=settings.log_format)

    # httpx logs one INFO line per request, and the agent asks the worker's
    # control API how it is doing every ten seconds — a log file that rotates on
    # nothing but its own chatter is a log file that has lost the failure it was
    # kept for. Quietened here rather than in configure_logging: the worker's
    # requests are rare and worth seeing.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    process = WorkerProcess(settings=settings, log_lines=settings.agent_log_lines)
    supervisor = Supervisor(
        process=process,
        probe=lambda: probe_worker(settings),
        control_port=settings.local_api_port,
        restart_limit=settings.agent_restart_limit,
        backoff_seconds=settings.agent_restart_backoff_seconds,
        shutdown_grace_seconds=settings.agent_shutdown_grace_seconds,
    )
    runner = Agent(
        settings=settings,
        store=AgentStore(firestore_client(settings), settings),
        supervisor=supervisor,
        tick_seconds=settings.agent_tick_seconds,
        heartbeat_seconds=settings.agent_heartbeat_seconds,
    )
    runner.install_signal_handlers()

    if written_to is None:
        typer.echo(
            f"agent {settings.worker_id} watching "
            f"{'the local emulators' if settings.use_emulators else settings.firebase_project_id}"
            " — Ctrl-C to stop"
        )
    runner.run()


@app.command()
def submit(
    submission: str = typer.Argument(
        "", help="A YouTube URL or a local media file. Omit to enqueue an ECHO job."
    ),
    uid: str = typer.Option(
        "local",
        help=(
            "Who to attribute the job to. The default is a placeholder that matches no "
            "Firebase account — the job still runs and, in a shared workspace, is still "
            "visible to everyone, but it is credited to nobody. `clipforge-worker user "
            "list` prints the real ids."
        ),
    ),
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
def remake(
    clip_id: str = typer.Argument(..., help="The clip to correct."),
    notes: str = typer.Option("", help="What is wrong with it, in your own words."),
    framing: str = typer.Option(
        "", help="AS_RENDERED, FIT, PAN or TRACK. Empty leaves the framing alone."
    ),
    crop: str = typer.Option("", help="left, centre or right. AS_RENDERED only."),
    language: str = typer.Option("", help="Speak it in this language, e.g. es or fr-fr."),
    keep_audio: bool = typer.Option(
        default=False, help="Keep the original audio ducked under the narration."
    ),
    script: str = typer.Option("", help="Say this instead of the clip's own words."),
    hide: bool = typer.Option(
        default=False,
        help="Find logos, watermarks and burnt-in text in the footage and cover them.",
    ),
    start: float = typer.Option(0.0, help="Move the cut's start, in seconds. Negative is earlier."),
    end: float = typer.Option(0.0, help="Move the cut's end, in seconds. Positive is later."),
    uid: str = typer.Option("cli", help="Who is asking. Recorded on the job."),
) -> None:
    """Queue a correction to a finished clip.

    The same job the review screen creates, from a terminal. Useful for trying a
    framing mode against one clip without reaching for a phone — and the only
    way to drive a remake on a machine where the PWA is not set up.

    Nothing happens until a worker claims it: there is no server-side executor,
    so a job that stays QUEUED means `clipforge-worker run` is not running.
    """
    from clipforge_contracts import (
        Framing,
        FramingMode,
        ObscureOptions,
        RemakeOptions,
        SpeechMode,
        VoiceCaptions,
        VoiceOptions,
    )

    from clipforge.media.speech import DEFAULT_VOICES, SpeechError, kokoro_language
    from clipforge.stages.pipeline import new_remake_job
    from clipforge.store.firestore import JobStore, firestore_client

    settings = get_settings()

    framing_option = None
    if framing:
        try:
            mode = FramingMode(framing.upper())
        except ValueError:
            typer.echo(
                f"unknown framing {framing!r}; expected one of "
                f"{', '.join(m.value for m in FramingMode)}",
                err=True,
            )
            raise typer.Exit(code=2) from None
        if mode is FramingMode.PAN:
            # PAN needs points, and a terminal is the wrong place to set them
            # against footage you cannot see.
            typer.echo(
                "PAN needs keyframes, which are set while watching the clip. "
                "Use TRACK here, or set the points in the review screen.",
                err=True,
            )
            raise typer.Exit(code=2)
        framing_option = Framing(mode=mode, crop=crop or None)

    voice_option = None
    if language:
        try:
            resolved = kokoro_language(language)
        except SpeechError as exc:  # a language with no voice is a usage error here
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
        voice_option = VoiceOptions(
            mode=SpeechMode.BED if keep_audio else SpeechMode.REPLACE,
            voice=DEFAULT_VOICES.get(resolved, settings.speech_default_voice),
            language=language,
            translate=True,
            script=script or None,
            captions=VoiceCaptions.REBUILD,
        )

    options = RemakeOptions(
        notes=notes or None,
        framing=framing_option,
        voice=voice_option,
        start_delta_sec=start,
        end_delta_sec=end,
        # Only ever the search here. A rectangle is a thing you point at, and a
        # terminal cannot show you the frame to point at — so the flag asks the
        # worker to look, and what it finds is recorded on the clip where it can
        # be seen and kept.
        obscure=ObscureOptions(auto=True) if hide else None,
    )
    if (
        options.notes is None
        and framing_option is None
        and voice_option is None
        and not hide
        and not start
        and not end
    ):
        typer.echo(
            "nothing to change: give a note, a framing, a language, --hide or a nudge.",
            err=True,
        )
        raise typer.Exit(code=2)

    job = new_remake_job(uid=uid, clip_id=clip_id, options=options)
    JobStore(firestore_client(settings), settings).create(job)
    typer.echo(job.id)


@app.command()
def research(
    topic: list[str] = typer.Option(  # noqa: B008 - typer's declared-default idiom
        [], "--topic", "-t", help="What the channel is about. Repeatable; none means 'anything'."
    ),
    region: str = typer.Option("US", help="Two-letter country code for the trend feeds."),
    hours: int = typer.Option(48, help="How far back counts as 'now', 6-168."),
    videos: int = typer.Option(5, help="How many videos to look up per topic, 1-10."),
    limit: int = typer.Option(12, help="How many trends to keep, 1-30."),
    subreddit: list[str] = typer.Option(  # noqa: B008
        [], "--subreddit", help="Where on Reddit to look. Repeatable; empty uses the defaults."
    ),
    curate: bool = typer.Option(
        default=True, help="Put the ranked list to the local model for an angle per row."
    ),
    uid: str = typer.Option("cli", help="Who is asking. Recorded on the job."),
) -> None:
    """Queue a trend run: what is the web talking about, and which videos carry it.

    The same job the Trends page creates. It touches no media and nothing in it
    runs on its own — the result is a ranked list to pick from, on the Trends
    page or with `clipforge-worker trends JOB_ID`.
    """
    from clipforge_contracts import ResearchOptions
    from pydantic import ValidationError

    from clipforge.stages.pipeline import new_research_job
    from clipforge.store.firestore import JobStore, firestore_client

    settings = get_settings()
    try:
        options = ResearchOptions(
            topics=[t.strip() for t in topic if t.strip()],
            region=region.upper(),
            lookback_hours=hours,
            videos_per_topic=videos,
            max_trends=limit,
            subreddits=[s.strip().removeprefix("r/") for s in subreddit if s.strip()],
            curate=curate,
        )
    except ValidationError as exc:
        typer.echo(f"those options are out of range: {exc}", err=True)
        raise typer.Exit(code=2) from None

    job = new_research_job(uid=uid, options=options)
    JobStore(firestore_client(settings), settings).create(job)
    typer.echo(job.id)


@app.command()
def trends(job_id: str = typer.Argument(..., help="The RESEARCH job whose list to print.")) -> None:
    """Print a research run's ranked list, with the videos behind each row."""
    from clipforge.store.firestore import TrendStore, firestore_client

    settings = get_settings()
    rows = TrendStore(firestore_client(settings), settings).for_job(job_id)
    if not rows:
        typer.echo("no trends recorded for that job (has it finished?)")
        raise typer.Exit(code=1)
    for trend in rows:
        sources = ", ".join(sorted({s.source.value.lower() for s in trend.signals}))
        typer.echo(f"{trend.rank:>2}. [{trend.score:>3}] {trend.topic}  ({sources})")
        if trend.angle:
            typer.echo(f"      {trend.angle}")
        for video in trend.videos[:3]:
            views = f"{video.view_count:,} views" if video.view_count is not None else "views ?"
            typer.echo(f"      - [{video.score:>3}] {video.title[:70]}  {views}  {video.url}")


@app.command()
def compile(  # noqa: A001 - the verb is the command
    items: list[str] = typer.Argument(  # noqa: B008
        ..., help="Two or more YouTube URLs or local files, in the order they should appear."
    ),
    theme: str = typer.Option(..., help="What ties them together. The model looks for it."),
    title: str = typer.Option("", help="What to call the result. Defaults to the theme."),
    length: int = typer.Option(60, help="Target length in seconds, 20-180."),
    max_segment: int = typer.Option(20, help="Longest any one segment may be, 5-60."),
    captions: bool = typer.Option(default=True, help="Burn each segment's own captions."),
    title_card: bool = typer.Option(default=True, help="Open with the theme on a card."),
    fade: bool = typer.Option(default=True, help="Dip to black between segments."),
    uid: str = typer.Option("cli", help="Who is asking. Recorded on the job."),
) -> None:
    """Queue a compilation: one vertical clip cut from the best moment of each video.

    Give a window for an item as URL@START-END, e.g. `https://youtu.be/x@42-58`,
    to cut exactly there and skip the model for that one.
    """
    from clipforge_contracts import CompileItem, CompileOptions, CompileTransition
    from pydantic import ValidationError

    from clipforge.stages.pipeline import new_compile_job
    from clipforge.store.firestore import JobStore, firestore_client

    settings = get_settings()
    parsed: list[CompileItem] = []
    for raw in items:
        submission, _, window = raw.rpartition("@") if "@" in raw else ("", "", "")
        if submission and "-" in window:
            start_text, _, end_text = window.partition("-")
            try:
                parsed.append(
                    CompileItem(
                        submission=submission,
                        start_sec=float(start_text),
                        end_sec=float(end_text),
                    )
                )
                continue
            except ValueError:
                pass
        parsed.append(CompileItem(submission=raw))

    try:
        options = CompileOptions(
            theme=theme.strip(),
            title=title.strip() or None,
            items=parsed,
            target_duration_sec=length,
            max_segment_sec=max_segment,
            captions=captions,
            title_card=title_card,
            transition=CompileTransition.FADE if fade else CompileTransition.CUT,
        )
    except ValidationError as exc:
        typer.echo(f"that compilation cannot be made: {exc}", err=True)
        raise typer.Exit(code=2) from None

    job = new_compile_job(uid=uid, options=options)
    JobStore(firestore_client(settings), settings).create(job)
    typer.echo(job.id)


@app.command(name="fetch-voices")
def fetch_voices(
    force: bool = typer.Option(default=False, help="Download again even if the files are present."),
) -> None:
    """Download the Kokoro speech model, so a remake can change the voice.

    Kept out of the install because it is ~330 MB that a worker which never
    re-voices a clip has no use for. The model and its voice pack are Apache 2.0
    and are fetched from the kokoro-onnx release the `speech` extra pins.
    """
    import urllib.request

    release = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
    settings = get_settings()
    wanted = [
        (f"{release}/kokoro-v1.0.onnx", settings.speech_model_path.expanduser()),
        (f"{release}/voices-v1.0.bin", settings.speech_voices_path.expanduser()),
    ]

    for url, destination in wanted:
        if destination.is_file() and not force:
            typer.echo(f"have {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_suffix(destination.suffix + ".partial")
        typer.echo(f"fetching {url}")
        try:
            urllib.request.urlretrieve(url, staging)  # noqa: S310 - a fixed https release URL
        except OSError as exc:
            staging.unlink(missing_ok=True)
            typer.echo(f"could not fetch {url}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        staging.replace(destination)
        typer.echo(f"wrote {destination} ({destination.stat().st_size / 1_048_576:.0f} MB)")

    typer.echo("speech is ready; a remake can now change the voice or the language")


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
    uid: str = typer.Option(
        "local",
        help=(
            "Who to attribute the job to. The default is a placeholder that matches no "
            "Firebase account — the job still runs and, in a shared workspace, is still "
            "visible to everyone, but it is credited to nobody. `clipforge-worker user "
            "list` prints the real ids."
        ),
    ),
    at: str = typer.Option("", help="ISO-8601 time to publish at; immediate when omitted."),
) -> None:
    """Queue an approved clip for publishing.

    This only enqueues. The publish gate is checked by the worker when the job
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
    from clipforge.publish.gate import check_publishable

    refusal = check_publishable(clip, publishing_enabled=settings.publishing_enabled)
    if refusal is not None:
        typer.echo(f"{refusal.code}: {refusal.message}", err=True)
        raise typer.Exit(code=1)

    publish_at = _datetime.fromisoformat(at) if at else None
    job = new_publish_job(uid=uid, clip_id=clip_id, publish_at=publish_at)
    JobStore(client, settings).create(job)
    typer.echo(job.id)


@app.command()
def retention(
    apply: bool = typer.Option(
        default=False, help="Write the rule to the bucket. Without this, only report."
    ),
) -> None:
    """Show or apply the bucket's clip-retention rule.

    Clips are uploaded so a phone can review them and are meant to disappear
    shortly afterwards. The deletion is a Cloud Storage lifecycle rule rather
    than a scheduled job here, and that is deliberate: a task on the worker
    would only expire clips while the worker was running, which is precisely
    when an unattended bucket is not the thing to worry about. Google evaluates
    lifecycle rules server-side, for free, whether this machine is on or not.

    What the worker still owes the rule is agreement. `Clip.playbackExpiresAt`
    is stamped from ``CLIPFORGE_CLIP_RETENTION_DAYS`` at upload so the app can
    stop offering a video the bucket has collected — if the two numbers drift,
    the app either offers a dead object or hides a live one. This command is
    where they are compared, and `--apply` is how they are made to agree.
    """
    from google.cloud import storage as gcs  # type: ignore[attr-defined]

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    if not settings.firebase_storage_bucket:
        typer.echo(
            "No CLIPFORGE_FIREBASE_STORAGE_BUCKET is set, so there is no bucket to "
            "keep in check. Clips stay on this machine.",
            err=True,
        )
        raise typer.Exit(code=1)

    if settings.google_application_credentials:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.google_application_credentials

    bucket = gcs.Client().bucket(settings.firebase_storage_bucket)
    bucket.reload()

    wanted = [
        {
            "action": {"type": "Delete"},
            "condition": {"age": settings.clip_retention_days, "matchesPrefix": ["clips/"]},
        }
    ]
    current = list(bucket.lifecycle_rules)

    typer.echo(f"bucket     {settings.firebase_storage_bucket}")
    typer.echo(f"configured {settings.clip_retention_days} day(s), for objects under clips/")
    typer.echo(f"on bucket  {json.dumps(current) if current else 'no lifecycle rules'}")

    if not apply:
        agreed = (
            len(current) == 1
            and current[0].get("condition", {}).get("age") == settings.clip_retention_days
        )
        typer.echo("")
        typer.echo("in step" if agreed else "NOT in step — run with --apply")
        raise typer.Exit(code=0 if agreed else 1)

    bucket.lifecycle_rules = wanted
    bucket.patch()
    typer.echo("")
    typer.echo(f"applied: clips expire {settings.clip_retention_days} day(s) after upload")


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


@app.command(name="poll-metrics")
def poll_metrics(
    days: int = typer.Option(14, help="How many days back to fetch."),
    dry_run: bool = typer.Option(default=False, help="Fetch and report, write nothing."),
) -> None:
    """Fetch daily metrics for every published clip, and store them.

    Safe to run twice. Snapshot ids carry the day, so a second run of the same
    day overwrites rather than doubling a clip's views — and a day old enough
    that YouTube has stopped restating it is never rewritten at all.
    """
    from clipforge.analytics.poller import poll_publication
    from clipforge.analytics.youtube import AnalyticsClient
    from clipforge.publish.credentials import TokenStore
    from clipforge.publish.youtube import QuotaLedger, YouTubeClient, load_client_secrets
    from clipforge.store.firestore import MetricStore, PublicationStore, firestore_client

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    tokens = TokenStore(settings.youtube_token_store)
    if not tokens.exists():
        typer.echo("no YouTube credentials. Run: clipforge-worker youtube-auth", err=True)
        raise typer.Exit(code=1)

    client_id, client_secret = load_client_secrets(Path(settings.youtube_client_secrets))
    ledger = QuotaLedger.today()
    uploader = YouTubeClient(
        tokens=tokens, client_id=client_id, client_secret=client_secret, ledger=ledger
    )
    analytics = AnalyticsClient(
        access_token=uploader.access_token,
        scopes=tokens.load().scopes,
        ledger=ledger,
    )

    db = firestore_client(settings)
    publications = PublicationStore(db, settings)
    metrics = MetricStore(db, settings)

    published = publications.all_published()
    if not published:
        typer.echo("nothing published yet, so there is nothing to measure.")
        return

    typer.echo(f"polling {len(published)} publication(s), {days} day window")
    total_written = 0
    for publication in published:
        snapshots, outcome = poll_publication(analytics, publication, window_days=days)
        if outcome.error:
            typer.echo(f"  {outcome.publication_id}: {outcome.error}", err=True)
            continue
        written = 0 if dry_run else metrics.save_all(snapshots)
        total_written += written
        typer.echo(
            f"  {outcome.external_id}: {outcome.fetched_days} day(s) reported, "
            f"{outcome.retention_points} retention point(s), {written} written"
        )

    typer.echo(f"{total_written} snapshot(s) written" + (" (dry run)" if dry_run else ""))
    typer.echo(f"quota: {ledger.used_units} units used today")


@app.command()
def calibrate(
    uid: str = typer.Option(..., help="Who is recording this report. Does NOT filter it."),
    window: int = typer.Option(28, help="Days of history to include."),
    out: str = typer.Option(
        "docs/calibration-report.md", help="Where to write the Markdown report."
    ),
) -> None:
    """Ask whether the predicted scores predicted anything, and write it down.

    Reports the answer whatever it is. A correlation near zero is the result
    this phase was built to be able to state, and it is stated — with its
    confidence interval and its sample size — rather than quietly omitted in
    favour of whichever cohort happened to look interesting.

    Changes nothing. Fitted weights, when the sample is large enough to produce
    any, are a proposal for a human to put into configuration.
    """
    from datetime import UTC, datetime, timedelta

    from clipforge_contracts import ScoreWeights

    from clipforge.analysis.ranking import DEFAULT_WEIGHTS
    from clipforge.analytics.poller import build_facts
    from clipforge.analytics.report import build_report, render_markdown
    from clipforge.store.firestore import (
        CalibrationStore,
        CandidateStore,
        ClipStore,
        MetricStore,
        PublicationStore,
        firestore_client,
    )

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    db = firestore_client(settings)
    publications = PublicationStore(db, settings)
    clips = ClipStore(db, settings)
    candidates = CandidateStore(db, settings)
    metrics = MetricStore(db, settings)
    reports = CalibrationStore(db, settings)

    # The window is a filter, not a label. A report whose header names 28 days
    # and whose numbers cover all history is a report with a false scope.
    since = (datetime.now(UTC) - timedelta(days=window)).date()

    facts = []
    # Workspace-wide. `uid` above stamps who ran this, not whose clips count.
    for publication in publications.all_published():
        snapshots = metrics.for_publication(publication.id, since=since)
        if not snapshots:
            # No metrics means no evidence. Including it as a row of zeroes
            # would report "we published it and nobody watched" for a clip that
            # has simply never been polled.
            continue
        clip = clips.get(publication.clip_id)
        candidate = candidates.get(clip.candidate_id) if clip else None
        facts.append(
            build_facts(
                publication=publication,
                clip=clip,
                candidate=candidate,
                snapshots=snapshots,
            )
        )

    baseline = ScoreWeights(
        hook=DEFAULT_WEIGHTS.hook,
        curiosity=DEFAULT_WEIGHTS.curiosity,
        standalone=DEFAULT_WEIGHTS.standalone,
        emotion=DEFAULT_WEIGHTS.emotion,
        pacing=DEFAULT_WEIGHTS.pacing,
        shareability=DEFAULT_WEIGHTS.shareability,
    )
    generated = datetime.now(UTC)
    report = build_report(
        facts,
        uid=uid,
        report_id=generated.strftime("%Y%m%dT%H%M%SZ"),
        baseline=baseline,
        window_days=window,
        now=generated,
    )
    reports.save(report)

    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_markdown(report), encoding="utf-8")

    typer.echo(f"n = {report.n}")
    for correlation in report.correlations:
        typer.echo(f"  {correlation.outcome}: {correlation.coefficient:+.3f} (n={correlation.n})")
    if report.underpowered:
        typer.echo("  UNDERPOWERED — no conclusion is drawn from this sample.")
    typer.echo(f"written to {destination}")
    typer.echo(f"stored as calibrations/{report.id}")


@app.command()
def rescore(
    hook: float = typer.Option(0.25, help="Weight for the hook dimension."),
    curiosity: float = typer.Option(0.20, help="Weight for curiosity."),
    standalone: float = typer.Option(0.20, help="Weight for standalone comprehensibility."),
    emotion: float = typer.Option(0.15, help="Weight for emotion."),
    pacing: float = typer.Option(0.10, help="Weight for pacing."),
    shareability: float = typer.Option(0.10, help="Weight for shareability."),
    apply: bool = typer.Option(default=False, help="Write the new totals back."),
) -> None:
    """Re-rank every historical candidate under different weights, with no inference.

    This is decision D5 collecting on its promise: the model supplied judgement
    and Python supplied arithmetic, so a changed weighting re-ranks work already
    done without asking a model anything at all.

    Reports the movement before changing anything. Without ``--apply`` it is a
    read-only comparison, which is the form worth running first — a reweighting
    that reorders the top of the list is a different proposition from one that
    shuffles positions forty through sixty.
    """
    from clipforge.analysis.ranking import ScoreWeights as Weights
    from clipforge.analysis.ranking import total_score
    from clipforge.store.firestore import CandidateStore, firestore_client

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    weights = Weights(
        hook=hook,
        curiosity=curiosity,
        standalone=standalone,
        emotion=emotion,
        pacing=pacing,
        shareability=shareability,
    )
    db = firestore_client(settings)
    candidates = CandidateStore(db, settings)

    everything = candidates.all()
    if not everything:
        typer.echo("no candidates to rescore.")
        return

    before = sorted(everything, key=lambda c: c.total or 0, reverse=True)
    rescored = [(c, total_score(c.sub_scores, weights)) for c in everything]
    after = sorted(rescored, key=lambda pair: pair[1], reverse=True)

    old_order = [c.id for c in before]
    new_order = [c.id for c, _ in after]
    moved = sum(1 for index, cid in enumerate(new_order) if old_order[index] != cid)

    typer.echo(f"{len(everything)} candidate(s); {moved} change position")
    for candidate, new_total in after[:10]:
        previous = candidate.total or 0
        arrow = "=" if new_total == previous else ("+" if new_total > previous else "-")
        typer.echo(f"  {arrow} {candidate.id}  {previous} -> {new_total}")

    if not apply:
        typer.echo("read-only. Pass --apply to write the new totals.")
        return

    candidates.rescore([(candidate.id, new_total) for candidate, new_total in rescored])
    typer.echo(f"{len(rescored)} candidate total(s) updated. No model was called.")


user_app = typer.Typer(help="Accounts: who may use this ClipForge, and at what level.")
app.add_typer(user_app, name="user")


def _user_context() -> tuple[object, object, object]:
    """Settings plus the two halves of an account: Firestore and Auth."""
    from clipforge.store.firestore import firestore_client
    from clipforge.store.users import UserAdmin, UserStore

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    client = firestore_client(settings)
    return settings, UserStore(client, settings), UserAdmin(settings)


@user_app.command("list")
def user_list() -> None:
    """Show every account and where it stands.

    Reads Auth and Firestore together on purpose: an account can exist in one
    and not the other, and that gap is exactly what goes wrong. Someone who
    registered but has no profile row cannot be approved, and a profile with no
    Auth record is a leftover.
    """
    from clipforge.store.users import UserNotFoundError  # noqa: F401 - imported for symmetry

    _, store, admin = _user_context()
    profiles = {p.uid: p for p in store.all()}  # type: ignore[attr-defined]
    accounts = admin.list_accounts()  # type: ignore[attr-defined]

    if not accounts:
        typer.echo("no accounts yet. Register in the app first.")
        raise typer.Exit(code=0)

    typer.echo(f"{'email':38} {'role':7} {'status':9} {'providers':22} uid")
    for account in accounts:
        profile = profiles.pop(account.uid, None)
        role = profile.role.value if profile else "-"
        status = profile.status.value if profile else "NO PROFILE"
        typer.echo(
            f"{account.email:38} {role:7} {status:9} {account.describe_providers():22} {account.uid}"
        )

    for orphan in profiles.values():
        typer.echo(
            f"{orphan.email:38} {orphan.role.value:7} {orphan.status.value:9} "
            f"{'(no auth record)':22} {orphan.uid}"
        )


@user_app.command("grant-admin")
def user_grant_admin(
    email: str = typer.Argument(..., help="The account to make an approved admin."),
) -> None:
    """Make an account an approved admin.

    The bootstrap: the first admin cannot be approved through the UI, because
    approving is the thing only an admin can do. Every account after this one
    goes through the app.
    """
    from clipforge_contracts import UserRole, UserStatus

    from clipforge.store.users import UserNotFoundError, new_profile

    _, store, admin = _user_context()
    try:
        account = admin.by_email(email)  # type: ignore[attr-defined]
    except UserNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    existing = store.get(account.uid)  # type: ignore[attr-defined]
    if existing is None:
        profile = new_profile(
            account, role=UserRole.ADMIN, status=UserStatus.APPROVED, decided_by=account.uid
        )
    else:
        from datetime import UTC, datetime

        profile = existing.model_copy(
            update={
                "role": UserRole.ADMIN,
                "status": UserStatus.APPROVED,
                "decided_at": datetime.now(UTC),
                "decided_by": account.uid,
            }
        )

    store.save(profile)  # type: ignore[attr-defined]
    typer.echo(f"{account.email} is now an approved ADMIN ({account.uid})")


@user_app.command("approve")
def user_approve(
    email: str = typer.Argument(..., help="The account to approve."),
    by: str = typer.Option(
        "", help="Email of the admin making the decision; defaults to the first admin."
    ),
) -> None:
    """Approve a pending account from the machine, rather than the UI."""
    from datetime import UTC, datetime

    from clipforge_contracts import UserStatus

    from clipforge.store.users import UserNotFoundError, new_profile

    _, store, admin = _user_context()
    try:
        account = admin.by_email(email)  # type: ignore[attr-defined]
        decider = admin.by_email(by).uid if by else None  # type: ignore[attr-defined]
    except UserNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if decider is None:
        admins = store.admins()  # type: ignore[attr-defined]
        decider = admins[0].uid if admins else account.uid

    existing = store.get(account.uid)  # type: ignore[attr-defined]
    profile = (
        new_profile(account, status=UserStatus.APPROVED, decided_by=decider)
        if existing is None
        else existing.model_copy(
            update={
                "status": UserStatus.APPROVED,
                "decided_at": datetime.now(UTC),
                "decided_by": decider,
            }
        )
    )
    store.save(profile)  # type: ignore[attr-defined]
    typer.echo(f"{account.email} approved")


@user_app.command("set-password")
def user_set_password(
    email: str = typer.Argument(..., help="The account to give a password."),
) -> None:
    """Set a password on an existing account, prompting for it.

    An account created by Google sign-in has no password, and Google will hold a
    fresh sign-in on a new device for verification. Adding a password to the
    *same* uid sidesteps that without creating a second account, so nothing that
    already references the uid needs migrating.

    Prompted rather than passed as an argument: a password on the command line
    is a password in the shell history.
    """
    from clipforge.store.users import UserNotFoundError

    _, _store, admin = _user_context()
    try:
        account = admin.by_email(email)  # type: ignore[attr-defined]
    except UserNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        typer.echo("Firebase requires at least 6 characters; use at least 8.", err=True)
        raise typer.Exit(code=1)

    admin.set_password(account.uid, password)  # type: ignore[attr-defined]
    typer.echo(f"password set for {account.email}. Sign in with email and password.")


if __name__ == "__main__":
    app()
