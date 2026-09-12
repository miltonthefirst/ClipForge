"""Clip selection: the intellectual core of the pipeline.

Everything here except the model call itself is a pure function, which is
deliberate. Decisions D4 and D5 split *judgement* (what is interesting) from
*arithmetic* (where exactly to cut, and how to score it) — the model does the
first and is not trusted with the second.

That split is why the selection algorithm is fully testable with no GPU and no
network: `select_candidates` takes the model call as a parameter.
"""

from clipforge.analysis.boundaries import SnapTolerances, snap_end, snap_start
from clipforge.analysis.ranking import (
    DEFAULT_WEIGHTS,
    ScoredWindow,
    ScoreWeights,
    iou,
    merge_overlapping,
    rank,
    total_score,
)
from clipforge.analysis.windows import WindowSpec, build_windows

__all__ = [
    "DEFAULT_WEIGHTS",
    "ScoreWeights",
    "ScoredWindow",
    "SnapTolerances",
    "WindowSpec",
    "build_windows",
    "iou",
    "merge_overlapping",
    "rank",
    "snap_end",
    "snap_start",
    "total_score",
]
