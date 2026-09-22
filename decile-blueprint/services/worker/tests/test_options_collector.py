"""OP3 — the chain collector, the index minute bars, the gate, the probe.

``docs/options/06`` OP3 AC asserted here:

* the fake client's **bars and quotes round-trip idempotently** — a minute written twice is one
  set of rows, an index day written twice is one set of bars (house rule 7);
* a quote batch **never exceeds 500 symbols** — the collector's pick is one call by construction
  and refuses a configuration that would need two;
* the collector makes **no call on a non-trading day** (nor with the flag off, outside
  09:15-15:30, or with no Kite session) — asserted on the real Celery task body;
* this month's and next month's ``op_chain_snapshot`` partitions are created before a write
  (OP2.5), proved on a month the migration did not create;
* the Tier-1 backfill resumes from the newest stored bar;
* the probe is read-only: its source names no order verb, and it runs end to end against a fake
  client that has none.

Database tests run in rolled-back transactions against ``BASKFY_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import subprocess
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import Instrument, OpChainSnapshot, OpContract, OpIndexMinute
from baskfy_core.options.calendar import Contract
from baskfy_core.options.config import ChainConfig, OptionsConfig, OptionType
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.kite import QUOTE_BATCH_SIZE
from baskfy_providers.records import (
    BasketMarginRecord,
    DepthLevelRecord,
    MarginLegRecord,
    MinuteBarRecord,
    OptionContractRecord,
    OptionQuoteRecord,
)
from baskfy_worker.options import collector, index_bars, probe
from baskfy_worker.options.collector import (
    SPOT_KEY,
    TooManySymbols,
    collect_gate,
    collect_minute,
    in_session,
    pick_contracts,
    snapshot_rows,
)
from baskfy_worker.options.partitions import partition_name
from baskfy_worker.options.reads import OptionsKite

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
API_DIR: Final = REPO_ROOT / "services" / "api"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: A literal lot size is allowed in a fixture and nowhere else (``04`` §1.4).
LOT: Final = 65
BASE: Final = 8_000_000_000
TICK: Final = Decimal("0.05")

#: A Tuesday session with the two nearest expiries after it, plus a far one.
DAY: Final = dt.date(2026, 9, 22)
NOW: Final = dt.datetime(2026, 9, 22, 10, 0, 20, tzinfo=IST)
EXPIRIES: Final = (dt.date(2026, 9, 22), dt.date(2026, 9, 29), dt.date(2026, 10, 6))
SPOT: Final = Decimal("25012.35")


def _strikes() -> list[Decimal]:
    """50-point strikes within 1,000 of 25,000, 100-point beyond — a real chain's shape."""
    near = [Decimal(k) for k in range(24000, 26001, 50)]
    far = [Decimal(k) for k in range(22000, 24000, 100)] + [
        Decimal(k) for k in range(26100, 28001, 100)
    ]
    return sorted(near + far)


def _contracts(expiries: Sequence[dt.date] = EXPIRIES) -> list[Contract]:
    out: list[Contract] = []
    n = 0
    for expiry in expiries:
        for strike in _strikes():
            for kind in (OptionType.CE, OptionType.PE):
                n += 1
                out.append(
                    Contract(
                        instrument_token=BASE + n,
                        tradingsymbol=f"FX{BASE + n}{kind.value}",
                        underlying="NIFTY",
                        expiry=expiry,
                        strike=strike,
                        option_type=kind,
                        lot_size=LOT,
                        tick_size=TICK,
                    )
                )
    return out


def _price(c: Contract, spot: Decimal) -> Decimal:
    """A plausible mid: intrinsic plus time value decaying away from ATM."""
    intrinsic = (
        max(spot - c.strike, Decimal(0))
        if c.option_type is OptionType.CE
        else max(c.strike - spot, Decimal(0))
    )
    days = Decimal((c.expiry - DAY).days + 1)
    time_value = Decimal(120) * days.sqrt() / (1 + abs(c.strike - spot) / Decimal(150))
    return (intrinsic + time_value).quantize(TICK)


