"""``docs/fno/04`` defaults and bounds, and ``02`` Track B's ceilings, in ``fno.config``."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest

from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    F1_SEED_CAPITAL_INR,
    F2_SEED_CAPITAL_INR,
    BookConfig,
    CommonConfig,
    F1Config,
    F2Config,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    FoSleeveGroup,
    FutureCostRates,
    StopVolConfig,
    ceiling_violations,
    f1_sleeve_for,
    group_of,
)

CFG = DEFAULT_FNO_CONFIG


class TestDefaultsAre04:
    def test_section_1(self) -> None:
        assert CFG.f1.underlyings == ("NIFTY", "BANKNIFTY")
        assert CFG.f1.entry_sessions_before == 15
        assert (CFG.f1.plan_time, CFG.f1.entry_window_end) == (dt.time(9, 20), dt.time(10, 30))
        assert CFG.f1.loss_close_mult == Decimal("1.5")
        assert CFG.f1.profit_take_pct == Decimal(50)
        assert CFG.common.hard_exit_before_expiry == 1
        assert CFG.common.hard_exit_time == dt.time(15, 0)
        assert CFG.f1.max_open_per_underlying == 1
        assert CFG.common.plan_ttl_minutes == 30

    def test_section_2(self) -> None:
        assert (CFG.f1.short_sigma, CFG.f1.wing_sigma) == (Decimal("1.0"), Decimal("0.5"))
        assert CFG.f1.min_short_oi_lots == 500
        assert CFG.f1.max_spread_pct == Decimal(5)

    def test_section_3(self) -> None:
        assert CFG.common.max_lots == 2
        assert CFG.common.max_lots_ceiling == 10
        assert CFG.f1.risk_per_trade_pct == Decimal("1.0")
        assert CFG.f1.max_cost_share_pct == Decimal(25)
        assert Decimal(1_000_000) == F1_SEED_CAPITAL_INR
        assert Decimal(0) == F2_SEED_CAPITAL_INR

    def test_section_7(self) -> None:
        assert (CFG.f1.pause_consecutive, CFG.f1.pause_loss_r) == (3, Decimal("-0.6"))
        assert CFG.f2.month_pause_r == Decimal(-6)
        assert CFG.book.monthly_pause_inr == Decimal(75000)

    def test_section_10(self) -> None:
        f2 = CFG.f2
        assert f2.universe_turnover_pct == Decimal(60)
        assert f2.breakout_sessions == 20
        assert f2.trend_sessions == 50
        assert f2.stop_atr == Decimal("3.0")
        assert f2.trail is True
        assert f2.max_sessions == 40
        assert f2.roll_before_expiry == 1
        assert f2.max_open == 5
        assert f2.max_per_industry == 2

    def test_future_costs_are_04_section_10(self) -> None:
        r = FutureCostRates()
        assert r.stt_sell_pct == Decimal("0.05")
        assert r.exchange_txn_pct == Decimal("0.00173")
        assert r.stamp_buy_pct == Decimal("0.002")
        assert r.brokerage_per_order_inr == Decimal(20)
        assert r.gst_pct == Decimal(18)
        assert r.slippage_pct == Decimal("0.03")

    def test_stop_band_is_the_desks(self) -> None:
        s = StopVolConfig()
        assert (s.stop_min, s.stop_max, s.vol_mult) == (0.08, 0.12, 2.2)

    def test_series_rules_are_04_section_4(self) -> None:
        s = CFG.series
        assert (s.ca_up_ratio, s.ca_down_ratio, s.ca_exclusion_sessions) == (1.4, 0.7, 5)
        assert (s.rv_sessions, s.annualisation_sessions, s.atr_sessions) == (20, 252, 14)
        assert s.iv_min_sessions_left == 8
        assert s.iv_rate == 0.0
        assert s.basis_from == dt.date(2024, 7, 8)


class TestCeilingsAre02:
    def test_defaults(self) -> None:
        c = DEFAULT_FNO_CEILINGS
        assert c.risk_per_trade_inr_max == Decimal(25000)
        assert c.risk_pct_max == Decimal("1.0")
        assert c.max_open_positions_max == 10
        assert c.max_per_underlying_max == 1
        assert c.book_monthly_loss_inr_max == Decimal(75000)

    def test_the_defaults_sit_under_the_ceilings(self) -> None:
        assert ceiling_violations(CFG, DEFAULT_FNO_CEILINGS) == ()

    def test_a_setting_above_a_ceiling_is_named(self) -> None:
        cfg = FnoConfig(
            f1=F1Config(risk_per_trade_pct=Decimal("1.5")),
            book=BookConfig(monthly_pause_inr=Decimal(80000), max_per_underlying=2),
        )
        found = ceiling_violations(cfg, DEFAULT_FNO_CEILINGS)
        assert any("BASKFY_FNO_RISK_PCT_MAX" in f for f in found)
        assert any("BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX" in f for f in found)
        assert any("BASKFY_FNO_MAX_PER_UNDERLYING_MAX" in f for f in found)

    def test_a_lower_ceiling_catches_the_default(self) -> None:
        low = FnoCeilings(max_open_positions_max=4)
        assert any("MAX_OPEN_POSITIONS_MAX" in f for f in ceiling_violations(CFG, low))

    def test_a_zero_ceiling_is_refused(self) -> None:
        with pytest.raises(ValueError):
            FnoCeilings(risk_per_trade_inr_max=Decimal(0))


@pytest.mark.parametrize(
    ("factory", "field", "bad"),
    [
        (CommonConfig, "hard_exit_before_expiry", 0),
        (CommonConfig, "hard_exit_before_expiry", 6),
        (CommonConfig, "max_lots", 0),
        (CommonConfig, "max_lots", 11),
        (F1Config, "entry_sessions_before", 9),
        (F1Config, "entry_sessions_before", 21),
        (F1Config, "loss_close_mult", Decimal("0.9")),
        (F1Config, "loss_close_mult", Decimal("2.1")),
        (F1Config, "profit_take_pct", Decimal(29)),
        (F1Config, "profit_take_pct", Decimal(81)),
        (F1Config, "short_sigma", Decimal("0.7")),
        (F1Config, "short_sigma", Decimal("1.6")),
        (F1Config, "wing_sigma", Decimal("0.2")),
        (F1Config, "wing_sigma", Decimal("1.1")),
        (F1Config, "max_open_per_underlying", 2),
        (F1Config, "underlyings", ("NIFTY", "FINNIFTY")),
        (F1Config, "underlyings", ()),
        (F1Config, "entry_window_end", dt.time(15, 5)),
        (F1Config, "plan_time", dt.time(9, 10)),
        (F2Config, "universe_turnover_pct", Decimal(19)),
        (F2Config, "breakout_sessions", 61),
        (F2Config, "trend_sessions", 19),
        (F2Config, "stop_atr", Decimal("1.4")),
        (F2Config, "stop_atr", Decimal("5.1")),
        (F2Config, "max_sessions", 9),
        (F2Config, "roll_before_expiry", 0),
        (F2Config, "roll_before_expiry", 4),
        (F2Config, "max_open", 11),
    ],
)
def test_out_of_bounds_is_refused(factory: Callable[..., object], field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        factory(**{field: bad})


def test_hard_exit_is_never_zero_even_by_replace() -> None:
    with pytest.raises(ValueError, match="fo_hard_exit_before_expiry"):
        dataclasses.replace(CommonConfig(), hard_exit_before_expiry=0)


def test_the_bounds_themselves_are_admitted() -> None:
    F1Config(entry_sessions_before=10, loss_close_mult=Decimal(1), profit_take_pct=Decimal(30))
    F1Config(entry_sessions_before=20, loss_close_mult=Decimal(2), profit_take_pct=Decimal(80))
    CommonConfig(hard_exit_before_expiry=5, max_lots=10)
    F2Config(stop_atr=Decimal("1.5"), max_sessions=60, roll_before_expiry=3, max_open=10)


def test_sleeves_and_groups() -> None:
    assert f1_sleeve_for("NIFTY") is FoSleeve.F1N
    assert f1_sleeve_for("BANKNIFTY") is FoSleeve.F1B
    assert group_of(FoSleeve.F1B) is FoSleeveGroup.F1
    assert group_of(FoSleeve.F2) is FoSleeveGroup.F2
    with pytest.raises(ValueError):
        f1_sleeve_for("RELIANCE")


def test_no_auto_execute_field_exists() -> None:
    for cls in (CommonConfig, F1Config, F2Config, BookConfig, FnoConfig):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not any("auto" in n for n in names), cls.__name__
