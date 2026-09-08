"""SW15 — "Scan now": detection on demand, intraday from Kite quotes, against a real database.

Maulik, 3 Sep 2026: "I wanted to have the scan anytime, and since we have the Kite API, we
should have all the data." What is asserted here, in the order a person meets it:

* the task decides the session — today, provisional, between 09:15 and 15:30 IST on a trading
  day; the last published session, plain, at any other time (`decide_session`);
* a provisional bar is built per quoted name in the adjusted space, a quote with no volume is
  still a bar, and a name with no factor is skipped rather than invented (`provisional_bars`);
* the whole loop: a textbook flag whose 140th bar is a live quote is detected at 13:42 and
  every row written says ``provisional``; the nightly for the same date then replaces them —
  the same key flips to ``False`` and a provisional row it did not re-detect is deleted;
* fail soft: a quote read that raises is a ``FAILED`` run with the reason and nothing written;
* the quote pull is exactly the liquid universe as of the last close;
* the sweep publishes queued rows once, and the Celery binding is named and routed.

The flag fixture is `test_swing_detect`'s: 139 published bars of its shape and the 140th as a
quote, so the only thing that differs from the nightly's proof is where the last bar came from.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from decimal import Decimal

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_swing_detect import _flag_shape, _sessions_before

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    SwConfig,
    SwMarketDaily,
    SwScanRun,
    SwSetupDaily,
)
from baskfy_core.swing.config import Setup
from baskfy_providers.records import QuoteRecord
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, QUEUE_DEFAULT, TASK_ROUTES
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import swing_scan_now
from baskfy_worker.tasks.celery_tasks import (
    SWING_SCAN_AFTER_LOGIN_TASK,
    SWING_SCAN_NOW_TASK,
    SWING_SCAN_SWEEP_TASK,
    request_login_scan,
    swing_scan_after_login_task,
    swing_scan_now_task,
    swing_scan_sweep_task,
)
from baskfy_worker.tasks.swing import run_detect_swing
from baskfy_worker.tasks.swing_premarket import LiquidName, QuoteSource
from baskfy_worker.tasks.swing_scan_now import (
    DONE,
    FAILED,
    QUEUED,
    REASON_AFTER_CLOSE,
    REASON_ALREADY_PUBLISHED,
    REASON_MARKET_OPEN,
    REASON_PUBLISHED,
    ScanNotRunnable,
    ScanReport,
    decide_session,
    market_is_open,
    provisional_bars,
    run_scan_now,
    sweep_queued,
)

pytestmark = requires_db

#: A Tuesday inside the seeded calendar, far enough into it that 140 sessions precede it.
TODAY = dt.date(2026, 8, 18)
THIRTEEN_FORTY_TWO = dt.datetime(2026, 8, 18, 13, 42)
SIX_PM = dt.datetime(2026, 8, 18, 18, 0)
UTC_NOON = dt.datetime(2026, 8, 18, 8, 12, tzinfo=dt.UTC)


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="sw15-user", email="sw15@example.com")
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


def _bar_numbers(index: int, close: float, total: int) -> tuple[float, float, int]:
    """`test_swing_detect._write_flag`'s high, low and volume for bar ``index`` of ``total``."""
    pole_bars, base_bars = 20, 35
    flat = total - pole_bars - base_bars
    volume = 1_000_000 if index < flat else 3_000_000
    if index >= flat + pole_bars:
        volume = int(1_500_000 - 1_000_000 * (index - flat - pole_bars) / base_bars)
    return close * 1.025, close * 0.975, volume


