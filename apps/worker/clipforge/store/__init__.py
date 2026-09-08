"""ClipForge worker: persistence adapters.

These are deliberately thin. Every decision about whether a state transition is
legal lives in :mod:`clipforge.scheduler.lease` as a pure function; the adapters
only read, apply, and compare-and-swap.
"""

from clipforge.store.firestore import JobStore, WorkerStore, firestore_client

__all__ = ["JobStore", "WorkerStore", "firestore_client"]
