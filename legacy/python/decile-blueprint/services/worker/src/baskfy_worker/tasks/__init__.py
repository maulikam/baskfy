"""The ten nightly pipeline steps of docs/03 §"Nightly pipeline".

Each module exposes a plain ``async def run_*(session, ...)`` function — the actual work, callable
and testable without a broker — and a thin Celery task that wraps it. Keeping the two apart is
what lets the acceptance tests run the whole chain in-process against a real database.
"""

from baskfy_worker.tasks import (
    adjustments,
    bars,
    corporate_actions,
    factors,
    fundamentals,
    instruments,
    listings,
    market_health,
    membership,
    publish,
    quality,
    snapshots,
)

__all__ = [
    "adjustments",
    "bars",
    "corporate_actions",
    "factors",
    "fundamentals",
    "instruments",
    "listings",
    "market_health",
    "membership",
    "publish",
    "quality",
    "snapshots",
]