def _quote(c: Contract, spot: Decimal = SPOT, *, oi: int = LOT * 20_000) -> OptionQuoteRecord:
    mid = _price(c, spot)
    return OptionQuoteRecord(
        symbol=c.tradingsymbol,
        exchange="NFO",
        instrument_token=c.instrument_token,
        last_price=mid,
        volume=1000,
        oi=oi,
        bids=tuple(
            DepthLevelRecord(price=mid - TICK * (i + 1), quantity=LOT * (i + 1), orders=i + 1)
            for i in range(5)
        ),
        asks=tuple(
            DepthLevelRecord(price=mid + TICK * (i + 1), quantity=LOT * (i + 1), orders=i + 1)
            for i in range(5)
        ),
        as_of=NOW,
        last_trade_time=NOW,
    )


class FakeQuotes:
    """A ``QuoteReader`` over a fixed chain; counts calls and the size of every batch."""

    def __init__(self, contracts: Sequence[Contract], spot: Decimal | None = SPOT) -> None:
        self._by_key = {f"NFO:{c.tradingsymbol}": _quote(c) for c in contracts}
        self._spot = spot
        self.batches: list[int] = []

    def option_quotes(self, keys: Sequence[str]) -> list[OptionQuoteRecord]:
        assert len(keys) <= QUOTE_BATCH_SIZE, "a batch above 500 would be two Kite calls"
        self.batches.append(len(keys))
        out = [self._by_key[k] for k in keys if k in self._by_key]
        if SPOT_KEY in keys and self._spot is not None:
            out.append(OptionQuoteRecord(symbol="NIFTY 50", exchange="NSE", last_price=self._spot))
        return out


class Forbidden:
    """A reader that must never be asked — any call is a failure."""

    def option_quotes(self, keys: Sequence[str]) -> list[OptionQuoteRecord]:
        raise AssertionError(f"a Kite quote was made: {len(keys)} keys")

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]:
        raise AssertionError(f"a Kite history call was made for {token}")


# --- which contracts ----------------------------------------------------------------------------


class TestThePick:
    def test_two_nearest_expiries_fifteen_strikes_a_side_both_types(self) -> None:
        pick = pick_contracts(_contracts(), SPOT, DAY, OptionsConfig())
        assert pick.expiries == EXPIRIES[:2]
        assert pick.steps == {EXPIRIES[0]: Decimal(50), EXPIRIES[1]: Decimal(50)}
        per_expiry = {e: [c for c in pick.contracts if c.expiry == e] for e in pick.expiries}
        for rows in per_expiry.values():
            strikes = sorted({c.strike for c in rows})
            assert strikes[0] == Decimal(24250)
            assert strikes[-1] == Decimal(25750)
            assert len(strikes) == 31
            assert len(rows) == 62
        assert len(pick.contracts) == 124

    def test_one_call_with_the_spot_and_never_more_than_500(self) -> None:
        keys = pick_contracts(_contracts(), SPOT, DAY, OptionsConfig()).keys()
        assert SPOT_KEY in keys
        assert len(keys) == 125 <= QUOTE_BATCH_SIZE

    def test_a_pick_that_would_need_two_calls_is_refused(self) -> None:
        wide = OptionsConfig(chain=replace(ChainConfig(), snapshot_strikes=200))
        dense = [
            replace(c, strike=Decimal(20000) + Decimal(25) * i, instrument_token=BASE + 10**6 + j)
            for j, (i, c) in enumerate(
                (i, c) for i in range(401) for c in _contracts(EXPIRIES[:2])[:4]
            )
        ]
        pick = pick_contracts(dense, SPOT, DAY, wide)
        assert len(pick.contracts) > QUOTE_BATCH_SIZE
        with pytest.raises(TooManySymbols):
            pick.keys()

    def test_on_an_expiry_day_after_it_the_monthly_and_next_week(self) -> None:
        pick = pick_contracts(_contracts(), SPOT, dt.date(2026, 9, 23), OptionsConfig())
        assert pick.expiries == EXPIRIES[1:]

    def test_the_step_is_read_from_the_master(self) -> None:
        coarse = [c for c in _contracts() if c.strike % 100 == 0]
        pick = pick_contracts(coarse, SPOT, DAY, OptionsConfig())
        assert set(pick.steps.values()) == {Decimal(100)}


