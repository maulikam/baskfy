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

import asyncio
import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import NoReturn, cast

import polars as pl
import pytest
from helpers import make_instrument, requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_providers.errors import ProviderError
from baskfy_worker import ops
from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import bars as bars_module
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

        async def fake_backfill(provider: object, window: DateWindow, **kwargs: object) -> _Report:
            called["window"] = window
            return _Report(bars_written=2516, days_written=1)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        outcome = StepOutcome()
        written = await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_BhavcopyOnly(),
            outcome=outcome,
            instruments=[],
            window=WINDOW,
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

        async def fake_backfill(provider: object, window: DateWindow, **kwargs: object) -> _Report:
            return _Report(bars_written=2516, days_written=1)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        outcome = StepOutcome()
        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_BhavcopyOnly(),
            outcome=outcome,
            instruments=[],
            window=WINDOW,
        )
        # `StepOutcome.detail` is the JSONB an operator reads back off `pipeline_run_step`.
        assert outcome.detail["fallback"] == "bhavcopy"
        assert "daily_bars" in str(outcome.detail["fallback_reason"])
        assert outcome.rows_out == 2516

    async def test_a_provider_with_neither_port_still_returns_zero_and_says_so(self) -> None:
        """The honest dead end. It must not raise — the quality gate owns 'is this publishable'."""
        outcome = StepOutcome()
        written = await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_NoBhavcopy(),
            outcome=outcome,
            instruments=[],
            window=WINDOW,
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
        import ast  # noqa: PLC0415 - only this test reads source
        import inspect  # noqa: PLC0415 - only this test reads source

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
        import ast  # noqa: PLC0415 - only this test reads source
        import inspect  # noqa: PLC0415 - only this test reads source

        tree = ast.parse(inspect.getsource(ops.is_trading_day).strip())
        fn = tree.body[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = "\n".join(ast.unparse(node) for node in body)

        # `scalar` returns None for a date with no row; `bool(None)` is False. An `is True`
        # comparison or a default of True would both run the pipeline on an unknown day.
        assert "bool(found)" in code, "an absent row must be falsy, not an error or a default"


@pytest.fixture(autouse=True)
def _stub_coverage_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_fetch_daily_bars` asks the database which instruments the bhavcopy already covered
    (8 Sep 2026). Most tests in this module drive a bare `AsyncSession()` with no bind, so the
    read is stubbed to "nothing covered" — which is also the pre-change behaviour, keeping every
    existing assertion about the Kite pass meaningful. `TestTheBhavcopyLeadsForACompletedSession`
    overrides it to exercise the filtering itself."""

    async def _none(session: object, on: object) -> set[int]:
        return set()

    monkeypatch.setattr("baskfy_worker.tasks.bars._instruments_with_bars", _none)


class TestItDoesNotAskTenThousandTimes:
    """`CredentialsMissing` is a fact about the deployment, not about the instrument.

    Without this, an unconfigured Kite made the step attempt every one of 10,514 instruments —
    each through the retry policy's backoff — before failing anyway. A hand-run of the pipeline
    looked like a hang: no output, no run row (the CLI commits at the end), and many minutes of
    nothing. Rate-limit and payload errors are deliberately *not* treated this way, because those
    are per-symbol and the next instrument may succeed.
    """

    async def test_it_stops_after_the_first_credentials_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baskfy_providers.errors import CredentialsMissing  # noqa: PLC0415 - local to this test

        attempts: list[str] = []

        class _Unconfigured:
            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> NoReturn:
                attempts.append(str(token))
                raise CredentialsMissing("BASKFY_KITE_API_KEY is empty")

            def bhavcopy(self, on: dt.date) -> NoReturn:
                raise AssertionError("not reached in this test")

        async def fake_backfill(provider: object, window: DateWindow, **kwargs: object) -> _Report:
            return _Report(bars_written=2516, days_written=1)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        instruments = [(i, f"SYM{i}", i) for i in range(500)]
        outcome = StepOutcome()

        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_Unconfigured(),
            outcome=outcome,
            instruments=instruments,
            window=WINDOW,
        )
        assert len(attempts) == 1, f"asked {len(attempts)} times for one deployment-wide fact"

    async def test_a_per_symbol_error_does_not_stop_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side. One bad symbol must not abandon the other 499."""
        from baskfy_providers.errors import ProviderError  # noqa: PLC0415 - local to this test

        attempts: list[str] = []

        class _OneBadSymbol:
            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> NoReturn:
                attempts.append(str(token))
                raise ProviderError("rate limited")

            def bhavcopy(self, on: dt.date) -> NoReturn:
                raise AssertionError("not reached")

        async def fake_backfill(provider: object, window: DateWindow, **kwargs: object) -> _Report:
            return _Report(bars_written=0, days_written=0)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        instruments = [(i, f"SYM{i}", i) for i in range(20)]
        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_OneBadSymbol(),
            outcome=StepOutcome(),
            instruments=instruments,
            window=WINDOW,
        )
        assert len(attempts) == 20, "a per-symbol failure must not abandon the sweep"


# =====================================================================================
# M84 — the third fault, and the one the two above could not see.
#
# The fallback in this file asks for the bhavcopy when Kite wrote **nothing**. A Kite pass that
# dies halfway writes something, so it never reaches the fallback: the day lands partial and the
# gate judges a half session as if it were a whole one. For a single session the bhavcopy is not
# a fallback at all — it is the exchange's own end-of-day record, it needs no credential, and it
# is one 200 KB file — so any gap Kite leaves is now completed from it.
#
# On 3 Sep 2026 this mattered a second way: the chain never finished, and nothing re-ran it.
# `test_session_catch_up.py` is that half.
# =====================================================================================


class _KiteWithGaps:
    """Serves some names and refuses others — what a dying Kite session actually looks like."""

    def __init__(self, *, refuse: set[int] | None = None) -> None:
        self.refuse = refuse or set()
        self.asked: list[int] = []

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        self.asked.append(token)
        if token in self.refuse:
            raise ProviderError("[kite] Kite rejected the request as malformed: invalid token")
        return pl.DataFrame(
            {
                "date": [end],
                "open": [Decimal("100")],
                "high": [Decimal("101")],
                "low": [Decimal("99")],
                "close": [Decimal("100.5")],
                "volume": [1000],
                "source": ["kite"],
            }
        )

    def bhavcopy(self, on: dt.date) -> object:  # pragma: no cover - shape only
        raise AssertionError("the real fetch goes through backfill_bars_from_bhavcopy")


class TestAPartialKitePassIsCompletedFromTheBhavcopy:
    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, calls: list[DateWindow]) -> None:
        async def fake_backfill(provider: object, window: DateWindow, **kwargs: object) -> _Report:
            calls.append(window)
            return _Report(bars_written=3635, days_written=1)

        async def fake_upsert(session: object, instrument_id: int, frame: pl.DataFrame) -> int:
            return int(frame.height)

        monkeypatch.setattr(
            "baskfy_worker.bhavcopy_backfill.backfill_bars_from_bhavcopy", fake_backfill
        )
        monkeypatch.setattr("baskfy_worker.tasks.bars.upsert_bars", fake_upsert)

    async def test_a_gap_reaches_the_bhavcopy_even_though_kite_wrote_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some rows is not all rows, and the gate downstream cannot tell the difference.

        M84 asserted this as a *top-up* running after Kite. Since 8 Sep 2026 the bhavcopy LEADS
        — one 207 KB request against Kite's ~3,000 — so the file is read once at the start and
        the property this test exists for, that the day is never left half-landed, holds a
        fortiori. What is re-pinned is the mechanism, not the guarantee.
        """
        calls: list[DateWindow] = []
        self._patch(monkeypatch, calls)
        outcome = StepOutcome()

        written = await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_KiteWithGaps(refuse={222}),
            outcome=outcome,
            instruments=[(1, "AAA", 111), (2, "BBB", 222)],
            window=WINDOW,
        )

        assert [str(w) for w in calls] == [str(WINDOW)], "the day was left half-landed"
        assert outcome.detail["bhavcopy_first"] == 3635
        assert written == 3636, "the file's rows plus what Kite added on top"
        assert outcome.detail["failures"] == {
            "BBB": "[kite] Kite rejected the request as malformed: invalid token"
        }, "the bhavcopy's report overwrote the only record of why Kite failed"

    async def test_an_instrument_without_a_token_is_a_gap_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kite does not carry every NSE listing (docs/02). The bhavcopy does."""
        calls: list[DateWindow] = []
        self._patch(monkeypatch, calls)

        written = await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_KiteWithGaps(),
            outcome=StepOutcome(),
            instruments=[(1, "AAA", 111), (2, "SME", None)],
            window=WINDOW,
        )

        assert written >= 1
        assert len(calls) == 1, "the file is read exactly once for a session"

    async def test_the_file_is_read_once_a_session_and_never_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M84 asserted the opposite — "an ordinary night must not pay for a file it does not
        need" — and that premise inverted on 8 Sep 2026.

        The file is ONE 207 KB request. The Kite pass it displaces is ~3,000 requests and ~25
        minutes at the bulk lane's 2 req/s, which is what made 7 Sep take 73 minutes. It is now
        the cheap half of the step, so every session pays for it deliberately. What must not
        happen is paying TWICE — the end-of-step top-up re-reading the file that already led.
        """
        calls: list[DateWindow] = []
        self._patch(monkeypatch, calls)
        outcome = StepOutcome()

        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_KiteWithGaps(),
            outcome=outcome,
            instruments=[(1, "AAA", 111)],
            window=WINDOW,
        )

        assert len(calls) == 1, f"the session read the bhavcopy {len(calls)} times"
        assert "top_up" not in outcome.detail, "the end-of-step top-up re-read the same file"

    async def test_a_backfill_window_is_left_to_the_backfill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A multi-year window walks the archive day by day: hours, not a top-up."""
        calls: list[DateWindow] = []
        self._patch(monkeypatch, calls)

        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_KiteWithGaps(refuse={222}),
            outcome=StepOutcome(),
            instruments=[(1, "AAA", 111), (2, "BBB", 222)],
            window=DateWindow(dt.date(2024, 1, 1), dt.date(2026, 8, 27)),
        )

        assert calls == [], "a partial multi-year Kite pass triggered a day-by-day archive walk"


# =====================================================================================
# M84 — the fourth fault, found by the fix for the third.
#
# `_fetch_from_bhavcopy` runs INSIDE the chain's single open transaction. Until this was fixed
# it called `backfill_bars_from_bhavcopy`, which opened a connection of its own — and step 1 has
# upserted `instrument` rows the chain has not committed, so the bar insert on that second
# connection needs a KEY SHARE lock the chain still holds, while the chain waits for the call to
# return. Postgres sees a lock wait on one side and `ClientRead` on the other, which is not a
# cycle it can detect: no error, no timeout, forever.
#
# Observed on the box at 02:07 IST on 4 Sep 2026, one hour fifty-eight minutes into a re-run:
#
#     207399 | Lock/transactionid | blocked_by = 207394
#     207394 | Client/ClientRead  | blocked_by = (nothing)
#
# Both tests below are written to FAIL rather than hang if it comes back.
# =====================================================================================


class TestTheFallbackJoinsTheChainsTransaction:
    async def test_it_opens_no_second_connection_when_given_a_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seam itself: a caller with a transaction must never get a second one."""

        def explode(*args: object, **kwargs: object) -> NoReturn:
            raise AssertionError(
                "opened a second connection inside the chain's transaction — this is the "
                "1h58m hang of 4 Sep 2026"
            )

        monkeypatch.setattr("baskfy_worker.bhavcopy_backfill.session_scope", explode)
        # `cast` rather than a real session: the point of this test is that the function does
        # its reads on whatever it was handed and opens nothing of its own, and a double that
        # answers two queries with nothing proves that where a live session could not.
        report = await backfill_bars_from_bhavcopy(
            _NoBhavcopy(), WINDOW, session=cast(AsyncSession, _SessionWithNoInstruments())
        )
        # No bhavcopy port, so it stops at the setup check — having done its reads on our session.
        assert "setup" in report.failures

    @pytest.mark.db
    @requires_db
    async def test_it_writes_through_an_uncommitted_transaction_without_blocking(
        self, session: AsyncSession
    ) -> None:
        """The incident, in miniature, against a real database.

        An `instrument` row is written and **not committed** — exactly the state step 1 leaves the
        chain in — and then the bhavcopy path is asked to write a bar for it. On the same session
        that is ordinary. On a second connection it waits for a lock that will never be released,
        so this is wrapped in a timeout: a regression fails here in ten seconds instead of hanging
        the suite.
        """
        instrument = await make_instrument(session, "TATAMOTORS", token=884737)
        day = dt.date(2026, 9, 3)

        def fake_bhavcopy(on: dt.date) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "symbol": ["TATAMOTORS"],
                    "series": ["EQ"],
                    "date": [on],
                    "open": [Decimal("100")],
                    "high": [Decimal("101")],
                    "low": [Decimal("99")],
                    "close": [Decimal("100.5")],
                    "prev_close": [Decimal("99.5")],
                    "volume": [1000],
                    "turnover": [Decimal("100500")],
                    "upper_circuit": [Decimal("110")],
                    "lower_circuit": [Decimal("90")],
                }
            )

        provider = SimpleNamespace(bhavcopy=fake_bhavcopy)
        report = await asyncio.wait_for(
            backfill_bars_from_bhavcopy(
                provider, DateWindow.single(day), progress_every=0, session=session
            ),
            timeout=10,
        )

        assert report.failures == {}, report.failures
        assert report.bars_written >= 1
        # Visible inside the caller's transaction, which is the point: the chain publishes the
        # day or nothing, and a fallback that committed on its own broke that quietly.
        count = await session.scalar(
            text("select count(*) from ohlcv_daily where instrument_id = :i and date = :d"),
            {"i": instrument, "d": day},
        )
        assert count == 1


class _SessionWithNoInstruments:
    """Enough of an `AsyncSession` for the setup path: it answers both reads with nothing."""

    async def execute(self, *args: object, **kwargs: object) -> object:
        return _EmptyResult()

    async def scalars(self, *args: object, **kwargs: object) -> object:
        return _EmptyResult()


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[object]:
        return []

    def __iter__(self) -> Iterator[object]:
        return iter(())


class TestTheFetchDoesNotHoldRowLocks:
    """8 Sep 2026: the bars step took its first write lock in the first second and held it for
    the whole fetch, because the chain is deliberately one transaction (M84.1).

    Measured that day: the live swing scan sat on `Lock/transactionid` for 44 minutes while the
    chain crawled the bars at the bulk lane's 2 req/s, and with `--concurrency=2` both worker
    slots were then occupied by tasks doing nothing. py-spy put the chain in
    `RedisCallSpacer.acquire`, sleeping for its slot with the transaction open.

    The property that fixes it: **no write happens until every fetch is done.**
    """

    async def test_no_bar_is_written_until_every_fetch_has_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        class _Provider:
            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
                order.append(f"fetch:{token}")
                return pl.DataFrame(
                    {
                        "date": [start],
                        "open": [1.0],
                        "high": [1.0],
                        "low": [1.0],
                        "close": [1.0],
                        "volume": [1],
                        "turnover": [1.0],
                    }
                )

        async def fake_upsert(session: object, instrument_id: int, frame: pl.DataFrame) -> int:
            order.append(f"write:{instrument_id}")
            return frame.height

        monkeypatch.setattr("baskfy_worker.tasks.bars.upsert_bars", fake_upsert)

        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_Provider(),
            outcome=StepOutcome(),
            instruments=[(1, "AAA", 101), (2, "BBB", 102), (3, "CCC", 103)],
            window=WINDOW,
        )

        fetches = [i for i, entry in enumerate(order) if entry.startswith("fetch:")]
        writes = [i for i, entry in enumerate(order) if entry.startswith("write:")]
        assert fetches and writes
        assert max(fetches) < min(writes), (
            f"a write happened while fetches were still running: {order}"
        )

    async def test_a_long_window_still_flushes_so_memory_stays_bounded(self) -> None:
        """The escape hatch for a multi-year backfill, which does not share the box with a live
        scan and must not buffer the whole archive."""
        assert bars_module._FLUSH_ROWS > 0
        assert bars_module._FLUSH_ROWS >= 100_000, (
            "a threshold this low would flush during an ordinary session and reintroduce the bug"
        )


class TestTheBhavcopyLeadsForACompletedSession:
    """8 Sep 2026. Maulik: "why are we waiting so much?"

    Because `historical_data` has no batch form — one instrument per HTTP request, ~3,000
    requests, ~25 minutes at the bulk lane's 2 req/s. NSE's bhavcopy answers the same question
    in ONE request: measured for 7 Sep, a 207 KB zip with 3,704 rows (3,405 equity), which on
    its own cleared the quality gate's 3,171 threshold that day.

    So the file goes first and Kite is asked only for what it did not cover. Same coverage —
    the finished 7 Sep held 4,477 bars against the bhavcopy's 3,405, so Kite still matters — at
    roughly a third of the calls.
    """

    async def test_kite_is_only_asked_for_what_the_bhavcopy_missed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[int] = []

        class _Provider:
            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
                asked.append(token)
                return pl.DataFrame(
                    {
                        "date": [start],
                        "open": [1.0],
                        "high": [1.0],
                        "low": [1.0],
                        "close": [1.0],
                        "volume": [1],
                        "turnover": [1.0],
                    }
                )

        async def fake_bhavcopy(*a: object, **k: object) -> int:
            return 2

        # instruments 1 and 2 came from the file; only 3 is left for Kite
        async def fake_covered(session: object, on: dt.date) -> set[int]:
            return {1, 2}

        monkeypatch.setattr("baskfy_worker.tasks.bars._fetch_from_bhavcopy", fake_bhavcopy)
        monkeypatch.setattr("baskfy_worker.tasks.bars._instruments_with_bars", fake_covered)
        monkeypatch.setattr(
            "baskfy_worker.tasks.bars.upsert_bars",
            lambda session, instrument_id, frame: _async_int(frame.height),
        )

        outcome = StepOutcome()
        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_Provider(),
            outcome=outcome,
            instruments=[(1, "AAA", 101), (2, "BBB", 102), (3, "CCC", 103)],
            window=WINDOW,
        )

        assert asked == [103], f"Kite was asked for names the bhavcopy already had: {asked}"
        assert outcome.detail["kite_calls_saved"] == 2
        assert outcome.detail["kite_calls_remaining"] == 1

    async def test_a_missing_file_falls_through_to_kite_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running before NSE publishes, or NSE refusing, must not cost the session — the whole
        Kite pass still runs exactly as it did before."""
        asked: list[int] = []

        class _Provider:
            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
                asked.append(token)
                return pl.DataFrame(
                    {
                        "date": [start],
                        "open": [1.0],
                        "high": [1.0],
                        "low": [1.0],
                        "close": [1.0],
                        "volume": [1],
                        "turnover": [1.0],
                    }
                )

        async def boom(*a: object, **k: object) -> int:
            raise RuntimeError("bhavcopy not published yet")

        monkeypatch.setattr("baskfy_worker.tasks.bars._fetch_from_bhavcopy", boom)
        monkeypatch.setattr(
            "baskfy_worker.tasks.bars.upsert_bars",
            lambda session, instrument_id, frame: _async_int(frame.height),
        )

        outcome = StepOutcome()
        await run_fetch_daily_bars(
            session=AsyncSession(),
            provider=_Provider(),
            outcome=outcome,
            instruments=[(1, "AAA", 101), (2, "BBB", 102)],
            window=WINDOW,
        )

        assert asked == [101, 102], "a missing file must not skip the Kite pass"
        assert "bhavcopy_first_error" in outcome.detail


async def _async_int(value: int) -> int:
    return value
