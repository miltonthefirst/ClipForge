# Releasing

Every deploy is a release, and every release has a new version. Copied from
Sarungano at the owner's request (2026-11-15), so a phone can say which build it
runs and always gets the newest one.

```sh
python tools/release.py            # 0.0.4 -> 0.0.5; 0.0.9 -> 0.1.0
python tools/release.py --major    # the first digit: the owner's call only
python tools/release.py --restart  # only restart the agent and worker, later
```

## What a release does

1. Refuses unless the tree is clean and `master` is in step with `origin`.
2. Raises `VERSION` and writes it everywhere a version lives: the worker's
   `pyproject.toml`, `clipforge/version.py` and `uv.lock`; `apps/web` and
   `apps/desktop` `package.json`; `tauri.conf.json`.
3. Commits `Release X.Y.Z`, tags `vX.Y.Z`, pushes both.
4. Deploys with `tools/deploy.ps1` (rules, indexes, storage, the PWA, the site).
5. Rebuilds the desktop app, unless it is open.
6. Restarts the agent and the worker on the new code: worker wished STOPPED,
   agent stopped, `uv sync` with every extra, agent started, worker wished back.
   Skipped while a job is running or queued; run `--restart` afterwards.

## The rules that hold it together

- **One version.** `VERSION` at the root is the source; a unit test
  (`tests/unit/test_version.py`) fails if any other copy disagrees.
- **The PWA knows its build.** `tools/stamp-build.mjs` writes
  `apps/web/src/app/build-info.ts` (version, commit, time) before every build,
  serve, test, lint and typecheck. It is generated and gitignored.
- **Only a release reaches the phone.** `firebase.json`'s predeploy hook for the
  PWA (`tools/check-release.mjs`) refuses a build whose stamp is not the tagged
  `HEAD` at `VERSION`. A bare `tools/deploy.ps1 -Only hosting` therefore fails
  outside a release; rules-only and site-only deploys are unaffected.
- **The phone runs the newest build.** The sidebar shows `App X (commit) ·
  built …` and the PC's version; a new version says "Updated to version X" once.
  The service worker checks for a new build on open, on return to the app and
  every ten minutes, and switches at once if the app was just opened or is in
  the background; otherwise "Update ready" in the header switches now and
  leaving the app switches quietly (`core/app-update.ts`).
