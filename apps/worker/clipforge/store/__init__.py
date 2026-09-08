"""ClipForge worker: persistence adapters.

These are deliberately thin. Every decision about whether a state transition is
legal lives in :mod:`clipforge.scheduler.lease` as a pure function; the adapters
only read, apply, and compare-and-swap.
"""

from clipforge.store.blobs import BlobRef, BlobStore, LocalBlobStore, build_blob_store
from clipforge.store.firestore import (
    CandidateStore,
    JobStore,
    SourceStore,
    WorkerStore,
    firestore_client,
)

__all__ = [
    "BlobRef",
    "BlobStore",
    "CandidateStore",
    "JobStore",
    "LocalBlobStore",
    "SourceStore",
    "WorkerStore",
    "build_blob_store",
    "firestore_client",
]
