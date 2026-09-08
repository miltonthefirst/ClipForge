# ADR-0001 — Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-07
- **Phase:** 0

## Context

ClipForge spans three runtimes — an Angular PWA, Firebase, and a Python worker —
and its shape is driven by an unusual constraint: a 6 GB VRAM budget. Many of the
decisions that follow from that constraint look arbitrary or even wrong without
the reasoning attached. Six months from now, "why is the GPU lane serial?" and
"why isn't the source video in Storage?" are questions that will cost real time
to re-derive.

The repository is also a portfolio artefact. A reviewer's most valuable signal is
not the code but the reasoning behind it, and undocumented reasoning is
indistinguishable from no reasoning.

## Decision

Record every decision that closes off a plausible alternative as an ADR in
`docs/adr/`, numbered sequentially and never deleted. Superseded ADRs stay in
place with their status changed and a pointer to the ADR that replaced them.

An ADR is required when a decision:

- rules out an approach a competent engineer would otherwise reach for, or
- is forced by a constraint that is not visible from the code, or
- will look wrong without its context.

Routine choices — a library that has one obvious option, a naming convention —
do not need one.

Format: Context, Decision, Consequences, Alternatives considered. Short is fine;
a paragraph that explains *why* beats a page that restates *what*.

## Consequences

- Every phase's Definition of Done in `docs/PLAN.md` includes writing any ADRs
  the phase's decisions require.
- `docs/PLAN.md` §3.1 lists nine revisions (D1–D9) to the original brainstorm.
  Seven of them warrant an ADR and are being written as their phase lands, not
  retroactively.
- ADRs are immutable once accepted. Changing your mind means a new ADR.

## Alternatives considered

- **A single running decision log.** Cheaper to write, but it grows into an
  undifferentiated wall of text and gives no stable anchor to link to.
- **Documenting decisions in code comments only.** Comments explain the code
  they sit next to; they cannot capture a decision that spans three runtimes.
- **Nothing.** The default, and the reason most repositories cannot answer "why".