async def _write_flag_but_the_last_bar(
    session: AsyncSession, symbol: str, dates: Sequence[dt.date], *, adj_factor: str = "1"
) -> tuple[int, QuoteRecord]:
    """139 published bars of the textbook flag, and the 140th as the quote Kite would give at
    13:42 — open/high/low/close of the day so far, the session's volume so far."""
    factor = Decimal(adj_factor)
    instrument_id = await make_instrument(session, symbol)
    closes = _flag_shape(len(dates))
    for index, (on, close) in enumerate(zip(dates[:-1], closes[:-1], strict=True)):
        high, low, volume = _bar_numbers(index, close, len(dates))
        session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=on,
                open=Decimal(str(round(close, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(close, 4))),
                volume=volume,
                close_raw=Decimal(str(round(close / float(factor), 4))),
                volume_raw=volume,
                turnover=Decimal(str(round(close * volume, 2))),
                adj_factor=factor,
                source="nse",
            )
        )
    await session.flush()
    last = closes[-1]
    high, low, volume = _bar_numbers(len(dates) - 1, last, len(dates))
    raw = 1 / float(factor)
    quote = QuoteRecord(
        symbol=symbol,
        last_price=Decimal(str(round(last * raw, 2))),
        volume=volume,
        open=Decimal(str(round(last * raw, 2))),
        high=Decimal(str(round(high * raw, 2))),
        low=Decimal(str(round(low * raw, 2))),
        prev_close=Decimal(str(round(closes[-2] * raw, 2))),
        upper_circuit=Decimal(str(round(last * raw * 1.2, 2))),
    )
    return instrument_id, quote


async def _write_real_last_bar(
    session: AsyncSession, instrument_id: int, on: dt.date, quote: QuoteRecord
) -> None:
    """Tonight's publish of the bar the quote previewed."""
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=on,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.last_price,
            volume=quote.volume,
            close_raw=quote.last_price,
            volume_raw=quote.volume,
            turnover=quote.last_price * quote.volume,
            adj_factor=Decimal(1),
            source="nse",
        )
    )
    await session.flush()


async def _queue(session: AsyncSession, user_id: int, *, source: str = "web") -> int:
    row = SwScanRun(user_id=user_id, status=QUEUED, detail={"source": source})
    session.add(row)
    await session.flush()
    return int(row.id)


class CountingQuotes:
    def __init__(self, answers: list[QuoteRecord] | None = None) -> None:
        self.answers = answers or []
        self.requests: list[list[str]] = []

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        self.requests.append(list(symbols))
        return [quote for quote in self.answers if quote.symbol in set(symbols)]


class ExplodingQuotes:
    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        raise RuntimeError("Kite: TokenException: Incorrect `api_key` or `access_token`.")


def _never_asked() -> CountingQuotes:
    raise AssertionError("the quote source was built outside market hours")


async def _scan(  # noqa: PLR0913 - one keyword per input the scan depends on
    session: AsyncSession,
    *,
    user_id: int,
    run_id: int,
    quotes: CountingQuotes | ExplodingQuotes | None,
    now: dt.datetime = THIRTEEN_FORTY_TWO,
    build_source: bool = True,
) -> ScanReport:
    source: Callable[[], QuoteSource] | None
    if quotes is not None:
        answers = quotes
        source = lambda: answers  # noqa: E731 - the factory the task hands over
    else:
        source = _never_asked if build_source else None
    return await run_scan_now(
        session,
        run_id=run_id,
        user_id=user_id,
        index_slug="nifty-500",
        execution_enabled=False,
        quote_source=source,
        now=now,
        clock=lambda: UTC_NOON,
    )


async def _setups(session: AsyncSession, on: dt.date) -> list[SwSetupDaily]:
    rows = await session.execute(
        sa.select(SwSetupDaily).where(SwSetupDaily.date == on).order_by(SwSetupDaily.setup)
    )
    return list(rows.scalars())


async def _market(session: AsyncSession, on: dt.date) -> SwMarketDaily | None:
    return (
        await session.execute(sa.select(SwMarketDaily).where(SwMarketDaily.date == on))
    ).scalar_one_or_none()


async def _run(session: AsyncSession, run_id: int) -> SwScanRun:
    return (await session.execute(sa.select(SwScanRun).where(SwScanRun.id == run_id))).scalar_one()


# --- the decision --------------------------------------------------------------------


