"""Assembling the calibration report, and writing it as Markdown.

Two outputs from one computation: a :class:`CalibrationReport` document the PWA
renders, and a Markdown file for ``docs/``. They are generated together rather
than the second from the first, so that neither can quietly say something the
other does not.

**The report is written to be readable when the answer is "nothing".** That is
the likely answer for a long time, and the failure mode to design against is a
template that only reads well when it has a finding — because a report that
looks broken when it reports no effect is a report nobody will run twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from clipforge_contracts import (
    CalibrationMethod,
    CalibrationReport,
    CohortKind,
    CohortStat,
    ScoreWeights,
)

from clipforge.analytics.calibration import (
    MIN_N_FOR_CONCLUSION,
    MIN_N_FOR_FIT,
    OUTCOMES,
    Outcome,
    correlate,
    fit_weights,
)
from clipforge.analytics.cohorts import MIN_BUCKET_N, ClipFacts, summarise

__all__ = ["build_report", "render_markdown"]

_KIND_TITLES = {
    CohortKind.HOOK_TYPE: "Hook type",
    CohortKind.DURATION_BUCKET: "Clip duration",
    CohortKind.SCORE_BAND: "Predicted score band",
    CohortKind.TOPIC: "Topic (first tag)",
    CohortKind.POSTING_HOUR: "Posting time",
    CohortKind.CAPTION_STYLE: "Caption style",
    CohortKind.RENDER_PROFILE: "Render profile",
}

_KIND_CAVEATS = {
    CohortKind.HOOK_TYPE: (
        "Classified by an English-only heuristic. Clips narrated in another "
        "language land in STATEMENT regardless of their actual shape, so this "
        "breakdown is about English hooks plus one mixed bucket."
    ),
    CohortKind.POSTING_HOUR: (
        "UTC, not the audience's local time, which this project does not know."
    ),
    CohortKind.TOPIC: (
        "The clip's first grounded tag. A clip with no tags is absent rather "
        "than counted as untagged."
    ),
}


def build_report(
    facts: Sequence[ClipFacts],
    *,
    uid: str,
    report_id: str,
    baseline: ScoreWeights,
    window_days: int = 28,
    fit_against: Outcome | None = None,
    now: datetime | None = None,
) -> CalibrationReport:
    """Correlate, bucket, and attempt a fit. Never applies anything."""
    generated = now or datetime.now(UTC)
    correlations = [correlate(facts, outcome) for outcome in OUTCOMES]
    cohorts: list[CohortStat] = summarise(facts)

    target = fit_against or OUTCOMES[0]
    fitted = fit_weights(facts, target)
    # How many rows the fit could actually have used — clips carrying both
    # sub-scores and this outcome. Not len(facts): a null fit has two quite
    # different causes, and reporting "needs n=40 and there were 57" when the
    # real reason was that no dimension fitted positive is a note that sends the
    # reader looking for more data to solve a problem more data will not solve.
    usable = sum(1 for f in facts if f.sub_scores is not None and target.value(f) is not None)

    n = len(facts)
    notes = [
        f"Computed over {n} publication(s) with at least one settled metric "
        f"snapshot, across a {window_days}-day window.",
        f"Correlations are Spearman rank correlations. No conclusion is drawn "
        f"below n={MIN_N_FOR_CONCLUSION}.",
        f"Cohort means are withheld for buckets smaller than {MIN_BUCKET_N}; "
        f"the bucket and its count are still shown.",
    ]
    if fitted is None and usable < MIN_N_FOR_FIT:
        notes.append(
            f"No weights were fitted: a fit needs n={MIN_N_FOR_FIT} clips carrying "
            f"both sub-scores and '{target.label}', and there were {usable}."
        )
    elif fitted is None:
        notes.append(
            f"No weights were fitted, and not for want of data — {usable} clips "
            f"qualified. Every rubric dimension fitted zero or negative against "
            f"'{target.label}', which is a finding rather than a shortage: as "
            f"written, the rubric has no positive relationship with this outcome."
        )
    else:
        notes.append(
            f"Weights were fitted against '{target.label}'. They are a proposal. "
            f"Nothing has been re-ranked and no configuration has changed — "
            f"adopting them is a deliberate edit to the score weights."
        )

    return CalibrationReport(
        id=report_id,
        uid=uid,
        generated_at=generated,
        n=n,
        window_days=window_days,
        correlations=correlations,
        cohorts=cohorts,
        baseline_weights=baseline,
        fitted_weights=fitted,
        underpowered=n < MIN_N_FOR_CONCLUSION,
        notes=notes,
    )


def _fmt(value: float | None, places: int = 2) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _weights_row(label: str, weights: ScoreWeights) -> str:
    return (
        f"| {label} | {weights.hook:.3f} | {weights.curiosity:.3f} | "
        f"{weights.standalone:.3f} | {weights.emotion:.3f} | "
        f"{weights.pacing:.3f} | {weights.shareability:.3f} |"
    )


def render_markdown(report: CalibrationReport) -> str:
    """The written report, for ``docs/``."""
    lines: list[str] = []
    generated = report.generated_at.strftime("%Y-%m-%d")
    lines.append("# Calibration report")
    lines.append("")
    lines.append(
        f"> Generated {generated} over **n={report.n}** published clip(s), "
        f"{report.window_days}-day window. Report id `{report.id}`."
    )
    lines.append("")

    if report.underpowered:
        lines.append(
            f"> [!WARNING]\n"
            f"> **This report cannot support a conclusion.** n={report.n} is below "
            f"the {MIN_N_FOR_CONCLUSION} required, so every coefficient below is "
            f"printed for completeness and none of them should be acted on. This "
            f"is the expected state until considerably more clips have been "
            f"published; it is not a fault."
        )
        lines.append("")

    lines.append("## Did the score predict anything?")
    lines.append("")
    # Greek rho, deliberately: it is the conventional symbol for Spearman's
    # coefficient, and a Latin "p" in a statistics table reads as a p-value.
    lines.append("| Outcome | n | Spearman ρ | 95% interval | Reading |")  # noqa: RUF001
    lines.append("| --- | ---: | ---: | --- | --- |")
    for correlation in report.correlations:
        interval = (
            f"[{_fmt(correlation.ci_low)}, {_fmt(correlation.ci_high)}]"
            if correlation.ci_low is not None
            else "—"
        )
        method = "ρ" if correlation.method is CalibrationMethod.SPEARMAN else "r"  # noqa: RUF001
        lines.append(
            f"| {correlation.outcome} | {correlation.n} | "
            f"{method}&nbsp;{_fmt(correlation.coefficient)} | {interval} | "
            f"{correlation.interpretation or '—'} |"
        )
    lines.append("")

    lines.append("## Breakdowns")
    lines.append("")
    by_kind: dict[CohortKind, list[CohortStat]] = {}
    for stat in report.cohorts:
        by_kind.setdefault(stat.kind, []).append(stat)

    if not by_kind:
        lines.append("Nothing to break down yet.")
        lines.append("")

    for kind, stats in by_kind.items():
        lines.append(f"### {_KIND_TITLES.get(kind, kind.value)}")
        lines.append("")
        caveat = _KIND_CAVEATS.get(kind)
        if caveat:
            lines.append(f"*{caveat}*")
            lines.append("")
        lines.append("| Bucket | n | Mean score | Mean views | Mean % viewed | At midpoint |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for stat in stats:
            lines.append(
                f"| {stat.bucket} | {stat.n} | "
                f"{_fmt(stat.mean_predicted_score, 1)} | "
                f"{_fmt(stat.mean_views, 1)} | "
                f"{_fmt(stat.mean_view_percentage, 1)} | "
                f"{_fmt(stat.mean_retention_at_half, 3)} |"
            )
        lines.append("")

    lines.append("## Weights")
    lines.append("")
    lines.append("| | hook | curiosity | standalone | emotion | pacing | shareability |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    lines.append(_weights_row("In use", report.baseline_weights))
    if report.fitted_weights is not None:
        lines.append(_weights_row("Fitted (proposal)", report.fitted_weights))
    lines.append("")
    if report.fitted_weights is None:
        lines.append("No fitted weights. See the notes below for why.")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    for note in report.notes or []:
        lines.append(f"- {note}")
    lines.append("")
    lines.append(
        "*Generated by `clipforge-worker calibrate`. Rewriting it by hand would "
        "make it disagree with the `calibrations/` document the app renders.*"
    )
    return "\n".join(lines) + "\n"
