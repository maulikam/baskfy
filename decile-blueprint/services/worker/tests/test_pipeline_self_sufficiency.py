"""The two reasons the nightly pipeline stopped publishing — 18 Aug to 29 Aug 2026.

The visible symptom was a product serving a ten-day-old trading session. Underneath were two
independent faults, and either alone was enough to stop every publish:

**1. `fetch_daily_bars` could only speak to Kite.** `daily_bars` is Kite's port, this deployment
has no Kite credentials, and so the step wrote zero bars and the quality gate correctly refused
the run — "0 bars against a 10-day median of 2532 (threshold 2279)". The bhavcopy needs no
credentials and was already ingesting these exact bars by hand.

**2. The schedule ran on days the exchange was shut.** 2026-08-28 was a Friday *and* a holiday.
The chain ran, found nothing, and raised a CRITICAL alert for a day with no session. Every
weekend did the same, which is how an alert becomes noise.
"""

from __future__ import annotations

import datetime as dt

import pytest

from baskfy_worker import ops
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.bars import run_fetch_daily_bars
from baskfy_worker.window import DateWindow

WINDOW = DateWindow(dt.date(2026, 8, 27), dt.date(2026, 8, 27))


class _NoBhavcopy:
    """A provider that offers neither port — the genuinely unserviceable case."""


class _BhavcopyOnly:
    """What this deployment actually has: NSE, and no Kite."""

    def bhavcopy(self, on: dt.date) -> object:  # pragma: no cover - shape only
        raise AssertionError("the real fetch is exercised through backfill_bars_from_bhavcopy")


class TestTheBarStepFallsBackToTheBhavcopy:
    async def test_no_daily_bars_port_reaches_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 2026-08-28 case: "no provider could serve 'daily_bars'".

        Before this, the step returned 0 here and the night was over.
        """
        called: dict[str, object] = {}

        async def fake_backfill(provider, window, **kwargs):  # noqa: ANN001, ANN003
            called["window"] = window
            return _Report(bars_written=2516, days_written=1)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        outcome = StepOutcome()
        written = await run_fetch_daily_bars(
            session=None, provider=_BhavcopyOnly(), outcome=outcome, instruments=[], window=WINDOW
        )
        assert written == 2516
        assert called["window"] == WINDOW

    async def test_it_reports_which_source_served_the_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A day's bars coming from a different source than usual must be legible afterwards.

        The step's note is what an operator reads at 3am; "2516 bars" with no provenance would
        leave them unable to tell a Kite night from a bhavcopy one.
        """

        async def fake_backfill(provider, window, **kwargs):  # noqa: ANN001, ANN003
            return _Report(bars_written=2516, days_written=1)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        outcome = StepOutcome()
        await run_fetch_daily_bars(
            session=None, provider=_BhavcopyOnly(), outcome=outcome, instruments=[], window=WINDOW
        )
        # `StepOutcome.detail` is the JSONB an operator reads back off `pipeline_run_step`.
        assert outcome.detail["fallback"] == "bhavcopy"
        assert "daily_bars" in str(outcome.detail["fallback_reason"])
        assert outcome.rows_out == 2516

    async def test_a_provider_with_neither_port_still_returns_zero_and_says_so(self) -> None:
        """The honest dead end. It must not raise — the quality gate owns 'is this publishable'."""
        outcome = StepOutcome()
        written = await run_fetch_daily_bars(
            session=None, provider=_NoBhavcopy(), outcome=outcome, instruments=[], window=WINDOW
        )
        assert written == 0


class _Report:
    def __init__(self, *, bars_written: int, days_written: int) -> None:
        self.bars_written = bars_written
        self.days_written = days_written
        self.missing_days: list[dt.date] = []
        self.unmatched_symbols: set[str] = set()
        self.failures: dict[str, str] = {}


class TestTheScheduleRespectsTheCalendar:
    """Prompt 3 deliverable 7: "never attempt to ingest or compute for a non-trading day"."""

    def test_is_trading_day_is_a_calendar_lookup_not_a_weekday_rule(self) -> None:
        """Holidays are the case that matters, and no weekday rule knows them.

        2026-08-28 was a Friday and shut. A `weekday() < 5` check would have run the pipeline on
        it, which is exactly what happened.
        """
        import ast
        import inspect

        # The *code*, with the docstring stripped — this test's own first draft failed because
        # the docstring says "no weekday rule knows them", which is prose about the bug rather
        # than the bug. Asserting over raw source would have made every explanation a hazard.
        tree = ast.parse(inspect.getsource(ops.is_trading_day).strip())
        fn = tree.body[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = "\n".join(ast.unparse(node) for node in body)

        assert "TradingDay" in code, "must read the calendar"
        assert "weekday" not in code, "a weekday rule cannot know a holiday"

    def test_an_unknown_date_is_not_a_trading_day(self) -> None:
        """The two ways to be wrong are not symmetric.

        Skipping a real session delays a publish by a day and shows in the freshness pill;
        running a phantom one produces a failed run and a CRITICAL alert for a day the exchange
        was closed. So an absent calendar row must read as "do not run".
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(ops.is_trading_day).strip())
        fn = tree.body[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = "\n".join(ast.unparse(node) for node in body)

        # `scalar` returns None for a date with no row; `bool(None)` is False. An `is True`
        # comparison or a default of True would both run the pipeline on an unknown day.
        assert "bool(found)" in code, "an absent row must be falsy, not an error or a default"
