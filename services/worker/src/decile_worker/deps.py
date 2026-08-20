"""What the pipeline needs from outside the database.

Its own module so that ``decile_worker.providers`` (which builds the stack) and
``decile_worker.orchestrator`` (which consumes it) do not have to import each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from decile_worker.tasks.factors import FactorEngine
from decile_worker.window import DateWindow


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    """Everything the chain needs from outside the database.

    Bundled so a test can drive the whole pipeline with a ``FixtureProvider`` and no cache, which
    is what makes the acceptance criteria runnable in-process.
    """

    provider: object
    cache: object = None
    factor_engine: FactorEngine | None = None
    #: Backfill window for step 2. Defaults to the trade date alone — a nightly run fetches one
    #: day; the backfill CLI passes a wider window.
    bars_window: DateWindow | None = None