# --- what a row holds ---------------------------------------------------------------------------


class TestTheRows:
    def test_forward_iv_and_greeks_are_computed_at_write_and_rounded(self) -> None:
        cfg = OptionsConfig()
        pick = pick_contracts(_contracts(), SPOT, DAY, cfg)
        quotes = FakeQuotes(_contracts())
        answered = {q.key: q for q in quotes.option_quotes(pick.keys())}
        rows, no_greeks = snapshot_rows(
            pick, answered, SPOT, minute=collector.minute_of(NOW), now=NOW, config=cfg
        )
        assert len(rows) == 124
        assert no_greeks < 124
        row = next(
            r
            for r in rows
            if r["strike"] == Decimal(25000)
            and r["option_type"] == "CE"
            and r["expiry"] == EXPIRIES[1]
        )
        assert row["ts"] == dt.datetime(2026, 9, 22, 10, 0, tzinfo=IST)
        assert row["spot"] == Decimal("25012.35")
        forward = row["forward"]
        assert isinstance(forward, Decimal)
        assert abs(forward - SPOT) < 100
        for greek in ("iv", "delta", "gamma", "theta", "vega"):
            value = row[greek]
            assert isinstance(value, Decimal), greek
            exponent = value.as_tuple().exponent
            assert isinstance(exponent, int)
            assert -exponent <= 6, greek
        delta = row["delta"]
        assert isinstance(delta, Decimal)
        assert Decimal("0.3") < delta < Decimal("0.7")
        assert row["greeks_model"] == cfg.chain.greeks_model
        assert row["bid_qty"] == LOT
        assert row["source"] == "QUOTE"
        depth = row["depth_json"]
        assert isinstance(depth, dict)
        assert len(depth["buy"]) == 5
        assert len(depth["sell"]) == 5

    def test_no_spot_keeps_the_quotes_and_drops_forward_and_greeks(self) -> None:
        cfg = OptionsConfig()
        pick = pick_contracts(_contracts(), SPOT, DAY, cfg)
        answered = {
            q.key: q for q in FakeQuotes(_contracts(), spot=None).option_quotes(pick.keys())
        }
        rows, no_greeks = snapshot_rows(pick, answered, None, minute=NOW, now=NOW, config=cfg)
        assert len(rows) == no_greeks == 124
        assert all(r["forward"] is None and r["iv"] is None for r in rows)
        assert all(r["bid"] is not None for r in rows)

    def test_an_unquoted_contract_is_absent_not_invented(self) -> None:
        cfg = OptionsConfig()
        pick = pick_contracts(_contracts(), SPOT, DAY, cfg)
        answered = {q.key: q for q in FakeQuotes(_contracts()).option_quotes(pick.keys())}
        dropped = next(iter(k for k in answered if k.startswith("NFO:")))
        del answered[dropped]
        rows, _ = snapshot_rows(pick, answered, SPOT, minute=NOW, now=NOW, config=cfg)
        assert len(rows) == 123


# --- the gate: no call unless every condition holds ---------------------------------------------


def _day(is_trading: bool) -> collector.TradingDayCheck:
    async def check(day: dt.date) -> bool:
        del day
        return is_trading

    return check


def _kite_never() -> bool:
    raise AssertionError("the Kite session was consulted before a cheaper refusal")