class TestTheSessionDecision:
    @pytest.mark.parametrize(
        ("when", "open_"),
        [
            (dt.time(9, 14), False),
            (dt.time(9, 15), True),
            (dt.time(13, 42), True),
            (dt.time(15, 30), True),
            (dt.time(15, 31), False),
        ],
    )
    def test_the_market_is_open_between_0915_and_1530_inclusive(
        self, when: dt.time, open_: bool
    ) -> None:
        assert market_is_open(dt.datetime.combine(TODAY, when)) is open_

    async def test_during_the_session_it_scans_today_provisionally(
        self, session: AsyncSession
    ) -> None:
        dates = await _sessions_before(session, TODAY, 140)
        await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        decision = await decide_session(session, THIRTEEN_FORTY_TWO)
        assert decision.session_date == TODAY
        assert decision.provisional is True
        assert decision.published_as_of == dates[-2]
        assert decision.reason == REASON_MARKET_OPEN

    async def test_before_the_open_it_scans_the_last_published_session_plainly(
        self, session: AsyncSession
    ) -> None:
        """Only BEFORE the open now (widened 8 Sep 2026). A quote at 08:00 carries yesterday's
        close, so stamping it as today would be a lie the detectors act on."""
        dates = await _sessions_before(session, TODAY, 140)
        await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        for when in (dt.datetime(2026, 8, 18, 8, 0), dt.datetime(2026, 8, 18, 9, 14)):
            decision = await decide_session(session, when)
            assert decision.session_date == dates[-2], when
            assert decision.provisional is False
            assert decision.reason == REASON_PUBLISHED

    async def test_after_the_close_it_still_scans_today(self, session: AsyncSession) -> None:
        """This used to answer "the last published session" and that was the bug Maulik hit:
        a 16:00 or 18:00 login was served yesterday for the hours between the close and the
        nightly, on a day whose prices were final and readable from Kite."""
        dates = await _sessions_before(session, TODAY, 140)
        await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        for when in (dt.datetime(2026, 8, 18, 16, 0), SIX_PM):
            decision = await decide_session(session, when)
            assert decision.session_date == TODAY, when
            assert decision.provisional is True
            assert decision.reason == REASON_AFTER_CLOSE

    async def test_a_day_already_published_is_never_scanned_twice_over(
        self, session: AsyncSession
    ) -> None:
        """A redelivered task waking after the nightly: today is published, so no provisional
        bar goes on top of a real one."""
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        await _write_real_last_bar(session, instrument_id, TODAY, quote)
        decision = await decide_session(session, THIRTEEN_FORTY_TWO)
        assert (decision.session_date, decision.provisional) == (TODAY, False)

    async def test_nothing_published_is_not_runnable(self, session: AsyncSession) -> None:
        with pytest.raises(ScanNotRunnable, match="no published bars"):
            await decide_session(session, THIRTEEN_FORTY_TWO)


# --- the bar ---------------------------------------------------------------------------


def _name(instrument_id: int, symbol: str) -> LiquidName:
    return LiquidName(
        instrument_id=instrument_id,
        symbol=symbol,
        avg_daily_volume=Decimal(1_000_000),
        prev_close=Decimal("100"),
    )


def _quote(symbol: str, last: str, *, volume: int, ohlc: bool = True) -> QuoteRecord:
    return QuoteRecord(
        symbol=symbol,
        last_price=Decimal(last),
        volume=volume,
        open=Decimal("101") if ohlc else None,
        high=Decimal("104") if ohlc else None,
        low=Decimal("99") if ohlc else None,
        upper_circuit=Decimal("120"),
    )


