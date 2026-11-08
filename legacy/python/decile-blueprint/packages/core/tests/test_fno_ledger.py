"""FO10, pure: the journal figures, the ledger, ``04`` §7's pauses and ``04`` §9's checklist.

Every threshold is ``04``'s: -0.6R three times running per F1 underlying, -6R in a calendar
month for F2, ``fo_book_config.monthly_pause_inr`` (≤ the ₹75,000 ceiling) for the book — and
paper and live are never pooled (``03`` §6; DECISIONS-FO FO10.1 corrects FO4.9).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.fno.checklist import (
    CycleOutcome,
    F1Cycle,
    F2Close,
    LegFill,
    Violation,
    ViolationKind,
    into_expiry,
    journal_gap,
    mark_gaps,
    missed_cycles,
    paper_checklist,
    uncovered_steps,
)
from baskfy_core.fno.config import DEFAULT_FNO_CEILINGS, FoSleeve, FutureCostRates, PlanKind
from baskfy_core.fno.config import Structure as FoStructure
from baskfy_core.fno.costs import future_charges
from baskfy_core.fno.journal import FoFillRecord, PooledRows, journal_figures, r_multiple
from baskfy_core.fno.ledger import (
    ClosedTrade,
    FoPause,
    OpenMark,
    PauseCode,
    book_pause_limit,
    build_ledger,
    column_pause_applies,
    evaluate_pauses,
    figures,
    pauses_for,
)
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import CostFill, charges

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D = Decimal


def trade(  # noqa: PLR0913 - one journal row
    n: int,
    sleeve: FoSleeve,
    r: str,
    *,
    on: dt.date,
    simulated: bool = True,
    inr: str | None = None,
    symbol: str | None = None,
) -> ClosedTrade:
    return ClosedTrade(
        position_id=n,
        sleeve=sleeve,
        symbol=symbol or {"F1N": "NIFTY", "F1B": "BANKNIFTY"}.get(sleeve.value, "RELIANCE"),
        simulated=simulated,
        closed_on=on,
        closed_at=dt.datetime.combine(on, dt.time(15, 0), tzinfo=IST),
        net_pnl_inr=D(inr) if inr is not None else D(r) * 10000,
        r_multiple=D(r),
        closed_reason="LOSS_CLOSE",
    )


def run(sleeve: FoSleeve, rs: list[str], *, simulated: bool = True) -> list[ClosedTrade]:
    return [
        trade(i + 1, sleeve, r, on=dt.date(2026, 7, 1 + i), simulated=simulated)
        for i, r in enumerate(rs)
    ]


AS_OF = dt.date(2026, 7, 20)


def pauses(
    trades: list[ClosedTrade],
    *,
    simulated: bool = True,
    book: str = "0",
    lifted_after: dict[FoSleeve, dt.datetime] | None = None,
) -> tuple[FoPause, ...]:
    return evaluate_pauses(
        trades,
        simulated=simulated,
        as_of=AS_OF,
        monthly_pause_inr=D(book),
        lifted_after=lifted_after,
    )


def codes(found: tuple[FoPause, ...]) -> list[tuple[str, PauseCode]]:
    return [(p.scope, p.code) for p in found]


# --- F1: three consecutive closes at or below -0.6R, per underlying -------------------------------


class TestF1Pause:
    def test_three_at_exactly_minus_0_6r_pause_that_underlying(self) -> None:
        found = pauses(run(FoSleeve.F1N, ["-0.60", "-0.73", "-0.60"]))
        assert codes(found) == [("F1N", PauseCode.F1_LOSS_RUN)]
        assert found[0].paused_until is None  # stands until lifted (FO10.2)
        assert pauses_for(found, FoSleeve.F1N) and not pauses_for(found, FoSleeve.F1B)
        assert not pauses_for(found, FoSleeve.F2)

    def test_minus_0_59r_breaks_the_run(self) -> None:
        assert pauses(run(FoSleeve.F1N, ["-0.60", "-0.59", "-0.73"])) == ()

    def test_two_are_not_three(self) -> None:
        assert pauses(run(FoSleeve.F1N, ["-0.73", "-0.73"])) == ()

    def test_a_win_after_the_losses_clears_it(self) -> None:
        assert pauses(run(FoSleeve.F1N, ["-0.7", "-0.7", "-0.7", "0.3"])) == ()

    def test_the_other_underlying_does_not_count(self) -> None:
        trades = [
            *run(FoSleeve.F1N, ["-0.7", "-0.7"]),
            trade(9, FoSleeve.F1B, "-0.7", on=dt.date(2026, 7, 9)),
        ]
        assert pauses(trades) == ()

    def test_a_lift_restarts_the_run(self) -> None:
        trades = run(FoSleeve.F1N, ["-0.7", "-0.7", "-0.7"])
        lifted = {FoSleeve.F1N: dt.datetime(2026, 7, 3, 16, 0, tzinfo=IST)}
        assert pauses(trades, lifted_after=lifted) == ()
        more = [*trades, *[
            trade(10 + i, FoSleeve.F1N, "-0.61", on=dt.date(2026, 7, 6 + i)) for i in range(3)
        ]]  # fmt: skip
        assert codes(pauses(more, lifted_after=lifted)) == [("F1N", PauseCode.F1_LOSS_RUN)]


# --- F2: the calendar month's closed trades at or below -6R ---------------------------------------


class TestF2Pause:
    def test_minus_6r_pauses_to_the_month_end(self) -> None:
        found = pauses(run(FoSleeve.F2, ["-1.00", "-2.50", "-2.50"]))
        assert codes(found) == [("F2", PauseCode.F2_MONTH_R)]
        assert found[0].paused_until == dt.date(2026, 7, 31)
        assert pauses_for(found, FoSleeve.F2) and not pauses_for(found, FoSleeve.F1N)

    def test_minus_5_99r_does_not(self) -> None:
        assert pauses(run(FoSleeve.F2, ["-1.00", "-2.49", "-2.50"])) == ()

    def test_last_months_losses_are_last_months(self) -> None:
        june = [trade(i, FoSleeve.F2, "-3", on=dt.date(2026, 6, 20 + i)) for i in range(3)]
        assert pauses([*june, trade(9, FoSleeve.F2, "-1", on=dt.date(2026, 7, 2))]) == ()


# --- the book: the month's realised FO loss at the rupee limit ------------------------------------


class TestBookPause:
    def test_zero_means_the_75000_ceiling(self) -> None:
        assert book_pause_limit(D(0), DEFAULT_FNO_CEILINGS) == D(75000)
        losses = [
            trade(1, FoSleeve.F1N, "-0.5", on=dt.date(2026, 7, 1), inr="-50000.00"),
            trade(2, FoSleeve.F2, "-0.5", on=dt.date(2026, 7, 2), inr="-25000.00"),
        ]
        found = pauses(losses)
        assert codes(found) == [("BOOK", PauseCode.BOOK_MONTH_INR)]
        assert found[0].paused_until == dt.date(2026, 7, 31)
        assert all(pauses_for(found, s) for s in FoSleeve)

    def test_one_paisa_short_of_the_limit_does_not(self) -> None:
        losses = [trade(1, FoSleeve.F2, "-0.5", on=dt.date(2026, 7, 1), inr="-74999.99")]
        assert pauses(losses) == ()

    def test_a_set_amount_is_the_limit_and_the_ceiling_holds_it(self) -> None:
        losses = [trade(1, FoSleeve.F1N, "-0.5", on=dt.date(2026, 7, 1), inr="-50000.00")]
        assert codes(pauses(losses, book="50000")) == [("BOOK", PauseCode.BOOK_MONTH_INR)]
        assert pauses(losses, book="50000.01") == ()
        assert book_pause_limit(D(100000), DEFAULT_FNO_CEILINGS) == D(75000)

    def test_wins_offset_losses_in_the_month(self) -> None:
        rows = [
            trade(1, FoSleeve.F1N, "-0.5", on=dt.date(2026, 7, 1), inr="-80000.00"),
            trade(2, FoSleeve.F1B, "0.5", on=dt.date(2026, 7, 2), inr="10000.00"),
        ]
        assert pauses(rows) == ()


# --- paper and live never pooled ------------------------------------------------------------------


class TestNeverPooled:
    def test_a_live_loss_never_pauses_paper(self) -> None:
        live = run(FoSleeve.F1N, ["-0.7", "-0.7", "-0.7"], simulated=False)
        live += [trade(9, FoSleeve.F2, "-7", on=dt.date(2026, 7, 9), simulated=False,
                       inr="-90000")]  # fmt: skip
        assert pauses(live, simulated=True) == ()
        assert {p.scope for p in pauses(live, simulated=False)} == {"F1N", "F2", "BOOK"}

    def test_a_paper_loss_never_pauses_live(self) -> None:
        paper = run(FoSleeve.F1N, ["-0.7", "-0.7", "-0.7"])
        assert pauses(paper, simulated=False) == ()
        assert all(p.reason.startswith("PAPER:") for p in pauses(paper))

    def test_a_mixed_run_is_not_a_run(self) -> None:
        mixed = [
            trade(1, FoSleeve.F1N, "-0.7", on=dt.date(2026, 7, 1)),
            trade(2, FoSleeve.F1N, "-0.7", on=dt.date(2026, 7, 2), simulated=False),
            trade(3, FoSleeve.F1N, "-0.7", on=dt.date(2026, 7, 3)),
        ]
        assert pauses(mixed) == () and pauses(mixed, simulated=False) == ()

    def test_one_figure_refuses_both_modes(self) -> None:
        with pytest.raises(PooledRows):
            figures(
                [
                    trade(1, FoSleeve.F2, "1", on=dt.date(2026, 7, 1)),
                    trade(2, FoSleeve.F2, "1", on=dt.date(2026, 7, 2), simulated=False),
                ],
                [],
                AS_OF,
            )

    def test_a_stored_pause_binds_its_own_mode(self) -> None:
        day = dt.date(2026, 7, 10)
        until = dt.date(2026, 7, 31)
        assert column_pause_applies(until, "PAPER:F2_MONTH_R", simulated=True, day=day)
        assert not column_pause_applies(until, "PAPER:F2_MONTH_R", simulated=False, day=day)
        assert column_pause_applies(until, "LIVE:BOOK_MONTH_INR", simulated=False, day=day)
        assert column_pause_applies(until, "set by hand", simulated=False, day=day)
        assert column_pause_applies(until, None, simulated=True, day=day)
        assert not column_pause_applies(until, "set by hand", simulated=True,
                                        day=dt.date(2026, 8, 1))  # fmt: skip
        assert not column_pause_applies(None, "PAPER:F2_MONTH_R", simulated=True, day=day)


# --- the ledger -----------------------------------------------------------------------------------


class TestLedger:
    def test_lines_and_books_per_mode(self) -> None:
        trades = [
            trade(1, FoSleeve.F1N, "0.40", on=dt.date(2026, 6, 20), inr="10000"),
            trade(2, FoSleeve.F1N, "-0.70", on=dt.date(2026, 7, 2), inr="-17500"),
            trade(3, FoSleeve.F1N, "0.20", on=dt.date(2026, 7, 9), inr="5000"),
            trade(4, FoSleeve.F2, "1.50", on=dt.date(2026, 7, 10), inr="3000"),
            trade(5, FoSleeve.F1N, "-1.00", on=dt.date(2026, 7, 3), inr="-25000", simulated=False),
        ]
        marks = [OpenMark(7, FoSleeve.F1B, "BANKNIFTY", True, dt.date(2026, 7, 17), D("1200"),
                          D("25000"))]  # fmt: skip
        ledger = build_ledger(trades, marks, AS_OF)
        nifty = ledger.line(FoSleeve.F1N, "NIFTY", simulated=True)
        assert nifty is not None
        assert (nifty.closed, nifty.wins) == (3, 2)
        assert (nifty.realised_inr, nifty.realised_r) == (D("-2500"), D("-0.10"))
        assert (nifty.mtd_closed, nifty.mtd_realised_inr) == (2, D("-12500"))
        assert nifty.max_drawdown_inr == D("17500") and nifty.max_drawdown_r == D("0.70")
        live = ledger.line(FoSleeve.F1N, "NIFTY", simulated=False)
        assert live is not None and live.realised_inr == D("-25000")
        paper = ledger.book(simulated=True)
        assert paper is not None
        assert paper.realised_inr == D("500") and paper.open_marked_inr == D("1200")
        assert paper.total_inr == D("1700") and paper.open_positions == 1
        live_book = ledger.book(simulated=False)
        assert live_book is not None and live_book.realised_inr == D("-25000")
        assert live_book.open_positions == 0

    def test_a_close_after_as_of_is_not_in_it(self) -> None:
        later = [trade(1, FoSleeve.F2, "1", on=dt.date(2026, 7, 21))]
        line = build_ledger(later, [], AS_OF).line(FoSleeve.F2, "RELIANCE", simulated=True)
        assert line is not None and line.closed == 0


# --- the journal figures --------------------------------------------------------------------------


def _fill(plan: str, kind: PlanKind, side: Side, price: str, qty: int, day: int) -> FoFillRecord:  # noqa: PLR0913, PLR0917 - one fill
    return FoFillRecord(plan, kind, side, D(price), qty,
                        dt.datetime(2026, 7, day, 10, 0, tzinfo=IST))  # fmt: skip


class TestJournalFigures:
    def test_a_condor_in_rupees_and_r_with_every_order_charged(self) -> None:
        entry = [
            _fill("E", PlanKind.ENTRY, Side.BUY, "10.50", 65, 1),
            _fill("E", PlanKind.ENTRY, Side.BUY, "12.50", 65, 1),
            _fill("E", PlanKind.ENTRY, Side.SELL, "55.00", 65, 1),
            _fill("E", PlanKind.ENTRY, Side.SELL, "60.00", 65, 1),
        ]
        exit_ = [
            _fill("X", PlanKind.EXIT, Side.BUY, "20.50", 65, 9),
            _fill("X", PlanKind.EXIT, Side.BUY, "18.50", 65, 9),
            _fill("X", PlanKind.EXIT, Side.SELL, "2.75", 65, 9),
            _fill("X", PlanKind.EXIT, Side.SELL, "1.75", 65, 9),
        ]
        fig = journal_figures(
            structure=FoStructure.IRON_CONDOR, fills=[*entry, *exit_], r_inr=D("20020.00"),
            marks_inr=[D("130"), D("-400"), D("715")], option_rates=CostRates(),
            future_rates=FutureCostRates(),
        )  # fmt: skip
        assert fig.entry_inr == D("92.00") * 65  # the credit taken
        assert fig.exit_inr == D("34.50") * 65  # the cost to close
        assert fig.gross_pnl_inr == D("57.50") * 65
        rates = CostRates()
        expected = charges([CostFill(f.side, f.price, f.quantity) for f in entry], rates).total
        expected += charges([CostFill(f.side, f.price, f.quantity) for f in exit_], rates).total
        assert fig.costs_inr == expected and fig.entry_costs.orders == 4
        assert fig.net_pnl_inr == fig.gross_pnl_inr - fig.costs_inr
        assert fig.r_multiple == r_multiple(fig.net_pnl_inr, D("20020.00"))
        assert fig.mae_r == D("-0.02") and fig.mfe_r == D("0.04")
        assert fig.cost_detail()["total"] == str(fig.costs_inr)

    def test_an_f2_roll_is_journalled_with_its_own_costs(self) -> None:
        fills = [
            _fill("E", PlanKind.ENTRY, Side.BUY, "1400.00", 500, 2),
            _fill("R1", PlanKind.ROLL, Side.SELL, "1430.00", 500, 23),
            _fill("R1", PlanKind.ROLL, Side.BUY, "1438.10", 500, 23),
            _fill("X", PlanKind.EXIT, Side.SELL, "1460.00", 500, 28),
        ]
        rates = FutureCostRates()
        fig = journal_figures(structure=FoStructure.FUTURE, fills=fills, r_inr=D("30000"),
                              marks_inr=[], option_rates=CostRates(),
                              future_rates=rates)  # fmt: skip
        assert fig.gross_pnl_inr == D("25950.00")  # (1430-1400 + 1460-1438.10) x 500
        assert len(fig.rolls) == 1
        roll = fig.rolls[0]
        assert (roll.plan_id, roll.on) == ("R1", dt.date(2026, 7, 23))
        assert (roll.sold_inr, roll.bought_inr) == (D("715000.00"), D("719050.00"))
        charged = future_charges(D("719050.00"), D("715000.00"), 2, rates)
        assert roll.costs.total == charged.total - charged.slippage  # slippage is in the price
        assert roll.costs.orders == 2 and roll.costs.stt > 0
        assert fig.costs_inr == fig.entry_costs.total + fig.exit_costs.total + roll.costs.total
        assert fig.mae_r is None and fig.mfe_r is None

    def test_r_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="R must be positive"):
            journal_figures(structure=FoStructure.FUTURE, fills=[], r_inr=D(0), marks_inr=[],
                            option_rates=CostRates(), future_rates=FutureCostRates())  # fmt: skip

    def test_r_is_rounded_to_the_column_half_up(self) -> None:
        assert r_multiple(D("-6050"), D("10000")) == D("-0.61")
        assert r_multiple(D("-6000"), D("10000")) == D("-0.60")


# --- the checklist --------------------------------------------------------------------------------


SESSIONS = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(400)]
SESSIONS = [d for d in SESSIONS if d.weekday() < 5]


def cycles(sleeve: FoSleeve, outcomes: str, start_month: int = 1) -> list[F1Cycle]:
    kinds = {"O": CycleOutcome.OPENED, "S": CycleOutcome.SKIPPED, "M": CycleOutcome.MISSED}
    return [
        F1Cycle(sleeve, dt.date(2026, start_month + i, 5), kinds[c]) for i, c in enumerate(outcomes)
    ]


class TestChecklist:
    def test_f1_needs_six_cycles_each_and_four_opened(self) -> None:
        f1 = [*cycles(FoSleeve.F1N, "OSSOSS"), *cycles(FoSleeve.F1B, "SOSSOS")]
        met = paper_checklist(as_of=dt.date(2026, 7, 1), f1_cycles=f1, f2_start=None,
                              f2_closes=[], sessions=SESSIONS, violations=[])  # fmt: skip
        assert [(t.sleeve, t.cycles, t.opened) for t in met.f1] == [
            (FoSleeve.F1N, 6, 2), (FoSleeve.F1B, 6, 2)]  # fmt: skip
        assert met.f1_opened == 4 and met.f1_met
        three = [*cycles(FoSleeve.F1N, "OSSSSS"), *cycles(FoSleeve.F1B, "SOSSOS")]
        assert not paper_checklist(
            as_of=dt.date(2026, 7, 1),
            f1_cycles=three,
            f2_start=None,
            f2_closes=[],
            sessions=SESSIONS,
            violations=[],
        ).f1_met
        five = [*cycles(FoSleeve.F1N, "OOOOO"), *cycles(FoSleeve.F1B, "OOOOOO")]
        assert not paper_checklist(
            as_of=dt.date(2026, 7, 1),
            f1_cycles=five,
            f2_start=None,
            f2_closes=[],
            sessions=SESSIONS,
            violations=[],
        ).f1_met

    def test_a_missed_cycle_is_a_violation_and_restarts_the_count(self) -> None:
        f1 = [*cycles(FoSleeve.F1N, "OOMOOOOO"), *cycles(FoSleeve.F1B, "OOOOOOOO")]
        v = missed_cycles(f1)
        assert [(x.kind, x.sleeve, x.on) for x in v] == [
            (ViolationKind.MISSED_CYCLE, FoSleeve.F1N, dt.date(2026, 3, 5))]  # fmt: skip
        out = paper_checklist(as_of=dt.date(2026, 9, 1), f1_cycles=f1, f2_start=None,
                              f2_closes=[], sessions=SESSIONS, violations=v)  # fmt: skip
        assert [t.cycles for t in out.f1] == [5, 5]  # counted from April on, both underlyings
        assert not out.f1_met
        assert out.violations_by_kind() == {"MISSED_CYCLE": 1}

    def test_f2_needs_60_sessions_15_closed_and_3_rolls(self) -> None:
        start = dt.date(2026, 1, 1)
        closes = [F2Close(dt.date(2026, 2, 1 + i), 1 if i < 3 else 0) for i in range(15)]
        as_of = SESSIONS[SESSIONS.index(start) + 59]
        met = paper_checklist(as_of=as_of, f1_cycles=[], f2_start=start, f2_closes=closes,
                              sessions=SESSIONS, violations=[])  # fmt: skip
        assert (met.f2.sessions, met.f2.closed, met.f2.rolls) == (60, 15, 3) and met.f2_met
        early = paper_checklist(as_of=SESSIONS[SESSIONS.index(start) + 58], f1_cycles=[],
                                f2_start=start, f2_closes=closes, sessions=SESSIONS,
                                violations=[])  # fmt: skip
        assert early.f2.sessions == 59 and not early.f2_met
        two_rolls = [F2Close(c.closed_on, 1 if i < 2 else 0) for i, c in enumerate(closes)]
        assert not paper_checklist(as_of=as_of, f1_cycles=[], f2_start=start, f2_closes=two_rolls,
                                   sessions=SESSIONS, violations=[]).f2_met  # fmt: skip
        naked = Violation(ViolationKind.NAKED_FUTURE, FoSleeve.F2, dt.date(2026, 2, 10), "p", "x")
        after = paper_checklist(as_of=as_of, f1_cycles=[], f2_start=start, f2_closes=closes,
                                sessions=SESSIONS, violations=[naked])  # fmt: skip
        assert after.f2.since == dt.date(2026, 2, 11) and not after.f2_met
        assert after.violations_by_kind() == {"NAKED_FUTURE": 1}

    def test_uncovered_steps_replay_every_fill(self) -> None:
        on = dt.date(2026, 7, 1)
        longs_first = [
            LegFill("LONG_PUT", Side.BUY, 65, "p", on), LegFill("LONG_CALL", Side.BUY, 65, "p", on),
            LegFill("SHORT_PUT", Side.SELL, 65, "p", on),
            LegFill("SHORT_CALL", Side.SELL, 65, "p", on),
            LegFill("SHORT_CALL", Side.BUY, 65, "x", on),
            LegFill("SHORT_PUT", Side.BUY, 65, "x", on),
            LegFill("LONG_CALL", Side.SELL, 65, "x", on),
            LegFill("LONG_PUT", Side.SELL, 65, "x", on),
        ]  # fmt: skip
        assert uncovered_steps(FoSleeve.F1N, longs_first) == []
        short_first = [LegFill("SHORT_CALL", Side.SELL, 65, "p", on),
                       LegFill("LONG_CALL", Side.BUY, 65, "p", on)]  # fmt: skip
        found = uncovered_steps(FoSleeve.F1N, short_first)
        assert [v.kind for v in found] == [ViolationKind.UNCOVERED_SHORT]
        long_out_first = [*longs_first[:4], LegFill("LONG_PUT", Side.SELL, 65, "x", on)]
        assert len(uncovered_steps(FoSleeve.F1N, long_out_first)) == 1

    def test_into_expiry_and_journal_gaps(self) -> None:
        exp = dt.date(2026, 11, 24)
        assert into_expiry(sleeve=FoSleeve.F1N, ref="1", expiry=exp,
                           closed_on=dt.date(2026, 11, 23), as_of=exp) is None  # fmt: skip
        held = into_expiry(sleeve=FoSleeve.F1N, ref="1", expiry=exp, closed_on=exp, as_of=exp)
        assert held is not None and held.kind is ViolationKind.INTO_EXPIRY
        still_open = into_expiry(sleeve=FoSleeve.F2, ref="2", expiry=exp, closed_on=None,
                                 as_of=exp)  # fmt: skip
        assert still_open is not None
        assert journal_gap(sleeve=FoSleeve.F2, ref="2", closed_on=exp, journalled=True) is None
        gap = journal_gap(sleeve=FoSleeve.F2, ref="2", closed_on=exp, journalled=False)
        assert gap is not None and gap.kind is ViolationKind.JOURNAL_GAP
        days = [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)]
        missing = mark_gaps(sleeve=FoSleeve.F2, ref="2", held_sessions=days, marked=days[::2])
        assert [v.on for v in missing] == [dt.date(2026, 11, 3)]
