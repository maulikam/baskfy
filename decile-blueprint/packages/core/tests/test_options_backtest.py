"""``04`` §13 / ``07`` §4 — the backtest engine: one sleeve, one tier, verbatim caveats."""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from baskfy_core.options import backtest
from baskfy_core.options.backtest import (
    TIER1_CAVEAT,
    TIER2_CAVEAT,
    TIER2_O2_CAVEAT,
    DayOutcome,
    Tier,
    caveats,
    run,
    sample_banner,
    tier2_quote,
)
from baskfy_core.options.config import DEFAULT_OPTIONS_CONFIG, OptionType, Sleeve
from baskfy_core.options.greeks import black76_price

DOCS = Path(__file__).resolve().parents[4] / "docs"


def _flat(text: str) -> str:
    return " ".join(text.split())


class TestCaveatsAreVerbatim:
    def test_tier1(self) -> None:
        assert TIER1_CAVEAT in _flat(
            (DOCS / "options" / "07-data-reality.md").read_text(encoding="utf-8")
        )

    def test_tier2_is_condor_07s_sentence(self) -> None:
        assert TIER2_CAVEAT in _flat(
            (DOCS / "condor" / "07-data-reality.md").read_text(encoding="utf-8")
        )

    def test_tier2_o2_is_04s_sentence(self) -> None:
        assert TIER2_O2_CAVEAT in _flat(
            (DOCS / "options" / "04-business-rules.md").read_text(encoding="utf-8")
        )

    def test_o2_carries_both_and_the_others_one(self) -> None:
        assert caveats(Sleeve.O2, Tier.MODELLED, 0, 60) == (TIER2_CAVEAT, TIER2_O2_CAVEAT)
        assert caveats(Sleeve.O1M, Tier.MODELLED, 0, 12) == (TIER2_CAVEAT,)
        assert caveats(Sleeve.O3A, Tier.SIGNALS, 0, 20) == (TIER1_CAVEAT,)

    def test_tier3_sample_and_banner(self) -> None:
        assert sample_banner(11, 12) == "Insufficient sample: 11 of 12 observed sessions."
        assert sample_banner(12, 12) is None
        assert caveats(Sleeve.O1M, Tier.OBSERVED, 12, 12) == ("Observed sample: 12 sessions.",)
        assert len(caveats(Sleeve.O2, Tier.OBSERVED, 5, 60)) == 2


def _days() -> list[DayOutcome]:
    return [
        DayOutcome(
            dt.date(2025, 12, 30), traded=True, net_pnl_inr=Decimal(500), r_multiple=Decimal("0.5")
        ),
        DayOutcome(dt.date(2026, 1, 6), traded=False, skip_reason="ER_TOO_HIGH"),
        DayOutcome(
            dt.date(2026, 1, 13), traded=True, net_pnl_inr=Decimal(-1000), r_multiple=Decimal(-1)
        ),
        DayOutcome(
            dt.date(2026, 1, 20), traded=True, net_pnl_inr=Decimal(1500), r_multiple=Decimal("1.5")
        ),
        DayOutcome(dt.date(2026, 1, 27), traded=False, skip_reason="ER_TOO_HIGH"),
    ]


class TestRun:
    def test_one_row_of_one_sleeve_and_one_tier(self) -> None:
        days = list(reversed(_days()))
        got = run(Sleeve.O1W, Tier.MODELLED, days, lambda d: d, min_sessions=20)
        assert (got.sleeve, got.tier, got.sessions, got.traded) == (Sleeve.O1W, Tier.MODELLED, 5, 3)
        assert (got.date_from, got.date_to) == (dt.date(2025, 12, 30), dt.date(2026, 1, 27))
        assert got.skipped_by_reason == (("ER_TOO_HIGH", 2),)
        assert got.by_year == ((2025, 1, 1), (2026, 4, 2))
        assert got.win_rate == Decimal(2) / 3
        assert got.expectancy_r == Decimal("1") / 3
        assert got.net_pnl_inr == Decimal(1000)
        assert got.max_drawdown_r == Decimal(1)
        assert got.caveats == (TIER2_CAVEAT,)

    def test_tier1_has_no_pnl(self) -> None:
        with pytest.raises(ValueError, match="no P&L"):
            run(Sleeve.O2, Tier.SIGNALS, _days(), lambda d: d, min_sessions=60)
        signals = [
            DayOutcome(
                dt.date(2026, 1, 6),
                traded=True,
                trigger_time=dt.time(10, 5),
                mfe_points=Decimal(80),
            )
        ]
        got = run(Sleeve.O2, Tier.SIGNALS, signals, lambda d: d, min_sessions=60)
        assert (
            got.traded,
            got.win_rate,
            got.expectancy_r,
            got.net_pnl_inr,
            got.max_drawdown_r,
        ) == (1, None, None, None, None)

    def test_a_priced_tier_needs_r_on_every_trade(self) -> None:
        with pytest.raises(ValueError, match="needs its R"):
            run(
                Sleeve.O3A,
                Tier.OBSERVED,
                [DayOutcome(dt.date(2026, 1, 6), traded=True)],
                lambda d: d,
                min_sessions=20,
            )

    def test_empty(self) -> None:
        got = run(Sleeve.O1M, Tier.OBSERVED, [], lambda d: d, min_sessions=12)
        assert (got.sessions, got.date_from, got.expectancy_r) == (0, None, None)

    def test_nothing_combines_two_results(self) -> None:
        """``04`` §13.4 — no function in the module accepts two results or a list of them."""
        for _, fn in inspect.getmembers(backtest, inspect.isfunction):
            hints = " ".join(str(p.annotation) for p in inspect.signature(fn).parameters.values())
            assert "BacktestResult" not in hints, fn.__name__
        result = run(Sleeve.O1M, Tier.OBSERVED, [], lambda d: d, min_sessions=12)
        name = dataclasses.fields(result)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(result, name, Sleeve.O2)


class TestTier2Quote:
    def test_flat_vix_forward_equals_spot(self) -> None:
        t = 7 / 365
        got = tier2_quote(
            Decimal(25000), Decimal(25100), years=t, vix_close=Decimal(14),
            kind=OptionType.CE, rate=0.065, rates=DEFAULT_OPTIONS_CONFIG.costs,
        )  # fmt: skip
        assert float(got.model) == pytest.approx(
            black76_price(25000, 25100, t, 0.065, 0.14, OptionType.CE)
        )
        half = got.model * Decimal("0.015")
        assert got.ask - got.model == half
        assert got.model - got.bid == half

    def test_the_bid_is_floored_at_zero(self) -> None:
        got = tier2_quote(
            Decimal(25000), Decimal(27000), years=1 / 365, vix_close=Decimal(10),
            kind=OptionType.CE, rate=0.065, rates=DEFAULT_OPTIONS_CONFIG.costs,
        )  # fmt: skip
        assert got.bid == 0
        assert got.ask == got.model + Decimal("0.10")