class TestTheProvisionalBar:
    def test_one_bar_per_quote_in_the_adjusted_space(self) -> None:
        """A 1:2 split left `adj_factor` 0.5: the quote is the exchange print and the bar is
        half of it, as the published series is; turnover stays raw money; the circuit band is
        adjusted the way `load_swing_bars` adjusts the published one."""
        names = [_name(1, "SPLITCO")]
        build = provisional_bars(
            names,
            [_quote("SPLITCO", "102", volume=400_000)],
            factors={1: 0.5},
            on=TODAY,
        )
        assert build.frame.height == 1
        row = build.frame.row(0, named=True)
        assert row["date"] == TODAY
        assert (row["open"], row["high"], row["low"], row["close"]) == (50.5, 52.0, 49.5, 51.0)
        assert row["volume"] == 400_000.0
        assert row["turnover"] == 102 * 400_000
        assert row["upper_circuit"] == 60.0
        assert row["adj_factor"] == 0.5
        assert build.quotes == 1
        assert build.skipped == {"no_quote": 0, "no_factor": 0, "bad_price": 0}

    def test_a_quote_with_no_volume_is_still_a_bar(self) -> None:
        build = provisional_bars(
            [_name(1, "QUIET")], [_quote("QUIET", "100", volume=0)], factors={1: 1.0}, on=TODAY
        )
        assert build.frame.height == 1
        assert build.frame.row(0, named=True)["turnover"] == 0.0

    def test_a_missing_ohlc_side_takes_the_last_price_and_the_high_low_bracket_it(self) -> None:
        build = provisional_bars(
            [_name(1, "THIN")],
            [_quote("THIN", "100", volume=10, ohlc=False)],
            factors={1: 1.0},
            on=TODAY,
        )
        row = build.frame.row(0, named=True)
        assert (row["open"], row["high"], row["low"], row["close"]) == (100.0, 100.0, 100.0, 100.0)

    def test_a_name_with_no_factor_is_skipped_not_invented_and_a_silent_name_is_counted(
        self,
    ) -> None:
        """No factor means the name's last bar is not the last session; no quote means Kite did
        not answer for it. Both are counted so the run row says why the universe shrank."""
        names = [_name(1, "STALE"), _name(2, "SILENT"), _name(3, "FINE"), _name(4, "ZERO")]
        build = provisional_bars(
            names,
            [
                _quote("STALE", "100", volume=1),
                _quote("FINE", "100", volume=1),
                _quote("ZERO", "0", volume=1),
                _quote("STRANGER", "100", volume=1),
            ],
            factors={2: 1.0, 3: 1.0, 4: 1.0},
            on=TODAY,
        )
        assert build.frame["symbol"].to_list() == ["FINE"]
        assert build.skipped == {"no_quote": 1, "no_factor": 1, "bad_price": 1}
        assert build.quotes == 4

    def test_no_quotes_is_an_empty_frame_with_the_bar_columns(self) -> None:
        build = provisional_bars([_name(1, "A")], [], factors={1: 1.0}, on=TODAY)
        assert build.frame.is_empty()
        assert "adj_factor" in build.frame.columns
        assert build.frame.schema["date"] == pl.Date


# --- the whole loop --------------------------------------------------------------------


