"""Phase 9 — did any of the scoring predict anything?

Everything before this milestone produces scores. Nothing before it finds out
whether they were worth producing. The package is arranged so that the finding
out is separable from the fetching:

- :mod:`clipforge.analytics.youtube` talks to the YouTube Analytics API and is
  the only part that needs the network or a credential.
- :mod:`clipforge.analytics.cohorts` buckets clips by the dimensions the plan
  names, and is pure.
- :mod:`clipforge.analytics.calibration` computes correlations, intervals and a
  weight fit, and is pure.
- :mod:`clipforge.analytics.report` renders the result as Markdown, and is pure.
- :mod:`clipforge.analytics.poller` is the orchestration that joins them to the
  store.

The split is not tidiness. Three of the five are testable with no account, no
network and no published video, which is what lets this phase be built and
proven while the one thing it cannot fake — a real clip accruing real views —
is still waiting on an OAuth client.
"""

from __future__ import annotations
