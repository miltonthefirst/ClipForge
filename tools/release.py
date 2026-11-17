"""Release: the one way ClipForge is deployed, so every deploy has a new version.

    python tools/release.py            # 0.0.4 -> 0.0.5 (0.0.9 -> 0.1.0)
    python tools/release.py --major    # 0.4.2 -> 1.0.0, only when the owner says so
    python tools/release.py --restart  # only the last step, for a release that skipped it

Copied from Sarungano (scripts/release.py), at the owner's request, 2026-11-15,
so the phone can say which build it runs and every deploy is a new one.

The rule (Sarungano's owner's, 2026-10-23, adopted here): every deploy raises
the version. The last digit goes up by one; when it would reach 10 it goes back
to 0 and the middle digit goes up instead. The first digit is the owner's to
raise, never this script's on its own.

What it does, in order, stopping at the first failure:
  1. refuses unless the working tree is clean and master is in step with
     origin (a release is a commit, and it must contain exactly what ships);
  2. writes the new version to VERSION and every place that carries one (the
     worker's pyproject and version.py and its uv.lock, the web and desktop
     package.json, tauri.conf.json);
  3. commits "Release X.Y.Z", tags it vX.Y.Z, and pushes both;
  4. deploys with tools/deploy.ps1 (rules, indexes, storage, the PWA, the
     site); firebase.json's predeploy hook (tools/check-release.mjs) refuses a
     PWA that is not this tagged release;
  5. rebuilds the desktop app, unless it is open (then says so);
  6. restarts the agent and the worker so both run and report the new
     version: the worker is stopped through its wish, the agent is stopped,
     the venv is synced with every extra, the agent is started, and the worker
     is wished back to what it was. With a job running or queued it does not,
     and says to run `--restart` once the queue is empty.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "apps", "worker")
EXTRAS = ["gpu", "media", "speech", "home"]

VERSIONED_JSON = [
    "apps/web/package.json",
    "apps/desktop/package.json",
    "apps/desktop/src-tauri/tauri.conf.json",
]
PYPROJECT = "apps/worker/pyproject.toml"
VERSION_PY = "apps/worker/clipforge/version.py"


class ReleaseError(Exception):
    pass


def next_version(current: str, major: bool = False) -> str:
    """The version after `current` under the owner's rule."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current.strip())
    if not match:
        raise ReleaseError(f"VERSION reads {current!r}, not MAJOR.MINOR.PATCH.")
    first, middle, last = (int(part) for part in match.groups())
    if major:
        return f"{first + 1}.0.0"
    if last + 1 >= 10:
        return f"{first}.{middle + 1}.0"
    return f"{first}.{middle}.{last + 1}"


def free_version(current: str, taken: set[str], major: bool = False) -> str:
    """The next version under the rule whose tag does not exist yet.

    A tag can predate this script: ClipForge's plan tagged its v0.1.0 milestone
    long before releases were versioned, and the first release to reach 0.1.0
    stopped at `git tag` after committing. Skipping is safe because a version
    is never reused either way.
    """
    version = next_version(current, major)
    while f"v{version}" in taken:
        print(f"  v{version} is already a tag; skipping to the next version")
        version = next_version(version)
    return version


def _tags() -> set[str]:
    _run(["git", "fetch", "--quiet", "--tags", "origin"], quiet=True)
    return {line.strip() for line in _run(["git", "tag", "-l"], quiet=True).splitlines() if line.strip()}


def _run(args: list[str], cwd: str = ROOT, quiet: bool = False, env: dict | None = None) -> str:
    shell = os.name == "nt" and args[0] in ("npm", "npx")
    done = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=shell,
        env={**os.environ, **(env or {})},
    )
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-15:])
        raise ReleaseError(f"{' '.join(args[:6])} failed:\n{tail}")
    if not quiet:
        print(f"  ok: {' '.join(args[:5])}")
    return done.stdout


def _powershell(script: str, *args: str) -> str:
    return _run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *args],
        # npm prints a notice to stderr that Windows PowerShell 5.1 treats as
        # fatal; the scripts set this themselves too.
        env={"npm_config_loglevel": "warn"},
    )


def _check_tree() -> None:
    if _run(["git", "status", "--porcelain"], quiet=True).strip():
        raise ReleaseError("The working tree has uncommitted changes. Commit them first: a release ships a commit.")
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], quiet=True).strip()
    if branch != "master":
        raise ReleaseError(f"Releases are cut from master, not {branch}.")
    _run(["git", "fetch", "--quiet", "origin", "master"], quiet=True)
    behind = _run(["git", "rev-list", "--count", "HEAD..origin/master"], quiet=True).strip()
    if behind != "0":
        raise ReleaseError(f"master is {behind} commit(s) behind origin. Pull first.")


