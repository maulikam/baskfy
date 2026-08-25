"""The nightly chain's identity, without a database.

`test_pipeline_steps.py` is `requires_db` for the rest of Prompt 3. These assertions must
still run when Postgres is down — they are the lock on step order and the 12-month calendar
lookback (T9.2).
"""

from __future__ import annotations

from baskfy_worker.calendar import CALENDAR_LOOKBACK_DAYS
from baskfy_worker.orchestrator import CALENDAR_LOOKBACK_DAYS as PIPELINE_LOOKBACK
from baskfy_worker.steps import NIGHTLY_CHAIN


class TestTheChain:
    def test_it_is_the_ten_steps_docs_03_lists_plus_M30s_cache(self) -> None:
        """docs/03's ten, in order, and one addition after them.

        `refresh_basket` is M30 and is deliberately **eleventh**, after `publish`. Fundamentals
        are folded into `refresh_index_snapshots` (T9.1), not a twelfth step.
        """
        assert [s.value for s in NIGHTLY_CHAIN] == [
            "refresh_instruments",
            "fetch_daily_bars",
            "fetch_corporate_actions",
            "apply_adjustments",
            "refresh_index_membership",
            "refresh_index_snapshots",
            "compute_factors",
            "compute_market_health",
            "data_quality_gate",
            "publish",
            "refresh_basket",
        ]

    def test_docs_03s_own_ten_still_come_first_and_in_order(self) -> None:
        assert [s.value for s in NIGHTLY_CHAIN][:10] == [
            "refresh_instruments",
            "fetch_daily_bars",
            "fetch_corporate_actions",
            "apply_adjustments",
            "refresh_index_membership",
            "refresh_index_snapshots",
            "compute_factors",
            "compute_market_health",
            "data_quality_gate",
            "publish",
        ]

    def test_there_are_eleven(self) -> None:
        assert len(NIGHTLY_CHAIN) == 11

    def test_calendar_reconcile_covers_the_longest_factor_window(self) -> None:
        """9M/12M windows read a year of calendar; reconciling only tonight leaves them long."""
        assert CALENDAR_LOOKBACK_DAYS >= 365
        assert PIPELINE_LOOKBACK == CALENDAR_LOOKBACK_DAYS