class TestTheGate:
    @pytest.mark.parametrize(
        ("clock", "inside"),
        [
            (dt.time(9, 14, 59), False),
            (dt.time(9, 15), True),
            (dt.time(12, 0), True),
            (dt.time(15, 30), True),
            (dt.time(15, 30, 1), False),
        ],
    )
    def test_the_window_is_0915_to_1530_ist(self, clock: dt.time, inside: bool) -> None:
        assert in_session(dt.datetime.combine(DAY, clock, tzinfo=IST)) is inside

    async def test_the_flag_off_refuses_first(self) -> None:
        got = await collect_gate(NOW, enabled=False, kite_ok=_kite_never, trading_day=_day(True))
        assert got == "BASKFY_OPTIONS_COLLECT_ENABLED is false"

    async def test_outside_the_window_refuses_before_the_calendar(self) -> None:
        async def never(day: dt.date) -> bool:
            raise AssertionError(f"the calendar was read for {day}")

        evening = dt.datetime(2026, 9, 22, 18, 0, tzinfo=IST)
        got = await collect_gate(evening, enabled=True, kite_ok=_kite_never, trading_day=never)
        assert got == "outside 09:15-15:30 IST"

    async def test_a_holiday_refuses_before_the_kite_session(self) -> None:
        got = await collect_gate(NOW, enabled=True, kite_ok=_kite_never, trading_day=_day(False))
        assert got == "not an NSE trading day"

    async def test_no_kite_session_refuses(self) -> None:
        got = await collect_gate(NOW, enabled=True, kite_ok=lambda: False, trading_day=_day(True))
        assert got == "no usable Kite session"

    async def test_all_four_true_lets_the_minute_through(self) -> None:
        got = await collect_gate(NOW, enabled=True, kite_ok=lambda: True, trading_day=_day(True))
        assert got is None


