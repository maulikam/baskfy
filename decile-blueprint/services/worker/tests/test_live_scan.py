"""LV8 — the TWT and VBT scans read today so far during the session, and their entries are taken
now, at market (DECISIONS-LV LV8.0, Maulik 28 Sep 2026; DECISIONS-TW TW19; DECISIONS-VB VB16).

What is asserted, in the order the gates file lists it (``gates/live-8-live-scans.md``):

* **the bar** — one per quoted name, in the sleeve's own schema, adjusted by the name's last
  factor with the raw close beside it; a name without a quote or a factor is skipped and counted;
* **the decision** — shared with the swing book: before 09:15 or after the publish, the last
  published session; in session, today, provisionally; with no Kite session, the published one
  and a note saying why;
* **the rows** — stamped ``provisional``, never ratcheted, replaced by the nightly and swept when
  the real bar does not reproduce them;
* **the plan** — ``LIVE``, thirty minutes, TWT entries ``BUY_AT_OPEN`` and VBT entries
  ``BUY_AT_MARKET``, none when there is nothing to enter.

Nothing here places anything, and nothing here can: these modules name no gateway.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Sequence
from decimal import Decimal

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_twt_evening import _breadth as twt_breadth
from test_twt_evening import _signal as twt_signal
from test_twt_evening import _user as twt_user
from test_twt_scan_task import _published, _queued
from test_vbt_evening import AS_OF as VBT_AS_OF
from test_vbt_evening import _calendar as vbt_calendar
from test_vbt_evening import _signal as vbt_signal
from test_vbt_evening import _user as vbt_user

from baskfy_core.models import (
    TwBreadthDaily,
    TwOrder,
    TwPlan,
    TwPlanLine,
    TwSignalDaily,
    TwStateDaily,
    VbBreadthDaily,
    VbPlan,
    VbPlanLine,
    VbScanRun,
    VbSignalDaily,
)
from baskfy_core.twt.config import SignalState as TwtSignalState
from baskfy_core.vbt.config import SignalState as VbtSignalState
from baskfy_core.vbt.plan import LineKind as VbtLineKind
from baskfy_providers.records import QuoteRecord
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import live_scan, swing_scan_now, twt, twt_scan, vbt, vbt_rescan
from baskfy_worker.tasks.live_scan import (
    IST,
    SOURCE_LIVE,
    LiveName,
    build_live_plan_twt,
    build_live_plan_vbt,
    provisional_daily_bars,
)
from baskfy_worker.tasks.twt import BAR_SCHEMA as TWT_SCHEMA
from baskfy_worker.tasks.vbt import BAR_SCHEMA as VBT_SCHEMA

pytestmark = [requires_db, pytest.mark.db]

#: A published Thursday and the Friday after it — the day a live scan is of.
PUBLISHED = dt.date(2026, 9, 10)
TODAY = dt.date(2026, 9, 11)


def _ist(day: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


def _quote(symbol: str, last: str, *, volume: int = 500, ohlc: bool = True) -> QuoteRecord:
    return QuoteRecord(
        symbol=symbol,
        last_price=Decimal(last),
        volume=volume,
        open=Decimal("101") if ohlc else None,
        high=Decimal("104") if ohlc else None,
        low=Decimal("99") if ohlc else None,
        upper_circuit=Decimal("120"),
    )


class Quotes:
    def __init__(self, answers: Sequence[QuoteRecord]) -> None:
        self.answers = list(answers)
        self.requests: list[list[str]] = []

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        self.requests.append(list(symbols))
        return [q for q in self.answers if q.symbol in set(symbols)]


def _never() -> Quotes:
    raise AssertionError("the quote source was built outside the provisional path")


# --- the bar -----------------------------------------------------------------------------------


class TestTheProvisionalBar:
    def test_bars_one_per_quoted_name_in_the_sleeve_schema_adjusted_with_the_raw_close(
        self,
    ) -> None:
        names = [LiveName(1, "AAA"), LiveName(2, "BBB")]
        build = provisional_daily_bars(
            names,
            [_quote("AAA", "102.50", volume=700), _quote("BBB", "50")],
            factors={1: 2.0, 2: 1.0},
            on=TODAY,
            schema=TWT_SCHEMA,
        )
        assert build.frame.schema == TWT_SCHEMA
        assert build.frame.height == 2 and build.quotes == 2
        aaa = build.frame.filter(pl.col("symbol") == "AAA").row(0, named=True)
        assert aaa["close"] == 205.0 and aaa["close_raw"] == 102.5, "adjusted close, raw beside it"
        assert aaa["open"] == 202.0 and aaa["high"] == 208.0 and aaa["low"] == 198.0
        assert aaa["volume"] == 700.0 and aaa["upper_circuit"] == 240.0
        assert aaa["adj_factor"] == 2.0 and aaa["is_etf"] is False and aaa["date"] == TODAY
        assert build.skipped == {"no_quote": 0, "no_factor": 0, "bad_price": 0}

    def test_bars_in_the_vbt_schema_carry_no_etf_column(self) -> None:
        build = provisional_daily_bars(
            [LiveName(1, "AAA")],
            [_quote("AAA", "100")],
            factors={1: 1.0},
            on=TODAY,
            schema=VBT_SCHEMA,
        )
        assert build.frame.schema == VBT_SCHEMA and build.frame.height == 1

    def test_bars_skip_and_count_a_name_with_no_quote_no_factor_or_no_price(self) -> None:
        names = [
            LiveName(1, "QUIET"),
            LiveName(2, "NOFACTOR"),
            LiveName(3, "ZERO"),
            LiveName(4, "OK"),
        ]
        build = provisional_daily_bars(
            names,
            [
                _quote("NOFACTOR", "10"),
                _quote("ZERO", "0"),
                _quote("OK", "10"),
                _quote("STRANGER", "1"),
            ],
            factors={1: 1.0, 3: 1.0, 4: 1.0},
            on=TODAY,
            schema=TWT_SCHEMA,
        )
        assert build.frame["symbol"].to_list() == ["OK"]
        assert build.skipped == {"no_quote": 1, "no_factor": 1, "bad_price": 1}

    def test_bars_take_the_last_price_for_a_missing_ohlc_side_and_keep_high_above_last(
        self,
    ) -> None:
        build = provisional_daily_bars(
            [LiveName(1, "AAA")],
            [_quote("AAA", "150", ohlc=False)],
            factors={1: 1.0},
            on=TODAY,
            schema=VBT_SCHEMA,
        )
        row = build.frame.row(0, named=True)
        assert row["open"] == row["high"] == row["low"] == row["close"] == 150.0

    def test_the_decision_is_the_swing_books_one_function(self) -> None:
        assert live_scan.decide_session is swing_scan_now.decide_session


# --- the decision, through the two Scan buttons ------------------------------------------------


class TestTheDecisionThroughTheTwtScan:
    async def test_before_the_open_it_detects_the_published_session_and_is_not_provisional(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "pre")
        await _published(session, PUBLISHED, version=42)
        row = await _queued(session, user_id)

        result = await twt_scan.run_twt_scan(
            session, row.id, now=_ist(TODAY, "08:30"), quote_source=_never
        )

        assert result["status"] == "DONE" and result["provisional"] is False
        assert result["session_date"] == PUBLISHED.isoformat()
        await session.refresh(row)
        assert row.provisional is False
        assert row.detail is not None and row.detail["reason"]

    async def test_in_session_without_a_kite_session_it_falls_back_to_published_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "noq")
        await _published(session, PUBLISHED, version=42)
        row = await _queued(session, user_id)

        result = await twt_scan.run_twt_scan(session, row.id, now=_ist(TODAY, "11:00"))

        assert result["status"] == "DONE" and result["provisional"] is False
        assert result["session_date"] == PUBLISHED.isoformat()
        await session.refresh(row)
        assert row.detail is not None
        assert row.detail["live"] == twt_scan.LIVE_SKIPPED_NO_QUOTES

    async def test_in_session_with_quotes_it_detects_today_provisionally(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "live")
        await _published(session, PUBLISHED, version=42)
        row = await _queued(session, user_id)
        quotes = Quotes([])

        result = await twt_scan.run_twt_scan(
            session, row.id, now=_ist(TODAY, "11:00"), quote_source=lambda: quotes
        )

        assert result["status"] == "DONE", result
        assert result["provisional"] is True
        assert result["session_date"] == TODAY.isoformat()
        await session.refresh(row)
        assert row.provisional is True
        assert row.detail is not None
        assert row.detail["bars_built"] == 0 and row.detail["plan_id"] is None

    async def test_after_the_publish_it_is_a_plain_rerun_of_the_published_day(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "post")
        await _published(session, TODAY, version=43)
        row = await _queued(session, user_id)

        result = await twt_scan.run_twt_scan(
            session, row.id, now=_ist(TODAY, "22:00"), quote_source=_never
        )

        assert result["provisional"] is False and result["session_date"] == TODAY.isoformat()


class TestTheDecisionThroughTheVbtRescan:
    async def _queued(self, session: AsyncSession, user_id: int) -> VbScanRun:
        row = VbScanRun(
            user_id=user_id, requested_at=dt.datetime.now(tz=dt.UTC), status="QUEUED", source="desk"
        )
        session.add(row)
        await session.flush()
        return row

    async def test_in_session_with_quotes_it_detects_today_provisionally(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        await _published(session, PUBLISHED, version=42)
        row = await self._queued(session, user_id)

        result = await vbt_rescan.run_vbt_rescan(
            session, row.id, now=_ist(TODAY, "13:00"), quote_source=lambda: Quotes([])
        )

        assert result["status"] == "DONE", result
        assert result["provisional"] is True and result["session_date"] == TODAY.isoformat()
        await session.refresh(row)
        assert row.provisional is True
        assert row.detail is not None and row.detail["plan_id"] is None

    async def test_without_a_kite_session_it_falls_back_and_says_so(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        await _published(session, PUBLISHED, version=42)
        row = await self._queued(session, user_id)

        result = await vbt_rescan.run_vbt_rescan(session, row.id, now=_ist(TODAY, "13:00"))

        assert result["provisional"] is False and result["session_date"] == PUBLISHED.isoformat()
        await session.refresh(row)
        assert row.detail is not None
        assert row.detail["live"] == vbt_rescan.LIVE_SKIPPED_NO_QUOTES


# --- the rows ----------------------------------------------------------------------------------


class TestTheProvisionalRows:
    async def test_a_provisional_twt_detect_writes_provisional_rows_and_never_ratchets(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = await twt_user(session, "rows")
        instrument_id = await make_instrument(session, "LIVEONE")
        ratchets: list[dt.date] = []

        async def spy(*_: object, **kwargs: object) -> object:
            ratchets.append(TODAY)
            raise AssertionError("the ratchet ran on a provisional bar")

        monkeypatch.setattr(twt, "run_twt_ratchet", spy)
        extra = provisional_daily_bars(
            [LiveName(instrument_id, "LIVEONE")],
            [_quote("LIVEONE", "100")],
            factors={instrument_id: 1.0},
            on=TODAY,
            schema=TWT_SCHEMA,
        ).frame

        signals = await twt.run_detect_twt(
            session, StepOutcome(), TODAY, user_id=user_id, extra_bars=extra, provisional=True
        )

        assert signals == 0 and ratchets == []
        breadth = (
            await session.execute(
                sa.select(TwBreadthDaily).where(
                    TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == TODAY
                )
            )
        ).scalar_one()
        assert breadth.provisional is True

    async def test_the_nightly_sweeps_provisional_twt_rows_it_does_not_reproduce(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "sweep")
        gone = await make_instrument(session, "GONE")
        kept = await make_instrument(session, "KEPT")
        for instrument_id in (gone, kept):
            session.add(
                TwSignalDaily(
                    user_id=user_id,
                    date=TODAY,
                    instrument_id=instrument_id,
                    provisional=True,
                    state=TwtSignalState.SIGNAL.value,
                    failed_filters=[],
                    entry_reference_close=Decimal("100"),
                    stop_preview=Decimal("80"),
                    sessions_out_before=5,
                    rank_key=1,
                    turnover_avg_20=1,
                )
            )
            session.add(
                TwStateDaily(
                    user_id=user_id,
                    date=TODAY,
                    instrument_id=instrument_id,
                    provisional=True,
                    open=Decimal(1),
                    high=Decimal(1),
                    low=Decimal(1),
                    close=Decimal(1),
                    close_raw=Decimal(1),
                    adj_factor=Decimal(1),
                    week_close_0=Decimal(1),
                    week_close_1=Decimal(1),
                    week_close_2=Decimal(1),
                    week_range_pct=Decimal(1),
                    month_low_3=Decimal(1),
                    month_low_ratio=Decimal(1),
                    vol_sma_50=1,
                    volume=1,
                    turnover_inr=1,
                    turnover_avg_20=1,
                    sma_dma=Decimal(1),
                    sessions_in_state=1,
                    bars_in_window=1,
                    locked_upper_circuit=False,
                )
            )
        await session.flush()
        real = pl.DataFrame(
            {
                "instrument_id": [kept],
                "signal_state": [TwtSignalState.SIGNAL.value],
                "failed_filters": [[]],
                "entry_reference_close": [100.0],
                "stop_preview": [80.0],
                "sessions_out_before": [5],
                "rank_key": [1],
                "turnover_avg_20": [1],
            }
        )

        await twt.upsert_signals(
            session, real, user_id=user_id, trade_date=TODAY, pipeline_run_id=None
        )
        await twt.upsert_states(
            session, pl.DataFrame(), user_id=user_id, trade_date=TODAY, pipeline_run_id=None
        )

        rows = (
            await session.execute(
                sa.select(TwSignalDaily.instrument_id, TwSignalDaily.provisional).where(
                    TwSignalDaily.user_id == user_id, TwSignalDaily.date == TODAY
                )
            )
        ).all()
        assert [(r[0], r[1]) for r in rows] == [(kept, False)], (
            "the straggler went, the real row stays"
        )
        states = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(TwStateDaily)
                .where(TwStateDaily.date == TODAY)
            )
        ).scalar_one()
        assert states == 0

    async def test_the_sweep_keeps_a_provisional_signal_an_order_references(
        self, session: AsyncSession
    ) -> None:
        """`fk_tw_order_signal`: deleting the row would fail the nightly; keeping it, still
        provisional, is the honest record of what the buy was taken on."""
        user_id = await twt_user(session, "fk")
        instrument_id = await twt_signal(session, user_id, "BOUGHT", close="100.00", on=TODAY)
        signal = (await session.execute(sa.select(TwSignalDaily))).scalar_one()
        signal.provisional = True
        session.add(
            TwOrder(
                user_id=user_id,
                instrument_id=instrument_id,
                signal_date=TODAY,
                side="BUY",
                quantity=10,
                stop_price=Decimal("80"),
                state="FILLED",
                client_id="live:BOUGHT",
            )
        )
        await session.flush()

        await twt.upsert_signals(
            session, pl.DataFrame(), user_id=user_id, trade_date=TODAY, pipeline_run_id=None
        )

        left = (await session.execute(sa.select(TwSignalDaily))).scalar_one()
        assert left.instrument_id == instrument_id and left.provisional is True

    async def test_the_nightly_sweeps_provisional_vbt_rows_it_does_not_reproduce(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        gone = await make_instrument(session, "VGONE")
        session.add(
            VbSignalDaily(
                user_id=user_id,
                date=TODAY,
                instrument_id=gone,
                provisional=True,
                state=VbtSignalState.SIGNAL.value,
                failed_filters=[],
                close=Decimal("100"),
                close_raw=Decimal("100"),
                adj_factor=Decimal(1),
                limit_price=Decimal("100"),
                stop_price=Decimal("88"),
                turnover_avg_20=1,
                rank_key=1,
                locked_upper_circuit=False,
            )
        )
        await session.flush()

        await vbt.upsert_signals(
            session, pl.DataFrame(), user_id=user_id, trade_date=TODAY, pipeline_run_id=None
        )

        left = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(VbSignalDaily)
                .where(VbSignalDaily.date == TODAY)
            )
        ).scalar_one()
        assert left == 0

    async def test_a_provisional_vbt_detect_marks_its_breadth_row(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        instrument_id = await make_instrument(session, "VLIVE")
        extra = provisional_daily_bars(
            [LiveName(instrument_id, "VLIVE")],
            [_quote("VLIVE", "100")],
            factors={instrument_id: 1.0},
            on=TODAY,
            schema=VBT_SCHEMA,
        ).frame

        await vbt.run_detect_vbt(
            session, StepOutcome(), TODAY, user_id=user_id, extra_bars=extra, provisional=True
        )

        breadth = (
            await session.execute(
                sa.select(VbBreadthDaily).where(
                    VbBreadthDaily.user_id == user_id, VbBreadthDaily.date == TODAY
                )
            )
        ).scalar_one()
        assert breadth.provisional is True


# --- the plan ----------------------------------------------------------------------------------


class TestTheLivePlan:
    async def test_a_twt_signal_becomes_a_live_plan_with_a_thirty_minute_expiry(
        self, session: AsyncSession
    ) -> None:
        user_id = await twt_user(session, "plan")
        await twt_breadth(session, user_id, on=TODAY)
        await twt_signal(session, user_id, "TIGHT", close="100.00", on=TODAY)
        now = _ist(TODAY, "11:40")

        built = await build_live_plan_twt(
            session, user_id=user_id, trade_date=TODAY, execution_enabled=False, now=now
        )

        assert built.plan_id is not None and built.entries == 1 and built.kept is False
        plan = (await session.execute(sa.select(TwPlan))).scalar_one()
        assert plan.source == SOURCE_LIVE and plan.session_date == TODAY
        assert plan.expires_at == now + dt.timedelta(minutes=30)
        line = (await session.execute(sa.select(TwPlanLine))).scalar_one()
        assert line.kind == "BUY_AT_OPEN" and line.state == "PROPOSED"
        assert line.note is not None and line.note.startswith("live scan 11:40: buy now, at market")

    async def test_no_twt_signal_means_no_live_plan(self, session: AsyncSession) -> None:
        user_id = await twt_user(session, "quiet")
        await twt_breadth(session, user_id, on=TODAY)

        built = await build_live_plan_twt(
            session,
            user_id=user_id,
            trade_date=TODAY,
            execution_enabled=False,
            now=_ist(TODAY, "11:40"),
        )
        assert (built.plan_id, built.entries) == (None, 0)
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TwPlan))
        ).scalar_one() == 0

    async def test_a_vbt_signal_becomes_a_live_plan_of_buy_at_market_lines(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        await vbt_calendar(session, user_id)
        await vbt_signal(session, user_id, "VBTCO", limit="100.00", on=VBT_AS_OF)
        now = _ist(VBT_AS_OF, "13:05")

        built = await build_live_plan_vbt(
            session, user_id=user_id, trade_date=VBT_AS_OF, execution_enabled=False, now=now
        )

        assert built.plan_id is not None and built.entries == 1
        plan = (await session.execute(sa.select(VbPlan))).scalar_one()
        assert plan.source == SOURCE_LIVE and plan.expires_at == now + dt.timedelta(minutes=30)
        line = (await session.execute(sa.select(VbPlanLine))).scalar_one()
        assert line.kind == VbtLineKind.BUY_AT_MARKET.value and line.state == "PROPOSED"
        assert line.limit_price == Decimal("100.00"), "the preview the desk values against"
        assert line.client_id is not None and line.client_id.endswith(":VBTCO:BUY_AT_MARKET")
        assert line.note is not None and "buy now, at market" in line.note

    async def test_no_vbt_signal_means_no_live_plan(self, session: AsyncSession) -> None:
        user_id = await vbt_user(session)
        await vbt_calendar(session, user_id)
        built = await build_live_plan_vbt(
            session,
            user_id=user_id,
            trade_date=VBT_AS_OF,
            execution_enabled=False,
            now=_ist(VBT_AS_OF, "13:05"),
        )
        assert (built.plan_id, built.entries) == (None, 0)

    async def test_a_rescan_keeps_a_live_plan_whose_line_is_at_the_broker(
        self, session: AsyncSession
    ) -> None:
        """`store_plan` replaces a day's plan of a source wholesale; a line that is SENT must not
        be replaced by a fresh PROPOSED one for the same name under a new client_id."""
        user_id = await twt_user(session, "inflight")
        await twt_breadth(session, user_id, on=TODAY)
        await twt_signal(session, user_id, "TIGHT", close="100.00", on=TODAY)
        first = await build_live_plan_twt(
            session,
            user_id=user_id,
            trade_date=TODAY,
            execution_enabled=False,
            now=_ist(TODAY, "11:40"),
        )
        line = (await session.execute(sa.select(TwPlanLine))).scalar_one()
        line.state = "SENT"
        await session.flush()

        second = await build_live_plan_twt(
            session,
            user_id=user_id,
            trade_date=TODAY,
            execution_enabled=False,
            now=_ist(TODAY, "11:45"),
        )

        assert second.kept is True and second.plan_id == first.plan_id and second.entries == 0
        plans = (await session.execute(sa.select(sa.func.count()).select_from(TwPlan))).scalar_one()
        assert plans == 1
        assert (await session.execute(sa.select(TwPlanLine.state))).scalar_one() == "SENT"

    async def test_a_name_with_an_order_today_is_not_offered_again(
        self, session: AsyncSession
    ) -> None:
        """A market buy sent thirty seconds ago is not yet a position; TWT's BookState knows
        positions only, so the live plan removes ordered names itself."""
        user_id = await twt_user(session, "ordered")
        await twt_breadth(session, user_id, on=TODAY)
        instrument_id = await twt_signal(session, user_id, "TIGHT", close="100.00", on=TODAY)
        session.add(
            TwOrder(
                user_id=user_id,
                instrument_id=instrument_id,
                signal_date=TODAY,
                side="BUY",
                quantity=10,
                stop_price=Decimal("80"),
                state="SENT",
                client_id="earlier:TIGHT",
            )
        )
        await session.flush()

        built = await build_live_plan_twt(
            session,
            user_id=user_id,
            trade_date=TODAY,
            execution_enabled=False,
            now=_ist(TODAY, "11:45"),
        )

        assert (built.plan_id, built.entries, built.already_ordered) == (None, 0, 1)


class TestNothingHereReachesAnOrder:
    def test_the_live_scan_module_names_no_gateway_or_broker_verb(self) -> None:
        source = inspect.getsource(live_scan)
        for forbidden in (
            "place_order",
            "place_gtt",
            "OrderGateway",
            "baskfy_execution",
            "KiteConnect",
        ):
            assert forbidden not in source, forbidden
