"""``docs/fno/04`` §2, §3, §10 — the nightly scan's arithmetic (``baskfy_core.fno.scan``, FO4)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal

import polars as pl
import pytest

from baskfy_core.fno.condor import LegRole
from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    PlanState,
    SeriesConfig,
)
from baskfy_core.fno.exits import stop_from_vol
from baskfy_core.fno.scan import (
    CondorProposal,
    LegPrint,
    allocate_capacity,
    f2_signals,
    propose_condor,
    propose_future,
)
from baskfy_core.options.config import CostRates, OptionType, SizingMode

CFG = DEFAULT_FNO_CONFIG
SERIES = SeriesConfig()
EXPIRY = dt.date(2030, 12, 26)

#: A ₹10 lakh sleeve: ₹10,000 at 1 %. The arithmetic below is written against it; the seeded
#: F1 capital is ₹25 lakh (M.2) and is pinned in test_fno_config.
TEN_LAKH = Decimal(1_000_000)


def _sessions(start: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


DAYS = _sessions(dt.date(2030, 1, 1), 60)
LAST = DAYS[-1]


def _rows(
    symbol: str, settles: list[float], turnover: float, instrument: str = "FUTSTK"
) -> list[dict[str, object]]:
    return [
        {
            "trade_date": d,
            "symbol": symbol,
            "instrument": instrument,
            "expiry": EXPIRY,
            "open": s,
            "high": s * 1.01,
            "low": s * 0.99,
            "settle": s,
            "underlying": None,
            "open_interest": 1000,
            "turnover": turnover,
            "lot_size": 100,
        }
        for d, s in zip(DAYS, settles, strict=True)
    ]


def _panel() -> pl.DataFrame:
    rising = [100.0 + i for i in range(60)]
    falling = [200.0 - i for i in range(60)]
    flat = [50.0] * 60
    rows = [
        *_rows("AAA", rising, 5e9),
        *_rows("BBB", falling, 4e9),
        *_rows("CCC", flat, 3e9),
        *_rows("DDD", rising, 2e9),
        *_rows("EEE", rising, 1e9),
        *_rows("NIFTY", [20000.0 + 10 * i for i in range(60)], 9e11, "FUTIDX"),
    ]
    return pl.DataFrame(rows, schema_overrides={"underlying": pl.Float64})


class TestF2Signals:
    def test_breakout_trend_and_the_regime_gauge(self) -> None:
        out = {r["symbol"]: r for r in f2_signals(_panel(), LAST, CFG.f2, SERIES).to_dicts()}
        assert out["AAA"]["breakout"] is True and out["AAA"]["trend"] is True
        assert out["BBB"]["breakout"] is False and out["BBB"]["trend"] is False
        assert out["CCC"]["breakout"] is False, "equal to the prior high is not above it"
        assert out["NIFTY"]["trend"] is True

    def test_levels_are_rupees_on_the_held_settle(self) -> None:
        row = (
            f2_signals(_panel(), LAST, CFG.f2, SERIES)
            .filter(pl.col("symbol") == "AAA")
            .row(0, named=True)
        )
        assert row["close_inr"] == pytest.approx(159.0)
        # One contract throughout, so the series is the settle: the prior 20 closes peak at 158.
        assert row["prior_high_inr"] == pytest.approx(158.0)
        assert row["average_inr"] == pytest.approx(sum(110.0 + i for i in range(50)) / 50)

    def test_the_universe_is_the_top_sixty_percent_of_stocks_by_turnover(self) -> None:
        out = {r["symbol"]: r for r in f2_signals(_panel(), LAST, CFG.f2, SERIES).to_dicts()}
        assert {s for s, r in out.items() if r["in_universe"]} == {"AAA", "BBB", "CCC"}
        assert out["NIFTY"]["in_universe"] is False, "an index future is not an F&O stock"
        assert out["AAA"]["ranked"] == 5

    def test_a_short_window_says_null_not_false(self) -> None:
        short = _panel().filter(pl.col("trade_date") >= DAYS[30])
        row = (
            f2_signals(short, LAST, CFG.f2, SERIES)
            .filter(pl.col("symbol") == "AAA")
            .row(0, named=True)
        )
        assert row["breakout"] is True
        assert row["trend"] is None, "29 good sessions cannot answer a 50-session average"

    def test_a_row_after_the_session_is_refused(self) -> None:
        with pytest.raises(ValueError, match="look-ahead"):
            f2_signals(_panel(), DAYS[-2], CFG.f2, SERIES)


class TestAllocateCapacity:
    def test_max_open_counts_the_book_and_admits_in_order(self) -> None:
        got = allocate_capacity(
            [("A", "it"), ("B", "bank"), ("C", "auto")], ["pharma", "metal", "fmcg"], CFG.f2
        )
        assert got["A"] is None and got["B"] is None
        assert got["C"] is not None and "5 of f2_max_open 5" in got["C"]

    def test_two_per_industry_including_open_positions(self) -> None:
        got = allocate_capacity([("A", "it"), ("B", "it"), ("C", None)], ["it"], CFG.f2)
        assert got["A"] is None
        assert got["B"] is not None and "industry it" in got["B"]
        assert got["C"] is None, "no known industry: capped by f2_max_open only"


class TestProposeFuture:
    def test_stop_gtt_and_paper_one_lot_with_the_ceiling_recorded(self) -> None:
        p = propose_future(
            entry=Decimal("1000"),
            atr=Decimal("40"),
            ann_vol=0.30,
            lot_size=500,
            capital_inr=Decimal(0),
            config=CFG,
            ceilings=DEFAULT_FNO_CEILINGS,
        )
        assert p.stop == Decimal("880.00")
        assert p.gtt_trigger == max(p.stop, stop_from_vol(Decimal(1000), 0.30, CFG.stop_vol))
        assert p.risk_per_lot_inr == Decimal("60000.00")
        assert p.sizing.lots == 1 and p.sizing.sizing_mode is SizingMode.PAPER_ONE_LOT
        assert p.sizing.lots_at_ceiling == 0, "₹25,000 cannot carry ₹60,000 of one lot's risk"
        assert p.cost.total > 0 and p.cost_share_pct > 0

    def test_with_capital_a_lot_too_big_for_the_budget_is_rejected_size(self) -> None:
        p = propose_future(
            entry=Decimal("1000"),
            atr=Decimal("40"),
            ann_vol=0.30,
            lot_size=500,
            capital_inr=Decimal(1_000_000),
            config=CFG,
            ceilings=DEFAULT_FNO_CEILINGS,
        )
        assert p.sizing.state is PlanState.REJECTED_SIZE and p.sizing.lots == 0


def _chain(
    settles: Mapping[tuple[int, str], float], oi: int = 10_000_000, vol: int = 100
) -> dict[tuple[Decimal, OptionType], LegPrint]:
    return {
        (Decimal(k), OptionType(t)): LegPrint(Decimal(str(s)), oi, vol)
        for (k, t), s in settles.items()
    }


STRIKES = [Decimal(k) for k in range(18000, 22100, 100)]
GOOD = {(20800, "CE"): 60, (21100, "CE"): 30, (19200, "PE"): 70, (18900, "PE"): 35}


def _condor(
    prints: Mapping[tuple[Decimal, OptionType], LegPrint], lot_size: int = 25
) -> CondorProposal:
    return propose_condor(
        forward=Decimal(20000),
        iv=0.15,
        days_to_expiry=21,
        call_strikes=STRIKES,
        put_strikes=STRIKES,
        prints=prints,
        lot_size=lot_size,
        capital_inr=TEN_LAKH,
        config=CFG,
        rates=CostRates(),
        ceilings=DEFAULT_FNO_CEILINGS,
    )


class TestProposeCondor:
    def test_the_strikes_credit_and_size_of_04_section_2(self) -> None:
        p = _condor(_chain(GOOD))
        assert p.state is None, p.reasons
        assert p.strikes is not None
        assert (p.strikes.long_call, p.strikes.short_call) == (Decimal(21100), Decimal(20800))
        assert (p.strikes.short_put, p.strikes.long_put) == (Decimal(19200), Decimal(18900))
        assert p.credit == Decimal(65)
        assert p.max_loss_per_unit == Decimal(235)
        assert p.max_loss_per_lot_inr == Decimal("5875.00")
        assert p.sizing is not None and p.sizing.lots == 1
        assert p.cost is not None and p.cost.cost_share_pct is not None
        assert p.entry[LegRole.SHORT_PUT] == Decimal(70)

    def test_a_lot_whose_max_loss_exceeds_the_budget_is_rejected_size(self) -> None:
        p = _condor(_chain(GOOD), lot_size=75)
        assert p.state is PlanState.REJECTED_SIZE
        assert any("0 lots" in r for r in p.reasons)

    def test_liquidity_refusals_are_named(self) -> None:
        prints = _chain(GOOD, oi=75 * 100)
        prints[(Decimal(21100), OptionType.CE)] = LegPrint(Decimal(30), 10_000_000, 0)
        p = _condor(prints)
        assert p.state is PlanState.REJECTED_LIQUIDITY
        assert "long call 21100: the wing did not trade (volume 0)" in p.reasons
        assert any(r.startswith("short put 19200: OI") for r in p.reasons)

    def test_a_missing_leg_print_is_liquidity_not_a_guess(self) -> None:
        prints = _chain(GOOD)
        del prints[(Decimal(18900), OptionType.PE)]
        p = _condor(prints)
        assert p.state is PlanState.REJECTED_LIQUIDITY and p.credit is None
        assert "long put 18900: no settle in the bhavcopy" in p.reasons

    def test_a_credit_the_costs_eat_is_rejected_cost(self) -> None:
        cheap = {(20800, "CE"): 3, (21100, "CE"): 1, (19200, "PE"): 3, (18900, "PE"): 1}
        p = _condor(_chain(cheap))
        assert p.state is PlanState.REJECTED_COST
        assert any("% of the credit" in r for r in p.reasons)

    def test_no_listed_strike_is_rejected_structure(self) -> None:
        p = propose_condor(
            forward=Decimal(20000),
            iv=0.15,
            days_to_expiry=21,
            call_strikes=[Decimal(20000)],
            put_strikes=STRIKES,
            prints=_chain(GOOD),
            lot_size=25,
            capital_inr=TEN_LAKH,
            config=CFG,
            rates=CostRates(),
            ceilings=DEFAULT_FNO_CEILINGS,
        )
        assert p.state is PlanState.REJECTED_STRUCTURE and p.reasons