class TestTheCeleryTasksMakeNoCallOnANonTradingDay:
    """The real task bodies, with the calendar saying "holiday" and every switch otherwise on:
    no provider is even built, so no Kite call is possible."""

    @pytest.fixture
    def wired(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        built: list[str] = []

        async def holiday(session: object, day: dt.date) -> bool:
            del session, day
            return False

        def build(*_args: object, **_kwargs: object) -> OptionsKite:
            built.append("built")
            raise AssertionError("a Kite adapter was built on a holiday")

        def run_in_session(
            operation: Callable[[object], Coroutine[object, object, object]],
        ) -> object:
            return asyncio.run(operation(object()))

        monkeypatch.setattr(celery_tasks, "is_session_day", holiday)
        monkeypatch.setattr(celery_tasks, "kite_session_usable", lambda: True)
        monkeypatch.setattr(celery_tasks, "build_options_kite", build)
        monkeypatch.setattr(celery_tasks, "run_in_session", run_in_session)
        monkeypatch.setattr(
            celery_tasks,
            "get_worker_settings",
            lambda: WorkerSettings(_env_file=None, options_collect_enabled=True),
        )
        return built

    def test_the_collector(self, wired: list[str]) -> None:
        from baskfy_worker.tasks.celery_tasks import options_collect_chain_task  # noqa: PLC0415

        # Saturday 26 Sep 2026 at 11:00 is inside the clock window; the calendar says no.
        out = options_collect_chain_task.run("2026-09-26T11:00:00+05:30")
        assert out["skipped"] == "not an NSE trading day"
        assert wired == []

    def test_the_index_bars(self, wired: list[str]) -> None:
        from baskfy_worker.tasks.celery_tasks import options_index_bars_task  # noqa: PLC0415

        out = options_index_bars_task.run("2026-10-02T11:00:00+05:30")  # Gandhi Jayanti
        assert out["skipped"] == "not an NSE trading day"
        assert wired == []

    def test_with_the_flag_off_not_even_the_database_is_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        monkeypatch.setattr(
            celery_tasks, "get_worker_settings", lambda: WorkerSettings(_env_file=None)
        )
        for task in (celery_tasks.options_collect_chain_task, celery_tasks.options_index_bars_task):
            out = task.run("2026-09-22T11:00:00+05:30")
            assert out["skipped"] == "BASKFY_OPTIONS_COLLECT_ENABLED is false"

    def test_the_flag_defaults_off(self) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415

        assert WorkerSettings(_env_file=None).options_collect_enabled is False


# --- the probe is read-only ---------------------------------------------------------------------


ORDER_VERBS: Final = re.compile(
    r"place_order|modify_order|cancel_order|place_gtt|modify_gtt|delete_gtt|exit_order|"
    r"OrderGateway|baskfy_execution"
)


@pytest.mark.parametrize(
    "module", [probe, collector, index_bars], ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_no_options_read_module_names_an_order_verb(module: object) -> None:
    source = Path(str(getattr(module, "__file__", ""))).read_text(encoding="utf-8")
    assert ORDER_VERBS.search(source) is None


class FakeBars:
    """A ``BarReader``: minute bars for NIFTY 50 from 2015, VIX from 2016; unknown token raises."""

    def __init__(self, starts: dict[int, dt.date]) -> None:
        self._starts = starts
        self.calls: list[tuple[int, object, object]] = []

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]:
        self.calls.append((token, start, end))
        if token not in self._starts:
            raise ValueError("invalid token")  # Kite's InputException, as translated
        lo = start if isinstance(start, dt.date) and not isinstance(start, dt.datetime) else None
        hi = end if isinstance(end, dt.date) and not isinstance(end, dt.datetime) else None
        first = self._starts[token]
        if lo is None or hi is None or hi < first:
            return []
        day = max(lo, first)
        ts = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
        v = Decimal(100)
        return [MinuteBarRecord(ts=ts, open=v, high=v, low=v, close=v)]


class FakeGeneral:
    def __init__(self, contracts: Sequence[Contract]) -> None:
        self._contracts = contracts
        self.baskets: list[Sequence[MarginLegRecord]] = []

    def option_contracts(self, underlying: str) -> list[OptionContractRecord]:
        return [
            OptionContractRecord(
                instrument_token=c.instrument_token,
                tradingsymbol=c.tradingsymbol,
                underlying=underlying,
                expiry=c.expiry,
                strike=c.strike,
                option_type="CE" if c.option_type is OptionType.CE else "PE",
                lot_size=c.lot_size,
                tick_size=c.tick_size,
            )
            for c in self._contracts
        ]

    def margins_shape(self) -> dict[str, object]:
        return {"equity": {"net": "float"}}

    def basket_order_margins(
        self, legs: Sequence[MarginLegRecord], *, consider_positions: bool = False
    ) -> BasketMarginRecord:
        assert consider_positions is False
        self.baskets.append(legs)
        return BasketMarginRecord(
            initial_total=Decimal(200000), final_total=Decimal(45000), legs=len(legs)
        )


def _dig(value: object, *path: str | int) -> object:
    """Walk a JSON report, asserting each step is the container it should be."""
    for step in path:
        if isinstance(step, int):
            assert isinstance(value, list)
            value = value[step]
        else:
            assert isinstance(value, dict), f"{step!r} under a {type(value).__name__}"
            value = value[step]
    return value


class TestTheProbe:
    def test_it_runs_all_six_reads_against_a_client_with_no_order_verb(self) -> None:
        contracts = _contracts()
        general = FakeGeneral(contracts)
        kite = OptionsKite(
            quotes=FakeQuotes(contracts),
            bars=FakeBars({256265: dt.date(2015, 3, 2), 264969: dt.date(2016, 1, 4)}),
            general=general,
        )
        expired = replace(contracts[0], instrument_token=1, expiry=dt.date(2026, 9, 15))
        report = probe.run_probe(
            kite,
            probe.ProbeInputs(
                today=DAY,
                index_tokens={"NIFTY 50": 256265, "INDIA VIX": 264969},
                expired=expired,
            ),
        )
        assert report["read_only"] is True
        nifty_earliest = _dig(report, "a_earliest_minute_history", "NIFTY 50", "result", "earliest")
        assert isinstance(nifty_earliest, str)
        assert nifty_earliest.startswith("2015-03-02")
        vix_earliest = _dig(report, "a_earliest_minute_history", "INDIA VIX", "result", "earliest")
        assert isinstance(vix_earliest, str)
        assert vix_earliest.startswith("2016-01-04")
        assert _dig(report, "b_expired_contract", "result", "in_todays_master") is False
        assert _dig(report, "b_expired_contract", "result", "history", "error") == "invalid token"
        expiries = _dig(report, "c_next_expiries", "result", "expiries")
        assert isinstance(expiries, list)
        assert [_dig(e, "strike_step_near_atm") for e in expiries] == ["50", "50", "50"]
        assert _dig(report, "e_option_quotes", "result", "answered") == 5
        assert _dig(report, "e_option_quotes", "result", "oi_unit", "verdict") == "UNITS"
        legs = _dig(report, "f_basket_margins", "result", "legs")
        assert isinstance(legs, list)
        assert [_dig(leg, "side") for leg in legs] == ["BUY", "BUY", "SELL", "SELL"]
        assert len(general.baskets) == 1

    def test_one_failing_read_does_not_stop_the_rest(self) -> None:
        contracts = _contracts()

        class Broken(FakeGeneral):
            def margins_shape(self) -> dict[str, object]:
                raise RuntimeError("margins down")

        kite = OptionsKite(
            quotes=FakeQuotes(contracts), bars=FakeBars({}), general=Broken(contracts)
        )
        report = probe.run_probe(kite, probe.ProbeInputs(today=DAY, index_tokens={}, expired=None))
        assert _dig(report, "d_margins_shape", "ok") is False
        assert _dig(report, "d_margins_shape", "error") == "margins down"
        assert _dig(report, "e_option_quotes", "ok") is True

    @pytest.mark.parametrize(
        ("ois", "verdict"),
        [
            ((LOT * 1000, LOT * 7, LOT * 12345), "UNITS"),
            ((1001, 7, 12346), "LOTS"),
            ((LOT * 3, 7), "UNDETERMINED"),
            ((0, 0), "UNDETERMINED"),
        ],
    )
    def test_the_oi_unit_verdict(self, ois: tuple[int, ...], verdict: str) -> None:
        sample = [
            (OptionQuoteRecord(symbol=f"S{i}", exchange="NFO", oi=oi), LOT)
            for i, oi in enumerate(ois)
        ]
        assert probe.oi_unit_verdict(sample)["verdict"] == verdict


# --- on a real database -------------------------------------------------------------------------


def _database_url() -> str | None:
    return os.environ.get(ENV_VAR)


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set")
)