def _replace_once(path: str, pattern: str, replacement: str) -> None:
    full = os.path.join(ROOT, path)
    with open(full, encoding="utf-8", newline="") as f:
        text = f.read()
    # Only the version changes, so each file keeps its own layout and endings.
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(f"No version found in {path}.")
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(updated)


def write_version(version: str) -> list[str]:
    """Every file that says which version this is, set to `version`."""
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8", newline="\n") as f:
        f.write(version + "\n")
    for path in VERSIONED_JSON:
        _replace_once(path, r'("version"\s*:\s*")[^"]*(")', rf"\g<1>{version}\g<2>")
    _replace_once(PYPROJECT, r'^(version\s*=\s*")[^"]*(")', rf"\g<1>{version}\g<2>")
    _replace_once(VERSION_PY, r'^(__version__: Final\[str\] = ")[^"]*(")', rf"\g<1>{version}\g<2>")
    _run(["uv", "lock", "--quiet"], cwd=WORKER)
    return ["VERSION", *VERSIONED_JSON, PYPROJECT, VERSION_PY, "apps/worker/uv.lock"]


def _desktop_open() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq clipforge-desktop.exe"], capture_output=True, text=True)
    return "clipforge-desktop.exe" in out.stdout


def _agent(*args: str) -> dict:
    """`python -m clipforge agent-wish ...`: the module, not the console script,
    so the venv's clipforge-worker.exe is not held open during `uv sync`."""
    out = _run(["uv", "run", "--no-sync", "python", "-m", "clipforge", "agent-wish", *args], cwd=WORKER, quiet=True)
    return json.loads(out.strip().splitlines()[-1])


def restart(version: str) -> str:
    """Restart the agent and the worker on the code just released."""
    before = _agent("show")
    if before.get("running") or before.get("queued"):
        return (
            f"NOT restarted: {before['running']} job(s) running and {before['queued']} queued. "
            "Run `python tools/release.py --restart` once the queue is empty."
        )
    was_running = before.get("desired") == "RUNNING"
    if was_running:
        _agent("worker", "STOPPED", "--by", f"release-{version}")
    _powershell(os.path.join(ROOT, "tools", "agent.ps1"), "-Stop")
    time.sleep(3)
    sync = ["uv", "sync", "--locked"]
    for extra in EXTRAS:
        sync += ["--extra", extra]
    _run(sync, cwd=WORKER)
    _powershell(os.path.join(ROOT, "tools", "agent.ps1"), "-Start")

    deadline = time.monotonic() + 120
    state: dict = {}
    while time.monotonic() < deadline:
        state = _agent("show")
        if state.get("version") == version:
            break
        time.sleep(3)
    else:
        return f"the agent did not report {version} within two minutes: see ~/.clipforge/agent.log"
    if was_running:
        state = _agent("worker", "RUNNING", "--by", f"release-{version}")
    return f"agent {state.get('version')}, worker {state.get('state')}, home route {state['home'].get('state')}"


def release(major: bool = False) -> str:
    _check_tree()
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        current = f.read().strip()
    version = free_version(current, _tags(), major)
    print(f"Releasing {current} -> {version}")

    changed = write_version(version)
    _run(["git", "add", *changed])
    _run(["git", "commit", "-m", f"Release {version}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"])
    _run(["git", "tag", "-a", f"v{version}", "-m", f"Release {version}"])
    _run(["git", "push", "origin", "master", f"v{version}"])
    try:
        return _ship(version)
    except ReleaseError as exc:
        raise ReleaseError(
            f"{exc}\n\nRelease {version} is committed, tagged and pushed, but not fully shipped. Fix the cause, "
            f"commit the fix, and run the release again: it will be {next_version(version)}."
        ) from exc


def _ship(version: str) -> str:
    # Builds the PWA (whose prebuild stamps this version and commit), inlines the
    # project's config, and deploys; the predeploy hook checks the stamp.
    _powershell(os.path.join(ROOT, "tools", "deploy.ps1"), "-Yes")
    print("  deployed rules, indexes, storage, the PWA and the site")

    if _desktop_open():
        desktop = "NOT rebuilt: the desktop app is open. Close it and run tools/desktop.ps1."
    else:
        _powershell(os.path.join(ROOT, "tools", "desktop.ps1"))
        desktop = "rebuilt"
    print(f"  desktop app: {desktop}")
    print(f"  agent and worker: {restart(version)}")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--major", action="store_true", help="raise the first digit (the owner's call only)")
    parser.add_argument("--restart", action="store_true", help="only restart the agent and worker on VERSION")
    args = parser.parse_args()
    try:
        if args.restart:
            with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
                print(f"agent and worker: {restart(f.read().strip())}")
            return
        version = release(args.major)
    except ReleaseError as exc:
        print(f"Release stopped: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Released {version}.")


if __name__ == "__main__":
    main()
