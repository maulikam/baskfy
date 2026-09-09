"""What the pipeline needs from outside the database.

Its own module so that ``baskfy_worker.providers`` (which builds the stack) and
``baskfy_worker.orchestrator`` (which consumes it) do not have to import each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from baskfy_worker.tasks.factors import FactorEngine
from baskfy_worker.window import DateWindow


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

    # --- SW3: the swing detection step ---------------------------------------
    #
    # Passed in rather than read from settings inside the step, for the same reason the provider
    # is: a test drives the whole chain in-process, and a step that reaches for the environment
    # cannot be driven. `None` means "this deployment has no sole tenant", and the step then
    # skips itself and says so — the `sw_` schema is keyed by user and there is no default user
    # a nightly job may invent.
    swing_user_id: int | None = None
    #: The benchmark `04` §8.2's gate reads; `nifty-50` is the fallback inside the step.
    #: SW17 / M87-reverted: the MidSmallcap 400 is the book's tape. See
    #: `WorkerSettings.swing_index_slug` for why, and why it is not nifty-500.
    swing_index_slug: str = "nifty-mid-small-400"
    #: PACK.6: while this is false the exposure ladder reads *simulated* closes. The step needs
    #: it to pick which book to summarise, and it places nothing either way.
    swing_execution_enabled: bool = False