@pytest.fixture(scope="module")
def op_url() -> str:
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


#: Kite's documented index tokens, used here only as fixture values on our own rows.
NIFTY_TOKEN: Final = 256265
VIX_TOKEN: Final = 264969


async def _index(session: AsyncSession, symbol: str, token: int) -> int:
    """The index's ``instrument`` row, created or pointed at ``token`` inside the rollback."""
    existing = (
        await session.execute(
            sa.select(Instrument).where(
                Instrument.exchange_id == NSE_EXCHANGE_ID, Instrument.symbol == symbol
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.instrument_type = "INDEX"
        existing.kite_token = token
        await session.flush()
        return existing.id
    row = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=symbol,
        name=symbol,
        instrument_type="INDEX",
        kite_token=token,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row.id


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        await session.execute(sa.delete(OpContract).where(OpContract.underlying == "NIFTY"))
        await session.execute(sa.delete(OpIndexMinute))
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _seed_master(session: AsyncSession, contracts: Sequence[Contract]) -> None:
    await session.execute(
        sa.insert(OpContract),
        [
            {
                "instrument_token": c.instrument_token,
                "tradingsymbol": c.tradingsymbol,
                "underlying": "NIFTY",
                "expiry": c.expiry,
                "strike": c.strike,
                "option_type": c.option_type.value,
                "lot_size": c.lot_size,
                "tick_size": c.tick_size,
                "first_seen": DAY,
                "last_seen": DAY,
                "expired": False,
            }
            for c in contracts
        ],
    )


async def _snapshot_count(session: AsyncSession, ts: dt.datetime) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(OpChainSnapshot)
                .where(OpChainSnapshot.ts == ts)
            )
        ).scalar_one()
    )


