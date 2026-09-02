"""docs/swing/04 §9-§10 — the plan explains every refusal; the journal keeps score in R."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup
from baskfy_core.swing.journal import ClosedTrade, exit_average, summarize
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.plan import (
    LineKind,
    PlanLine,
    Skipped,
    SkipReason,
    SwingAccount,
    WatchItem,
    assemble,
    build_entries,
    exit_lines,
    to_tick,
)
from baskfy_core.swing.stops import Action, ActionKind, ActionReason

D = Decimal
AS_OF = dt.date(2026, 9, 2)
TIER = ExposureTier(1, 4, 50.0, new_entries_allowed=True)
ACCOUNT = SwingAccount(D("1000000"), D("1000000"), frozenset(), D("0"))


def item(  # noqa: PLR0913 - one keyword per watch field
    symbol: str,
    *,
    score: str = "70",
    setup: Setup = Setup.FLAG,
    trigger: str = "100",
    stop: str = "96",
    locked: bool = False,
) -> WatchItem:
    return WatchItem(symbol, setup, D(trigger), D(stop), D("5"), D("300000000"), D(score), locked)


def entries(
    watch: list[WatchItem],
    *,
    account: SwingAccount = ACCOUNT,
    gate: MarketGate = MarketGate.GREEN,
    tier: ExposureTier = TIER,
) -> tuple[list[PlanLine], list[Skipped]]:
    return build_entries(
        as_of=AS_OF, watch=watch, account=account, gate=gate, tier=tier, config=DEFAULT_SWING_CONFIG
    )


def test_a_flag_becomes_a_buy_line_with_trigger_stop_size_and_risk() -> None:
    lines, skipped = entries([item("A")])
    assert skipped == []
    line = lines[0]
    assert line.kind is LineKind.BUY_ON_TRIGGER
    assert (line.trigger, line.stop, line.quantity) == (D("100.00"), D("96.00"), 1250)
    assert line.risk_inr == D("5000.00")
    assert "trail MA20" in line.note


def test_best_score_is_lined_up_first_and_the_tier_caps_the_count() -> None:
    watch = [
        item("LOW", score="40"),
        item("HIGH", score="90"),
        item("MID", score="60"),
        item("X", score="50"),
        item("Y", score="45"),
    ]
    lines, skipped = entries(watch)
    assert [line.symbol for line in lines] == ["HIGH", "MID", "X", "Y"]
    assert [(s.symbol, s.reason) for s in skipped] == [("LOW", SkipReason.TIER_FULL)]


def test_open_positions_count_against_the_tier() -> None:
    account = SwingAccount(D("1000000"), D("1000000"), frozenset({"P1", "P2", "P3"}), D("300000"))
    lines, skipped = entries([item("A"), item("B")], account=account)
    assert [line.symbol for line in lines] == ["A"]
    assert skipped[0].reason is SkipReason.TIER_FULL


def test_red_gate_refuses_every_entry_by_name() -> None:
    lines, skipped = entries([item("A"), item("B")], gate=MarketGate.RED)
    assert lines == []
    assert {s.reason for s in skipped} == {SkipReason.GATE_RED}


def test_parabolic_shorts_are_never_lines() -> None:
    _, skipped = entries([item("RUNNER", setup=Setup.PARABOLIC_SHORT)])
    assert skipped[0].reason is SkipReason.NOT_TRADEABLE_SETUP


def test_a_held_name_and_a_locked_name_are_skipped() -> None:
    account = SwingAccount(D("1000000"), D("1000000"), frozenset({"HELD"}), D("100000"))
    _, skipped = entries([item("HELD"), item("LOCKED", locked=True)], account=account)
    assert [(s.symbol, s.reason) for s in skipped] == [
        ("HELD", SkipReason.ALREADY_HELD),
        ("LOCKED", SkipReason.LOCKED_UPPER_CIRCUIT),
    ]


def test_exposure_ceiling_of_the_tier_binds() -> None:
    tight = ExposureTier(0, 8, 25.0, new_entries_allowed=True)
    watch = [item(f"S{i}", stop="99", score=str(90 - i)) for i in range(4)]  # each ~20% of equity
    lines, skipped = entries(watch, tier=tight)
    assert len(lines) == 1
    assert {s.reason for s in skipped} == {SkipReason.EXPOSURE_FULL}


def test_size_refusals_are_carried_with_their_reason() -> None:
    _, skipped = entries([item("WIDE", stop="85")])
    assert skipped[0].reason is SkipReason.SIZE_REFUSED
    assert skipped[0].detail == "STOP_TOO_WIDE"


def test_cash_spent_by_earlier_lines_is_not_spent_twice() -> None:
    account = SwingAccount(D("1000000"), D("150000"), frozenset(), D("0"))
    lines, _ = entries([item("A", score="90"), item("B", score="80")], account=account)
    assert sum((line.position_value for line in lines), D("0")) <= D("150000")


def test_exit_lines_mirror_the_stop_rules() -> None:
    lines = exit_lines(
        "X",
        [
            Action(ActionKind.SELL_PARTIAL, ActionReason.PARTIAL_INTO_STRENGTH, 100),
            Action(
                ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AFTER_PARTIAL, new_stop=D("100.02")
            ),
            Action(ActionKind.HOLD, ActionReason.NOTHING_TO_DO),
        ],
    )
    assert [(line.kind, line.quantity, line.stop) for line in lines] == [
        (LineKind.SELL_AT_OPEN, 100, None),
        (LineKind.RAISE_GTT_STOP, 0, D("100.00")),
    ]


def test_assembled_plan_puts_exits_first_totals_entries_and_hashes_stably() -> None:
    buys, skipped = entries([item("A")])
    sells = exit_lines("Z", [Action(ActionKind.SELL_ALL, ActionReason.CLOSE_BELOW_TRAIL_MA, 50)])
    plan = assemble(
        as_of=AS_OF, gate=MarketGate.GREEN, tier=TIER, entries=buys, exits=sells, skipped=skipped
    )
    assert [line.symbol for line in plan.lines] == ["Z", "A"]
    assert plan.total_risk_inr == D("5000.00")
    assert plan.total_new_exposure_inr == D("125000.00")
    again = assemble(
        as_of=AS_OF, gate=MarketGate.GREEN, tier=TIER, entries=buys, exits=sells, skipped=skipped
    )
    assert plan.plan_hash() == again.plan_hash()


def test_to_tick_snaps_to_five_paise() -> None:
    assert to_tick(D("101.23")) == D("101.25")
    assert to_tick(D("101.22")) == D("101.20")


# --- journal ----------------------------------------------------------------


def trade(
    symbol: str, exit_avg: str, *, entry: str = "100", stop: str = "96", qty: int = 100
) -> ClosedTrade:
    return ClosedTrade(
        symbol, "FLAG", AS_OF, AS_OF + dt.timedelta(days=10), D(entry), D(stop), D(exit_avg), qty
    )


def test_r_multiple_and_rupee_pnl() -> None:
    t = trade("A", "112")
    assert t.r_multiple == D("3.00")
    assert t.pnl_inr == D("1200.00")
    assert t.holding_days == 10


def test_his_own_numbers_win_a_third_of_the_time_and_still_make_money() -> None:
    """25-35% win rate, winners 3-5x losers → positive expectancy. The journal must show it."""
    trades = [
        trade("W1", "116"),
        trade("W2", "112"),
        trade("L1", "96"),
        trade("L2", "96"),
        trade("L3", "97"),
        trade("L4", "96"),
    ]
    s = summarize(trades)
    assert s.trades == 6
    assert s.win_rate_pct == D("33.33")
    assert s.avg_win_r == D("3.50")
    assert s.avg_loss_r == D("-0.94")
    assert s.expectancy_r > 0
    assert s.profit_factor is not None and s.profit_factor > 1
    assert s.current_loss_streak == 4
    assert s.largest_win_r == D("4.00")


def test_empty_journal_is_zeros_not_an_error() -> None:
    s = summarize([])
    assert (s.trades, s.expectancy_r, s.profit_factor, s.current_loss_streak) == (
        0,
        D("0"),
        None,
        0,
    )


def test_exit_average_is_share_weighted() -> None:
    assert exit_average([(100, D("110")), (200, D("104"))]) == D("106.00")
    with pytest.raises(ValueError, match="no exit"):
        exit_average([])