class TestScanNowDuringTheSession:
    async def test_a_flag_whose_last_bar_is_a_live_quote_is_detected_and_flagged_provisional(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        run_id = await _queue(session, user_id)
        quotes = CountingQuotes([quote])

        report = await _scan(session, user_id=user_id, run_id=run_id, quotes=quotes)

        assert report.status == DONE
        assert report.session_date == TODAY
        assert report.provisional is True
        assert report.candidates == 1
        rows = await _setups(session, TODAY)
        assert [(row.instrument_id, row.setup) for row in rows] == [
            (instrument_id, Setup.FLAG.value)
        ]
        assert rows[0].provisional is True
        assert rows[0].user_id == user_id
        assert rows[0].close is not None
        assert rows[0].trigger is not None and rows[0].trigger > rows[0].close
        market = await _market(session, TODAY)
        assert market is not None
        assert market.provisional is True
        assert isinstance(market.detail, dict)
        scan = market.detail["scan"]
        assert isinstance(scan, dict)
        assert scan["run_id"] == run_id
        assert scan["provisional"] is True
        assert scan["scanned_at"] == UTC_NOON.isoformat()
        assert scan["quotes"] == 1
        funnel = market.detail["funnel"]
        assert isinstance(funnel, dict)
        assert funnel["with_a_bar_today"] == 1
        assert funnel["candidates"] == {"FLAG": 1, "EP": 0, "PARABOLIC_SHORT": 0}

    async def test_the_run_row_records_when_what_and_the_funnel(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        _, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        run_id = await _queue(session, user_id, source="desk")

        await _scan(session, user_id=user_id, run_id=run_id, quotes=CountingQuotes([quote]))

        run = await _run(session, run_id)
        assert run.status == DONE
        assert run.started_at == UTC_NOON and run.finished_at == UTC_NOON
        assert run.session_date == TODAY
        assert run.provisional is True
        assert run.error is None
        assert isinstance(run.detail, dict)
        assert run.detail["source"] == "desk"
        assert run.detail["reason"] == REASON_MARKET_OPEN
        assert run.detail["candidates"] == 1
        assert run.detail["quotes"] == 1
        assert run.detail["bars_built"] == 1
        assert run.detail["skipped"] == {"no_quote": 0, "no_factor": 0, "bad_price": 0}
        assert isinstance(run.detail["funnel"], dict)

    async def test_the_quote_pull_is_the_liquid_universe_as_of_the_last_close(
        self, session: AsyncSession
    ) -> None:
        """One request, the liquid names only — the thin name is never quoted (B10: quotes
        cost a limiter token a call, and the universe is `04` §1's, not the whole register)."""
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        _, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        thin = await make_instrument(session, "THINCO")
        for on in dates[:-1]:
            session.add(
                OhlcvDaily(
                    instrument_id=thin,
                    date=on,
                    open=Decimal(100),
                    high=Decimal(101),
                    low=Decimal(99),
                    close=Decimal(100),
                    volume=100,
                    close_raw=Decimal(100),
                    volume_raw=100,
                    turnover=Decimal(10_000),
                    adj_factor=Decimal(1),
                    source="nse",
                )
            )
        await session.flush()
        run_id = await _queue(session, user_id)
        quotes = CountingQuotes([quote])

        await _scan(session, user_id=user_id, run_id=run_id, quotes=quotes)

        assert quotes.requests == [["FLAGCO"]]

    async def test_a_quote_failure_is_a_failed_run_with_nothing_written(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        run_id = await _queue(session, user_id)

        report = await _scan(session, user_id=user_id, run_id=run_id, quotes=ExplodingQuotes())

        assert report.status == FAILED
        assert report.error is not None and "TokenException" in report.error
        run = await _run(session, run_id)
        assert run.status == FAILED
        assert run.error == report.error
        assert run.finished_at == UTC_NOON
        assert run.session_date == TODAY and run.provisional is True
        assert await _setups(session, TODAY) == []
        assert await _market(session, TODAY) is None

    async def test_no_kite_session_during_market_hours_is_a_failed_run_that_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        run_id = await _queue(session, user_id)

        report = await _scan(
            session, user_id=user_id, run_id=run_id, quotes=None, build_source=False
        )

        assert report.status == FAILED
        assert report.error is not None and "no Kite quote source" in report.error
        assert await _setups(session, TODAY) == []

    async def test_a_run_for_another_user_is_refused(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        run_id = await _queue(session, user_id)
        with pytest.raises(ScanNotRunnable, match="no sw_scan_run"):
            await run_scan_now(
                session,
                run_id=run_id,
                user_id=user_id + 1,
                index_slug="nifty-500",
                execution_enabled=False,
                quote_source=None,
                now=THIRTEEN_FORTY_TWO,
            )


class TestScanNowOutsideTheSession:
    async def test_it_re_runs_the_last_published_session_and_opens_no_kite_session(
        self, session: AsyncSession
    ) -> None:
        """At 18:00 the last close is the session; the nightly's body runs over published bars
        and the quote source is never even built."""
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        await _write_real_last_bar(session, instrument_id, TODAY, quote)
        run_id = await _queue(session, user_id)

        report = await _scan(session, user_id=user_id, run_id=run_id, quotes=None, now=SIX_PM)

        assert report.status == DONE
        assert report.session_date == TODAY
        assert report.provisional is False
        rows = await _setups(session, TODAY)
        assert [row.setup for row in rows] == [Setup.FLAG.value]
        assert rows[0].provisional is False
        market = await _market(session, TODAY)
        assert market is not None and market.provisional is False
        assert isinstance(market.detail, dict)
        scan = market.detail["scan"]
        assert isinstance(scan, dict) and scan["provisional"] is False
        run = await _run(session, run_id)
        assert run.detail is not None
        assert run.detail["reason"] == REASON_ALREADY_PUBLISHED
        assert "quotes" not in run.detail


class TestTheNightlyReplacesProvisionalRows:
    async def test_the_nightly_overwrites_the_same_key_and_removes_what_it_did_not_redetect(
        self, session: AsyncSession
    ) -> None:
        """G5. 13:42: the flag is provisional. 21:00: the bar is published, the nightly runs for
        the same date — the flag row flips to `provisional=False`, a provisional row the
        nightly did not re-detect is gone, and the market row is the evening's."""
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        run_id = await _queue(session, user_id)
        await _scan(session, user_id=user_id, run_id=run_id, quotes=CountingQuotes([quote]))
        ghost = await make_instrument(session, "GHOSTCO")
        session.add(
            SwSetupDaily(
                user_id=user_id,
                date=TODAY,
                instrument_id=ghost,
                setup=Setup.EP.value,
                status="SETTING_UP",
                score=Decimal("50.00"),
                provisional=True,
            )
        )
        await session.flush()
        assert {(r.instrument_id, r.provisional) for r in await _setups(session, TODAY)} == {
            (instrument_id, True),
            (ghost, True),
        }

        await _write_real_last_bar(session, instrument_id, TODAY, quote)
        outcome = StepOutcome()
        written = await run_detect_swing(session, outcome, TODAY, user_id=user_id)

        assert written == 1
        rows = await _setups(session, TODAY)
        assert [(r.instrument_id, r.setup, r.provisional) for r in rows] == [
            (instrument_id, Setup.FLAG.value, False)
        ]
        market = await _market(session, TODAY)
        assert market is not None
        assert market.provisional is False
        assert isinstance(market.detail, dict)
        assert "scan" not in market.detail
        assert isinstance(market.detail["funnel"], dict)

    async def test_a_second_scan_replaces_the_first_scans_provisional_rows(
        self, session: AsyncSession
    ) -> None:
        """A flag that was there at 11:00 and is not at 13:42 does not linger either: by 13:42
        the name has broken down 30% through its base, and the 11:00 row goes with it."""
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        first = await _queue(session, user_id)
        await _scan(session, user_id=user_id, run_id=first, quotes=CountingQuotes([quote]))
        eleven = await _setups(session, TODAY)
        assert [(r.instrument_id, r.provisional) for r in eleven] == [(instrument_id, True)]

        crash = (quote.last_price * Decimal("0.7")).quantize(Decimal("0.01"))
        broken = quote.model_copy(update={"last_price": crash, "low": crash})
        second = await _queue(session, user_id)
        report = await _scan(
            session, user_id=user_id, run_id=second, quotes=CountingQuotes([broken])
        )

        assert report.status == DONE
        rows = await _setups(session, TODAY)
        assert all(r.provisional for r in rows)
        assert not any(r.setup == Setup.FLAG.value and r.close == eleven[0].close for r in rows), (
            "the 11:00 flag row survived a scan that did not see it"
        )
        market = await _market(session, TODAY)
        assert market is not None and isinstance(market.detail, dict)
        scan = market.detail["scan"]
        assert isinstance(scan, dict) and scan["run_id"] == second

    async def test_a_scan_with_nothing_detected_clears_the_previous_provisional_rows(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        dates = await _sessions_before(session, TODAY, 140)
        instrument_id, quote = await _write_flag_but_the_last_bar(session, "FLAGCO", dates)
        first = await _queue(session, user_id)
        await _scan(session, user_id=user_id, run_id=first, quotes=CountingQuotes([quote]))
        assert len(await _setups(session, TODAY)) == 1

        # The second scan gets no quote for the name: no bar today, nothing detected, and the
        # 11:00 row goes rather than lingering as a flag nobody re-saw.
        second = await _queue(session, user_id)
        report = await _scan(session, user_id=user_id, run_id=second, quotes=CountingQuotes([]))

        assert report.status == DONE
        assert report.candidates == 0
        assert await _setups(session, TODAY) == []
        assert instrument_id not in {r.instrument_id for r in await _setups(session, TODAY)}


# --- the sweep and the binding ----------------------------------------------------------


class TestTheSweep:
    async def test_it_publishes_queued_rows_without_a_task_id_once(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        desk = await _queue(session, user_id, source="desk")
        published_already = await _queue(session, user_id)
        (await _run(session, published_already)).task_id = "already-sent"
        done = await _queue(session, user_id)
        (await _run(session, done)).status = DONE
        await session.flush()
        sent: list[int] = []

        def publish(run_id: int) -> str:
            sent.append(run_id)
            return f"task-{run_id}"

        assert await sweep_queued(session, user_id=user_id, publish=publish) == [desk]
        assert sent == [desk]
        assert (await _run(session, desk)).task_id == f"task-{desk}"
        assert await sweep_queued(session, user_id=user_id, publish=publish) == []
        assert sent == [desk]


class TestTheLoginTrigger:
    async def test_an_afternoon_login_creates_a_provisional_scan_once(
        self, session: AsyncSession
    ) -> None:
        dates = await _sessions_before(session, TODAY, 140)
        user_id = await _user(session)
        await _write_flag_but_the_last_bar(session, "LOGINSCAN", dates)
        requested = dt.datetime(2026, 8, 18, 8, 12, tzinfo=dt.UTC)

        result = await request_login_scan(
            session,
            user_id=user_id,
            market_now=THIRTEEN_FORTY_TWO,
            requested_at=requested,
            task_id="login-task-1",
        )

        run_id = result["run_id"]
        assert isinstance(run_id, int)
        run = await _run(session, run_id)
        assert run.status == QUEUED
        assert run.provisional is False
        assert run.task_id == "login-task-1"
        assert run.detail == {"source": "broker-login"}

        duplicate = await request_login_scan(
            session,
            user_id=user_id,
            market_now=THIRTEEN_FORTY_TWO,
            requested_at=requested + dt.timedelta(seconds=1),
            task_id="login-task-2",
        )
        assert duplicate["skipped"] == "scan-in-flight"
        assert duplicate["run_id"] == run.id

    async def test_after_close_login_still_scans_today(self, session: AsyncSession) -> None:
        """M85 wrote the opposite of this — an after-close login "defers to the published data
        path". Maulik asked for the reverse on 8 Sep: a login at any hour after the open should
        fetch today. Overriding my own earlier decision, at his instruction."""
        dates = await _sessions_before(session, TODAY, 140)
        user_id = await _user(session)
        await _write_flag_but_the_last_bar(session, "LOGINLATE", dates)

        result = await request_login_scan(
            session,
            user_id=user_id,
            market_now=SIX_PM,
            requested_at=dt.datetime(2026, 8, 18, 12, 30, tzinfo=dt.UTC),
            task_id="login-task-late",
        )

        assert "skipped" not in result, "an after-close login was served yesterday"
        assert "run_id" in result


class TestTheCeleryBinding:
    def test_scan_now_is_named_and_routed_to_the_compute_queue_with_no_beat_entry(self) -> None:
        assert SWING_SCAN_NOW_TASK == "baskfy.swing.scan_now"
        assert swing_scan_now_task.name == SWING_SCAN_NOW_TASK
        assert swing_scan_now_task.queue == QUEUE_COMPUTE
        assert swing_scan_now_task.acks_late is True
        assert TASK_ROUTES[SWING_SCAN_NOW_TASK]["queue"] == QUEUE_COMPUTE
        assert all(entry["task"] != SWING_SCAN_NOW_TASK for entry in BEAT_SCHEDULE.values())

    def test_login_scan_is_a_separate_compute_task_with_no_beat_entry(self) -> None:
        assert SWING_SCAN_AFTER_LOGIN_TASK == "baskfy.swing.scan_after_login"
        assert swing_scan_after_login_task.name == SWING_SCAN_AFTER_LOGIN_TASK
        assert swing_scan_after_login_task.queue == QUEUE_COMPUTE
        assert swing_scan_after_login_task.acks_late is True
        assert TASK_ROUTES[SWING_SCAN_AFTER_LOGIN_TASK]["queue"] == QUEUE_COMPUTE
        assert all(entry["task"] != SWING_SCAN_AFTER_LOGIN_TASK for entry in BEAT_SCHEDULE.values())

    def test_the_sweep_runs_every_minute_on_the_default_queue(self) -> None:
        assert swing_scan_sweep_task.name == SWING_SCAN_SWEEP_TASK
        entry = BEAT_SCHEDULE["swing-scan-sweep"]
        assert entry["task"] == SWING_SCAN_SWEEP_TASK
        assert entry["schedule"] == dt.timedelta(seconds=60)
        assert entry["options"] == {"queue": QUEUE_DEFAULT}
        assert TASK_ROUTES[SWING_SCAN_SWEEP_TASK]["queue"] == QUEUE_DEFAULT

    def test_without_a_sole_user_the_tasks_skip_rather_than_inventing_a_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_SOLE_USER_ID", raising=False)
        assert swing_scan_now_task(run_id=1) == {
            "run_id": 1,
            "skipped": "no BASKFY_SOLE_USER_ID configured",
        }
        assert swing_scan_sweep_task() == {
            "published": [],
            "skipped": "no BASKFY_SOLE_USER_ID configured",
        }


class TestALoginAtAnyHourGetsTodaysData:
    """Maulik, 8 Sep 2026: "given any period of the day, whenever user connects zerodha, start
    fetching data across the instruments, so we show all the data live across the site."

    The scan built today's provisional bar only between 09:15 and 15:30, so a login at 16:00 —
    after the close, before the nightly publishes around 18:45 — was served *yesterday* for two
    and a half hours, on a day whose prices were final and already readable from Kite.
    """

    def test_the_window_opens_at_the_open_and_stays_open(self) -> None:
        started = swing_scan_now.session_has_started
        assert started(dt.datetime(2026, 9, 8, 9, 15)) is True
        assert started(dt.datetime(2026, 9, 8, 13, 0)) is True
        assert started(dt.datetime(2026, 9, 8, 16, 0)) is True, "the 16:00 login this exists for"
        assert started(dt.datetime(2026, 9, 8, 23, 59)) is True

    def test_before_the_open_it_is_shut_and_that_is_deliberate(self) -> None:
        """The asymmetry is the point. After the close a quote is the day's final traded price,
        so a provisional bar is accurate. Before 09:15 a quote is YESTERDAY's close, and
        stamping that as today would be a lie the detectors then act on."""
        started = swing_scan_now.session_has_started
        assert started(dt.datetime(2026, 9, 8, 0, 1)) is False
        assert started(dt.datetime(2026, 9, 8, 8, 0)) is False
        assert started(dt.datetime(2026, 9, 8, 9, 14)) is False

    def test_it_is_strictly_wider_than_market_is_open_and_never_narrower(self) -> None:
        """Whatever the old window allowed, the new one must still allow — this widened the
        behaviour, it did not move it."""
        for hour in range(24):
            for minute in (0, 14, 15, 30, 59):
                moment = dt.datetime(2026, 9, 8, hour, minute)
                if swing_scan_now.market_is_open(moment):
                    assert swing_scan_now.session_has_started(moment), moment