@requires_db
class TestTheCollectorOnADatabase:
    async def test_a_minute_round_trips_idempotently_in_one_call(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            nifty = await _index(session, "NIFTY 50", NIFTY_TOKEN)
            contracts = _contracts()
            await _seed_master(session, contracts)
            await index_bars.upsert_bars(
                session,
                nifty,
                [
                    MinuteBarRecord(
                        ts=NOW.replace(minute=0, second=0) - dt.timedelta(minutes=1),
                        open=SPOT,
                        high=SPOT,
                        low=SPOT,
                        close=SPOT,
                    )
                ],
            )
            quotes = FakeQuotes(contracts)
            first = await collect_minute(session, quotes, NOW)
            assert first.calls == 1
            assert quotes.batches == [125]
            assert first.rows == first.inserted == 124
            minute = collector.minute_of(NOW)
            assert await _snapshot_count(session, minute) == 124
            before = (
                await session.execute(
                    sa.select(OpChainSnapshot.instrument_token, OpChainSnapshot.iv).where(
                        OpChainSnapshot.ts == minute
                    )
                )
            ).all()
            again = await collect_minute(session, FakeQuotes(contracts), NOW)
            assert again.inserted == 0
            after = (
                await session.execute(
                    sa.select(OpChainSnapshot.instrument_token, OpChainSnapshot.iv).where(
                        OpChainSnapshot.ts == minute
                    )
                )
            ).all()
            assert sorted(before) == sorted(after)

    async def test_no_stored_spot_costs_one_more_quote_and_nothing_else(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            await _seed_master(session, _contracts())
            quotes = FakeQuotes(_contracts())
            report = await collect_minute(session, quotes, NOW)
            assert quotes.batches == [1, 125]
            assert report.calls == 2
            assert report.rows == 124

    async def test_both_partitions_exist_before_a_write_in_an_uncreated_month(
        self, op_url: str
    ) -> None:
        later_day = dt.date(2028, 3, 14)
        later = dt.datetime.combine(later_day, dt.time(10, 0, 5), tzinfo=IST)
        expiries = (dt.date(2028, 3, 14), dt.date(2028, 3, 21))
        async with _rolled_back(op_url) as session:
            await _seed_master(session, _contracts(expiries))
            quotes = FakeQuotes(_contracts(expiries))
            report = await collect_minute(session, quotes, later)
            assert report.partitions == [
                partition_name(later_day),
                partition_name(dt.date(2028, 4, 1)),
            ]
            for name in report.partitions:
                exists = (
                    await session.execute(sa.text("SELECT to_regclass(:n)"), {"n": name})
                ).scalar_one()
                assert exists is not None, name
            assert report.inserted == 124

    async def test_no_master_means_no_call(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            report = await collect_minute(session, Forbidden(), NOW)
            assert report.skipped is not None
            assert report.calls == 0


class DayBars:
    """A ``BarReader`` that serves one session's minutes, forming bar included."""

    def __init__(self, day: dt.date, close_at: dt.time = dt.time(15, 29)) -> None:
        self._day = day
        self._close = close_at
        self.calls: list[tuple[int, object, object]] = []

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]:
        self.calls.append((token, start, end))
        out: list[MinuteBarRecord] = []
        t = dt.datetime.combine(self._day, dt.time(9, 15), tzinfo=IST)
        last = dt.datetime.combine(self._day, self._close, tzinfo=IST)
        lo = (
            start
            if isinstance(start, dt.datetime)
            else dt.datetime.combine(start, dt.time(0, 0), tzinfo=IST)
        )
        hi = (
            end
            if isinstance(end, dt.datetime)
            else dt.datetime.combine(end, dt.time(23, 59), tzinfo=IST)
        )
        base = Decimal(25000) if token == NIFTY_TOKEN else Decimal("13.50")
        while t <= last:
            if lo <= t <= hi:
                v = base + Decimal(t.minute) / 100
                out.append(MinuteBarRecord(ts=t, open=v, high=v + 1, low=v - 1, close=v))
            t += dt.timedelta(minutes=1)
        return out


async def _bars(session: AsyncSession, instrument_id: int) -> list[tuple[dt.datetime, Decimal]]:
    rows = (
        await session.execute(
            sa.select(OpIndexMinute.ts, OpIndexMinute.close)
            .where(OpIndexMinute.instrument_id == instrument_id)
            .order_by(OpIndexMinute.ts)
        )
    ).all()
    return [(r[0], Decimal(r[1])) for r in rows]


@requires_db
class TestTheIndexBarsOnADatabase:
    async def test_intraday_writes_only_closed_minutes_two_calls_idempotent(
        self, op_url: str
    ) -> None:
        now = dt.datetime(2026, 9, 22, 9, 20, 30, tzinfo=IST)
        async with _rolled_back(op_url) as session:
            nifty = await _index(session, "NIFTY 50", NIFTY_TOKEN)
            await _index(session, "INDIA VIX", VIX_TOKEN)
            reader = DayBars(DAY)
            report = await index_bars.pull_intraday(session, reader, now)
            assert report.calls == 2
            stored = await _bars(session, nifty)
            # 09:15..09:19 have closed by 09:20:30; the forming 09:20 bar is not a fact yet.
            assert [ts.astimezone(IST).time() for ts, _ in stored] == [
                dt.time(9, m) for m in range(15, 20)
            ]
            again = await index_bars.pull_intraday(session, DayBars(DAY), now)
            assert again.calls == 2
            assert await _bars(session, nifty) == stored

    async def test_the_eod_reconcile_fills_the_whole_session(self, op_url: str) -> None:
        now = dt.datetime(2026, 9, 22, 15, 45, tzinfo=IST)
        async with _rolled_back(op_url) as session:
            nifty = await _index(session, "NIFTY 50", NIFTY_TOKEN)
            await _index(session, "INDIA VIX", VIX_TOKEN)
            await index_bars.reconcile_day(session, DayBars(DAY), DAY, now)
            assert len(await _bars(session, nifty)) == 375

    async def test_the_backfill_resumes_from_the_newest_stored_bar(self, op_url: str) -> None:
        now = dt.datetime(2026, 9, 23, 8, 0, tzinfo=IST)
        async with _rolled_back(op_url) as session:
            nifty = await _index(session, "NIFTY 50", NIFTY_TOKEN)
            await _index(session, "INDIA VIX", VIX_TOKEN)
            commits: list[int] = []

            async def checkpoint() -> None:
                commits.append(1)

            first = await index_bars.backfill(
                session, DayBars(DAY), dt.date(2026, 5, 1), DAY, now=now, checkpoint=checkpoint
            )
            assert first.calls == 2 * 3  # 145 days → three 60-day windows, per index
            assert len(commits) == first.calls
            assert first.written["NIFTY 50"] == 375
            resumed = DayBars(DAY)
            second = await index_bars.backfill(session, resumed, dt.date(2026, 5, 1), DAY, now=now)
            # Resumed at the stored day: one window per index, not three.
            assert second.calls == 2
            assert all(call[1] == DAY for call in resumed.calls)
            assert len(await _bars(session, nifty)) == 375

    async def test_a_missing_index_row_is_reported_not_guessed(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            await session.execute(
                sa.update(Instrument)
                .where(Instrument.symbol.in_(index_bars.INDEX_SYMBOLS))
                .values(kite_token=None)
            )
            report = await index_bars.pull_intraday(session, Forbidden(), NOW)
            assert report.calls == 0
            assert set(report.skipped) == set(index_bars.INDEX_SYMBOLS)
