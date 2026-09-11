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
    def test_it_is_the_ten_steps_docs_03_lists_plus_four_post_publish_ones(self) -> None:
        """docs/03's ten, in order, and four additions after them.

        `refresh_basket` is M30 and is deliberately **eleventh**, after `publish`; `compute_swing`
        is SW3 and is **twelfth**; `compute_vbt` is VB4 and is **thirteenth**; `compute_twt` is
        TW4 and is **fourteenth**. All four for the same reason and under the same rule — each
        reads what `publish` has already blessed, and none of them may hold back a `data_version`
        that is otherwise good. Fundamentals are folded into `refresh_index_snapshots` (T9.1),
        not a step of their own.
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
            "compute_swing",
            "compute_vbt",
            "compute_twt",
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

    def test_there_are_fourteen(self) -> None:
        assert len(NIGHTLY_CHAIN) == 14

    def test_the_post_publish_steps_come_after_publish(self) -> None:
        """The order is the rule, not a convention. A basket or a swing row computed before
        `publish` would be labelled with the previous `data_version` and describe a data set it
        was not built from."""
        order = [s.value for s in NIGHTLY_CHAIN]
        assert order.index("publish") < order.index("refresh_basket")
        assert order.index("refresh_basket") < order.index("compute_swing")
        assert order.index("compute_swing") < order.index("compute_vbt")
        assert order.index("compute_vbt") < order.index("compute_twt")

    def test_calendar_reconcile_covers_the_longest_factor_window(self) -> None:
        """9M/12M windows read a year of calendar; reconciling only tonight leaves them long."""
        assert CALENDAR_LOOKBACK_DAYS >= 365
        assert PIPELINE_LOOKBACK == CALENDAR_LOOKBACK_DAYS
