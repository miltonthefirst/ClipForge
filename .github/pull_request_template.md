## What this changes

## Which phase

<!-- See docs/PLAN.md. If this is out of phase, explain why it should land now. -->

## Definition of Done

- [ ] CI green
- [ ] Unit tests for pure logic; emulator-backed tests for anything touching Firebase
- [ ] ADR added if this decision closes off an alternative
- [ ] `README.md` / `.env.example` updated if setup changed
- [ ] Reproducible from a clean clone by following the docs

## If this touches inference or media

- [ ] `uv run --project apps/worker pytest -m gpu` passes locally
- [ ] Stays within the ~5.4 GB VRAM budget, with only one model resident at a time
