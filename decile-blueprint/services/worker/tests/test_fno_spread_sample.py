"""FO3: the 15:00 spread sample, the half-spread statistic FO9 reads, and the results calendar.

``docs/fno/06`` FO3 and ``01`` §2 asserted here:

* **one** ``quote()`` over ATM ± 3 listed strikes, CE and PE, of the **near monthly** of the top
  30 stock underlyings by futures turnover plus NIFTY and BANKNIFTY — ≤ 500 keys;
* the names come from ``fo_contract_daily`` (median per-session futures turnover), the contracts
  from ``op_contract``, "monthly" is the last listed expiry of its month (never a weekday rule);
* rows are rounded at write time and **append-only**; a one-sided book keeps its null side;
* with no Kite session **no Kite reader is built and no call is made**; the flag defaults false
  and refuses before anything; Beat runs it at 15:00 on weekdays;
* the per-underlying median half-spread ÷ mid and its session count (FO9's ≥ 20 rule);
* the results calendar (QUESTIONS Q8) is forward only and fails soft per symbol.

No network. Database tests run in rolled-back transactions against ``BASKFY_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import FoContractDaily, FoSpreadSample, OpContract
from baskfy_providers.errors import TransientProviderError
from baskfy_providers.records import DepthLevelRecord, EarningsDateRecord, OptionQuoteRecord
from baskfy_worker.fno.spreads import (
    INDEX_UNDERLYINGS,
    STRIKES_EACH_SIDE,
    TOP_STOCKS,
    SampleContract,
    SpreadObservation,
    SpreadReport,
    atm_window,
    half_spread_ratio,
    half_spread_stats,
    in_session,
    load_spread_stats,
    monthly_expiries,
    near_monthly,
    pick_sample,
    quote_calls,
    rank_by_turnover,
    read_results,
    reference_level,
    run_spread_sample,
    sample_rows,
    upcoming_results,
)
from baskfy_worker.settings import WorkerSettings

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: Inside the migration's partitions (Jan 2022 - Dec 2027), clear of any real session.
S1, S2, S3 = dt.date(2027, 11, 1), dt.date(2027, 11, 2), dt.date(2027, 11, 3)
TODAY: Final = dt.date(2027, 11, 4)
AT_1500: Final = dt.datetime(2027, 11, 4, 15, 0, tzinfo=IST)
NOV, DEC = dt.date(2027, 11, 25), dt.date(2027, 12, 30)
NIFTY_WEEKLY: Final = dt.date(2027, 11, 9)


def _d(value: str) -> Decimal:
    return Decimal(value)


def contract(symbol: str, expiry: dt.date, strike: str, option_type: str = "CE") -> SampleContract:
    return SampleContract(
        symbol=symbol,
        tradingsymbol=f"{symbol}{expiry:%y%b%d}{strike}{option_type}".upper(),
        expiry=expiry,
        strike=_d(strike),
        option_type=option_type,
    )


def quote(key: str, bid: str | None, ask: str | None, oi: int | None = 1200) -> OptionQuoteRecord:
    exchange, _, symbol = key.partition(":")
    return OptionQuoteRecord(
        symbol=symbol,
        exchange=exchange,
        oi=oi,
        bids=(DepthLevelRecord(price=_d(bid), quantity=50),) if bid else (),
        asks=(DepthLevelRecord(price=_d(ask), quantity=50),) if ask else (),
    )


# --- pure: names, expiry, strikes ------------------------------------------------------------


class TestTheNames:
    def test_ranked_by_median_turnover_not_one_big_day(self) -> None:
        ranked = rank_by_turnover(
            {
                "SPIKE": [_d("1"), _d("1"), _d("900")],
                "STEADY": [_d("50"), _d("50"), _d("50")],
                "SMALL": [_d("10")],
            },
            top=2,
        )
        assert ranked == ["STEADY", "SMALL"]

    def test_ties_break_by_symbol_and_the_cut_is_top(self) -> None:
        turnover = {f"S{i:02d}": [_d("5")] for i in range(40)}
        ranked = rank_by_turnover(turnover)
        assert len(ranked) == TOP_STOCKS
        assert ranked[:2] == ["S00", "S01"]

    def test_the_key_budget_fits_one_quote_call(self) -> None:
        """``06`` FO3: 30 stocks + 2 indices, 7 strikes, CE and PE — ≤ 500 keys, one call."""
        keys = (TOP_STOCKS + len(INDEX_UNDERLYINGS)) * (2 * STRIKES_EACH_SIDE + 1) * 2
        assert keys == 448
        assert quote_calls(keys) == 1

    @pytest.mark.parametrize(("keys", "calls"), [(0, 0), (1, 1), (500, 1), (501, 2), (1001, 3)])
    def test_above_500_keys_is_split_and_counted(self, keys: int, calls: int) -> None:
        assert quote_calls(keys) == calls


class TestTheExpiry:
    def test_monthly_is_the_last_listed_expiry_of_its_month(self) -> None:
        listed = [NIFTY_WEEKLY, dt.date(2027, 11, 16), NOV, dt.date(2027, 12, 7), DEC]
        assert monthly_expiries(listed) == [NOV, DEC]

    def test_near_monthly_skips_weeklies(self) -> None:
        assert near_monthly([NIFTY_WEEKLY, NOV, DEC], TODAY) == NOV

    def test_on_expiry_day_the_next_monthly_is_sampled(self) -> None:
        assert near_monthly([NOV, DEC], NOV) == DEC

    def test_nothing_listed_ahead(self) -> None:
        assert near_monthly([dt.date(2027, 10, 28)], TODAY) is None

    def test_reference_is_the_same_expiry_settle_else_the_nearest(self) -> None:
        futures = {NOV: _d("810.10"), DEC: _d("815.00")}
        assert reference_level(futures, NOV) == _d("810.10")
        assert reference_level({DEC: _d("815.00")}, NOV) == _d("815.00")
        assert reference_level({}, NOV) is None


class TestTheStrikes:
    LADDER: Final = [_d(str(k)) for k in range(700, 921, 20)]

    def test_atm_and_three_listed_strikes_each_side(self) -> None:
        assert atm_window(self.LADDER, _d("808")) == tuple(
            _d(str(k)) for k in (740, 760, 780, 800, 820, 840, 860)
        )

    def test_a_tie_centres_on_the_lower_strike(self) -> None:
        assert atm_window(self.LADDER, _d("810"))[3] == _d("800")

    def test_an_edge_is_clipped_not_invented(self) -> None:
        assert atm_window(self.LADDER, _d("690")) == tuple(_d(str(k)) for k in (700, 720, 740, 760))

    def test_an_irregular_ladder_is_read_by_listing_order(self) -> None:
        ladder = [_d(k) for k in ("90", "95", "100", "102.5", "105", "110", "120", "140")]
        assert atm_window(ladder, _d("103")) == tuple(
            _d(k) for k in ("90", "95", "100", "102.5", "105", "110", "120")
        ), "centre 102.5, three listed strikes each side whatever their spacing"

    def test_pick_is_the_near_monthly_both_types(self) -> None:
        contracts = [
            contract("SBIN", expiry, str(k), t)
            for expiry in (NOV, DEC)
            for k in range(700, 921, 20)
            for t in ("CE", "PE")
        ]
        picked = pick_sample(contracts, _d("808"), TODAY)
        assert len(picked) == 14
        assert {c.expiry for c in picked} == {NOV}
        assert {c.option_type for c in picked} == {"CE", "PE"}


# --- pure: the rows ----------------------------------------------------------------------------


class TestTheRows:
    def test_a_two_sided_book_rounds_its_mid_half_up(self) -> None:
        c = contract("SBIN", NOV, "800")
        [row] = sample_rows(
            [c], {c.key: quote(c.key, "10.05", "10.10")}, trade_date=TODAY, taken_at=AT_1500
        )
        assert (row["bid"], row["ask"]) == (_d("10.05"), _d("10.10"))
        assert row["mid"] == _d("10.08"), "10.075 half-up to 2 dp (house rule 8)"
        assert row["oi"] == 1200
        assert row["taken_at"] == AT_1500 and row["trade_date"] == TODAY

    def test_a_one_sided_or_empty_book_is_stored_with_nulls(self) -> None:
        bid_only, empty = contract("SBIN", NOV, "800"), contract("SBIN", NOV, "820")
        rows = sample_rows(
            [bid_only, empty],
            {
                bid_only.key: quote(bid_only.key, "3.00", None),
                empty.key: quote(empty.key, None, None),
            },
            trade_date=TODAY,
            taken_at=AT_1500,
        )
        assert [(r["bid"], r["ask"], r["mid"]) for r in rows] == [
            (_d("3.00"), None, None),
            (None, None, None),
        ]

    def test_an_unanswered_key_writes_nothing(self) -> None:
        assert (
            sample_rows([contract("SBIN", NOV, "800")], {}, trade_date=TODAY, taken_at=AT_1500)
            == []
        )

    def test_a_crossed_book_keeps_both_prices_and_no_mid(self) -> None:
        c = contract("SBIN", NOV, "800")
        [row] = sample_rows(
            [c], {c.key: quote(c.key, "5.10", "5.00")}, trade_date=TODAY, taken_at=AT_1500
        )
        assert (row["bid"], row["ask"], row["mid"]) == (_d("5.10"), _d("5.00"), None)


# --- pure: the statistic FO9 reads ---------------------------------------------------------------


def obs(  # noqa: PLR0913 - one argument per column the fixture varies
    day: dt.date,
    bid: str | None,
    ask: str | None,
    *,
    symbol: str = "SBIN",
    strike: str = "800",
    taken_at: dt.datetime | None = None,
) -> SpreadObservation:
    return SpreadObservation(
        trade_date=day,
        symbol=symbol,
        expiry=NOV,
        strike=_d(strike),
        option_type="CE",
        bid=_d(bid) if bid else None,
        ask=_d(ask) if ask else None,
        taken_at=taken_at or dt.datetime.combine(day, dt.time(15), tzinfo=IST),
    )


class TestTheStatistic:
    def test_half_spread_over_mid(self) -> None:
        assert half_spread_ratio(_d("9"), _d("11")) == _d("0.1")
        assert half_spread_ratio(None, _d("11")) is None
        assert half_spread_ratio(_d("11"), _d("9")) is None, "crossed"

    def test_median_over_two_sided_rows_and_the_session_count(self) -> None:
        stats = half_spread_stats(
            [
                obs(S1, "9", "11"),  # 0.1
                obs(S1, "19", "21", strike="820"),  # 0.05
                obs(S2, "99", "101"),  # 0.01
                obs(S3, "5", None),  # one-sided: no spread, not a session of measurement
            ]
        )
        stat = stats["SBIN"]
        assert stat.median_half_spread_pct_of_mid == _d("0.05")
        assert (stat.sessions, stat.observations) == (2, 3)

    def test_a_rerun_in_a_session_counts_once_at_its_latest(self) -> None:
        early = dt.datetime(2027, 11, 1, 15, 0, tzinfo=IST)
        late = dt.datetime(2027, 11, 1, 15, 5, tzinfo=IST)
        stats = half_spread_stats(
            [obs(S1, "9", "11", taken_at=early), obs(S1, "99", "101", taken_at=late)]
        )
        assert stats["SBIN"].observations == 1
        assert stats["SBIN"].median_half_spread_pct_of_mid == _d("0.01")

    def test_only_the_last_n_sessions_are_read(self) -> None:
        stats = half_spread_stats([obs(S1, "9", "11"), obs(S2, "99", "101")], sessions=1)
        assert stats["SBIN"].sessions == 1
        assert stats["SBIN"].median_half_spread_pct_of_mid == _d("0.01")

    def test_the_twenty_session_rule(self) -> None:
        days = [dt.date(2027, 10, 1) + dt.timedelta(days=i) for i in range(20)]
        nineteen = half_spread_stats([obs(d, "9", "11") for d in days[:19]])["SBIN"]
        twenty = half_spread_stats([obs(d, "9", "11") for d in days])["SBIN"]
        assert not nineteen.measured()
        assert twenty.measured()


# --- pure: the results calendar -------------------------------------------------------------------


def meeting(symbol: str, day: dt.date) -> EarningsDateRecord:
    return EarningsDateRecord(
        symbol=symbol, event_date=day, purpose="Financial Results", url="https://example.test/"
    )


class FakeResults:
    def __init__(self, fail: frozenset[str] = frozenset()) -> None:
        self.fail = fail
        self.asked: list[str] = []

    def results_calendar(self, symbols: Sequence[str], *, on: dt.date) -> list[EarningsDateRecord]:
        del on
        self.asked.extend(symbols)
        if symbols[0] in self.fail:
            raise TransientProviderError("429", provider="nse")
        return [
            meeting(symbols[0], dt.date(2027, 10, 20)),
            meeting(symbols[0], dt.date(2027, 11, 12)),
        ]


class TestTheResultsCalendar:
    def test_forward_only(self) -> None:
        out = upcoming_results(
            [meeting("SBIN", dt.date(2027, 10, 20)), meeting("SBIN", TODAY), meeting("SBIN", DEC)],
            TODAY,
        )
        assert [m.event_date for m in out["SBIN"]] == [TODAY, DEC]

    def test_fails_soft_per_symbol(self) -> None:
        report = SpreadReport(trade_date=TODAY, taken_at=AT_1500)
        read_results(FakeResults(frozenset({"BAD"})), ["SBIN", "BAD"], TODAY, report)
        assert report.results == {"SBIN": ["2027-11-12"]}
        assert "BAD" in report.results_errors


def test_a_quote_is_a_live_book_only_in_session() -> None:
    assert in_session(AT_1500)
    assert not in_session(dt.datetime(2027, 11, 4, 15, 31, tzinfo=IST))
    assert not in_session(dt.datetime(2027, 11, 4, 9, 14, tzinfo=IST))


# --- the gate and the clock ---------------------------------------------------------------------


def test_the_task_refuses_before_anything_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415 - heavy import, one test

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a provider or a database session was used with the flag off")

    for name in ("build_options_kite", "build_nse_provider", "run_in_session"):
        monkeypatch.setattr(celery_tasks, name, explode)
    monkeypatch.setattr(celery_tasks, "get_worker_settings", lambda: WorkerSettings(_env_file=None))
    out = celery_tasks.fno_spread_sample_task(at=AT_1500.isoformat())
    assert out["skipped"] == "BASKFY_FNO_SCAN_ENABLED is false"


def test_the_task_refuses_outside_the_session_before_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415 - heavy import, one test

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a provider or a database session was used outside the session")

    for name in ("build_options_kite", "build_nse_provider", "run_in_session"):
        monkeypatch.setattr(celery_tasks, name, explode)
    monkeypatch.setattr(
        celery_tasks,
        "get_worker_settings",
        lambda: WorkerSettings(_env_file=None, fno_scan_enabled=True),
    )
    out = celery_tasks.fno_spread_sample_task(at="2027-11-04T16:00:00+05:30")
    assert out["skipped"] == "outside 09:15-15:30 IST"


def test_beat_runs_it_at_1500_on_weekdays() -> None:
    from baskfy_worker.celery_app import BEAT_SCHEDULE, build_celery  # noqa: PLC0415

    entry = BEAT_SCHEDULE["fno-spread-sample"]
    assert entry["task"] == "baskfy.fno.spread_sample"
    schedule = entry["schedule"]
    assert getattr(schedule, "minute") == {0}  # noqa: B009 - crontab's typed attribute
    assert getattr(schedule, "hour") == {15}  # noqa: B009
    assert getattr(schedule, "day_of_week") == {1, 2, 3, 4, 5}  # noqa: B009
    assert "baskfy.fno.spread_sample" in build_celery().tasks


# --- database -----------------------------------------------------------------------------------


def _database_url() -> str | None:
    return os.environ.get(ENV_VAR)


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set")
)


@pytest.fixture(scope="module")
def fo_url() -> str:
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return url


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


#: Test-only names, so no real row can meet them. Turnover (₹) per session for S1, S2, S3.
STOCKS: Final[dict[str, tuple[str, str, str]]] = {
    "ZZSTEADY": ("500", "500", "500"),
    "ZZSPIKE": ("1", "1", "90000"),
    "ZZMID": ("200", "200", "200"),
}
SETTLE: Final[dict[str, str]] = {
    "ZZSTEADY": "808.00",
    "ZZSPIKE": "150.00",
    "ZZMID": "95.00",
    "NIFTY": "24310.00",
    "BANKNIFTY": "55020.00",
}
_TOKEN_BASE: Final = 9_870_000_000


def _fcd(
    day: dt.date, symbol: str, expiry: dt.date, settle: str, turnover: str
) -> dict[str, object]:
    return {
        "trade_date": day,
        "instrument": "FUTIDX" if symbol in INDEX_UNDERLYINGS else "FUTSTK",
        "symbol": symbol,
        "expiry": expiry,
        "strike": Decimal(0),
        "option_type": "XX",
        "close": _d(settle),
        "settle": _d(settle),
        "turnover": _d(turnover),
        "source_key": "test/fo3",
    }


async def _seed(session: AsyncSession) -> None:
    daily: list[dict[str, object]] = []
    for i, day in enumerate((S1, S2, S3)):
        for symbol, turnovers in STOCKS.items():
            daily.append(_fcd(day, symbol, NOV, SETTLE[symbol], turnovers[i]))
        for index in INDEX_UNDERLYINGS:
            daily.append(_fcd(day, index, NOV, SETTLE[index], "99999999"))
    await session.execute(sa.insert(FoContractDaily).values(daily))
    ladders = {
        "ZZSTEADY": [str(k) for k in range(700, 921, 20)],
        "ZZSPIKE": [str(k) for k in range(120, 181, 5)],
        "ZZMID": [str(k) for k in range(80, 111, 2)],
        "NIFTY": [str(k) for k in range(24000, 24601, 50)],
        "BANKNIFTY": [str(k) for k in range(54500, 55501, 100)],
    }
    contracts: list[dict[str, object]] = []
    token = _TOKEN_BASE
    for symbol, ladder in ladders.items():
        expiries = [NOV, DEC] + ([NIFTY_WEEKLY] if symbol == "NIFTY" else [])
        for expiry in expiries:
            for strike in ladder:
                for option_type in ("CE", "PE"):
                    token += 1
                    c = contract(symbol, expiry, strike, option_type)
                    contracts.append(
                        {
                            "instrument_token": token,
                            "tradingsymbol": c.tradingsymbol,
                            "underlying": symbol,
                            "expiry": expiry,
                            "strike": _d(strike),
                            "option_type": option_type,
                            "lot_size": 75,
                            "tick_size": _d("0.05"),
                            "first_seen": S1,
                            "last_seen": S3,
                        }
                    )
    await session.execute(sa.insert(OpContract).values(contracts))


class RecordingQuotes:
    """Answers every key two-sided (bid 10.05, ask 10.10) except those named one-sided."""

    def __init__(self, one_sided: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.one_sided = one_sided

    def option_quotes(self, keys: Sequence[str]) -> list[OptionQuoteRecord]:
        self.calls.append(list(keys))
        return [quote(k, "10.05", None if k in self.one_sided else "10.10") for k in keys]


@requires_db
class TestTheRun:
    async def test_one_call_over_the_near_monthly_atm_window_and_append_only(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _seed(session)
            reader = RecordingQuotes()
            results = FakeResults()
            report = await run_spread_sample(
                session,
                AT_1500,
                kite_ok=lambda: True,
                quotes=lambda: reader,
                results=results,
                top=2,
            )
            assert report.skipped is None, report.as_dict()
            assert report.reference_session == S3
            assert report.underlyings == ["ZZSTEADY", "ZZMID", "NIFTY", "BANKNIFTY"], (
                "median turnover: one 90,000 day does not lift ZZSPIKE into the top 2"
            )
            assert len(reader.calls) == 1 and report.calls == 1
            keys = reader.calls[0]
            assert len(keys) == 4 * 14 <= 500
            assert all(k.startswith("NFO:") for k in keys)
            stored = (
                (
                    await session.execute(
                        sa.select(FoSpreadSample).where(FoSpreadSample.taken_at == AT_1500)
                    )
                )
                .scalars()
                .all()
            )
            assert len(stored) == report.inserted == 56
            assert {r.expiry for r in stored} == {NOV}, "the near monthly, never a weekly"
            steady = sorted({r.strike for r in stored if r.symbol == "ZZSTEADY"})
            assert steady == [_d(str(k)) for k in (740, 760, 780, 800, 820, 840, 860)]
            nifty = sorted({r.strike for r in stored if r.symbol == "NIFTY"})
            assert nifty[3] == _d("24300")
            assert all(r.mid == _d("10.08") and r.trade_date == TODAY for r in stored)
            assert results.asked == ["ZZSTEADY", "ZZMID"], "the stock names only"
            assert report.results["ZZSTEADY"] == ["2027-11-12"]

            again = await run_spread_sample(
                session,
                AT_1500,
                kite_ok=lambda: True,
                quotes=RecordingQuotes,
                results=None,
                top=2,
            )
            assert again.rows == 56 and again.inserted == 0, "append-only (house rule 7)"

    async def test_no_kite_session_builds_no_reader_and_makes_no_call(self, fo_url: str) -> None:
        def no_reader() -> RecordingQuotes:
            raise AssertionError("a Kite reader was built with no Kite session")

        async with _rolled_back(fo_url) as session:
            await _seed(session)
            results = FakeResults()
            report = await run_spread_sample(
                session,
                AT_1500,
                kite_ok=lambda: False,
                quotes=no_reader,
                results=results,
                top=2,
            )
            assert report.skipped == "no usable Kite session"
            assert report.calls == 0 and report.inserted == 0
            assert results.asked == ["ZZSTEADY", "ZZMID"], "NSE's calendar needs no Kite"

    async def test_one_sided_rows_are_kept_and_the_stat_reads_them_back(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _seed(session)
            steady = [
                contract("ZZSTEADY", NOV, str(k), t).key
                for k in (740, 760, 780, 800, 820, 840, 860)
                for t in ("CE", "PE")
            ]
            report = await run_spread_sample(
                session,
                AT_1500,
                kite_ok=lambda: True,
                quotes=lambda: RecordingQuotes(frozenset(steady[:2])),
                results=None,
                top=1,
            )
            assert report.one_sided == 2
            stats = await load_spread_stats(session, TODAY, symbols=["ZZSTEADY", "NIFTY"])
            assert stats["ZZSTEADY"].observations == 12
            assert stats["ZZSTEADY"].sessions == 1
            assert stats["ZZSTEADY"].median_half_spread_pct_of_mid == _d("0.05") / _d("20.15")
            assert not stats["ZZSTEADY"].measured()
            assert await load_spread_stats(session, S3, symbols=["ZZSTEADY"]) == {}, (
                "point in time: nothing sampled after as_of is read"
            )

    async def test_before_any_ingest_it_says_so_and_calls_nothing(self, fo_url: str) -> None:
        def no_reader() -> RecordingQuotes:
            raise AssertionError("a Kite reader was built with nothing to sample")

        async with _rolled_back(fo_url) as session:
            far_past = dt.datetime(2021, 1, 4, 15, 0, tzinfo=IST)
            report = await run_spread_sample(
                session, far_past, kite_ok=lambda: True, quotes=no_reader, results=None
            )
            assert report.skipped is not None and "no session before today" in report.skipped
