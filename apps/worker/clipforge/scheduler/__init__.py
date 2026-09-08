"""ClipForge worker: scheduling, leasing and checkpointed execution.

``lease`` holds the job state machine as pure functions over job documents; it
has no I/O and knows nothing about Firestore. The adapters that apply those
transitions transactionally live in ``clipforge.store``.
"""

from clipforge.scheduler.lease import (
    LeaseError,
    Transition,
    cancel,
    claim,
    complete,
    fail_stage,
    is_claimable,
    is_lease_expired,
    reap,
    renew,
)

__all__ = [
    "LeaseError",
    "Transition",
    "cancel",
    "claim",
    "complete",
    "fail_stage",
    "is_claimable",
    "is_lease_expired",
    "reap",
    "renew",
]
