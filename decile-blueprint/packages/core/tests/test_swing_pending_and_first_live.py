"""SW10.5 — STANDING-ANSWERS A7 (`PENDING_RANGE` lines), A8 (the marketable limit) and A9
(half risk at plan time) in the pure core, asserted against `docs/swing/04` §5, §7 and §9.

A7: a live gap has a trigger and no stop until the opening range sets one. The MORNING plan
shows it as a `PENDING_RANGE` line — no quantity, no stop, not executable, a preview at a 1-ADR
stop — and it **reserves one of the session's new-entry slots** so a lower-scored flag cannot
crowd it out; it never counts against the rung's position count, because it holds nothing.

A9: `risk_multiplier` scales `risk_per_trade_pct` before `size_position` (never a quantity at
send time), only while the first live sessions are counting down **and** execution is enabled;
SELL / RAISE lines are never touched.

A8: the live buy is a marketable LIMIT at `min(trigger x 1.005, range_high + 0.25 x ADR)`,
snapped down to the tick.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SizingConfig
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.plan import (
    EXECUTABLE_KINDS,
    LineKind,
    PlanLine,
    Skipped,
    SkipReason,
    SwingAccount,
    WatchItem,
    assemble,
    build_entries,
    exit_lines,
    first_live_multiplier,
    market_protection_pct,
    marketable_limit,
    sizing_at,
)
from baskfy_core.swing.stops import Action, ActionKind, ActionReason

D = Decimal
ONE = Decimal(1)
AS_OF = dt.date(2026, 9, 2)
TIER = ExposureTier(1, 4, 50.0, new_entries_allowed=True)
ACCOUNT = SwingAccount(D("1000000"), D("1000000"), frozenset(), D("0"))
CONFIG = DEFAULT_SWING_CONFIG


def item(  # noqa: PLR0913 - one keyword per watch field
    symbol: str,
    *,
    score: str = "70",
    setup: Setup = Setup.FLAG,
    trigger: str = "100",
    stop: str | None = "96",
    adr: str = "5",
    locked: bool = False,
) -> WatchItem:
    return WatchItem(
        symbol,
        setup,
        D(trigger),
        None if stop is None else D(stop),
        D(adr),
        D("300000000"),
        D(score),
        locked,
    )


def gap(
    symbol: str = "GAP", *, score: str = "80", adr: str = "5", locked: bool = False
) -> WatchItem:
    """A live gap as `watch_live_gaps` writes it: an EP, a trigger, no stop."""
    return item(
        symbol, score=score, setup=Setup.EP, trigger="112.50", stop=None, adr=adr, locked=locked
    )


def entries(  # noqa: PLR0913 - one keyword per plan input
    watch: list[WatchItem],
    *,
    account: SwingAccount = ACCOUNT,
    gate: MarketGate = MarketGate.GREEN,
    tier: ExposureTier = TIER,
    already: int = 0,
    multiplier: Decimal = ONE,
) -> tuple[list[PlanLine], list[Skipped]]:
    return build_entries(
        as_of=AS_OF,
        watch=watch,
        account=account,
        gate=gate,
        tier=tier,
        config=CONFIG,
        entries_already_today=already,
        risk_multiplier=multiplier,
    )


# --- A7: PENDING_RANGE -----------------------------------------------------------------


class TestPendingRange:
    def test_a_live_gap_is_a_pending_range_line_with_no_quantity_and_no_stop(self) -> None:
        lines, skipped = entries([gap()])
        assert skipped == []
        (line,) = lines
        assert line.kind is LineKind.PENDING_RANGE
        assert line.setup is Setup.EP
        assert (line.quantity, line.stop, line.risk_inr, line.position_value) == (
            0,
            None,
            D(0),
            D(0),
        )
        assert line.trigger == D("112.50")
        assert "slot reserved" in line.note

    def test_the_pending_range_preview_is_the_size_at_a_one_adr_stop(self) -> None:
        """Information only: 1 ADR of 5% under 112.50 is 106.875, snapped to the tick 106.90 —
        a 5.60 stop distance; 0.5% of ₹10 lakh is ₹5,000 of risk → 892 shares. The number is
        in the note, never in `quantity`."""
        (line,), _ = entries([gap(adr="5")])
        assert "≈ 892 shares" in line.note
        assert "1 ADR (5.00%)" in line.note
        assert line.quantity == 0

    def test_a_pending_range_line_with_no_adr_says_so_instead_of_guessing(self) -> None:
        (line,), _ = entries([gap(adr="0")])
        assert "ADR unknown" in line.note

    def test_a_locked_live_gap_is_shown_pending_with_the_lock_noted(self) -> None:
        """MD10's own words: "locked-circuit: no fill". The gap stays on the plan — it can
        unlock during the session — and its slot is still reserved."""
        (line,), skipped = entries([gap(locked=True)])
        assert skipped == []
        assert line.kind is LineKind.PENDING_RANGE
        assert "locked at the upper band" in line.note

    def test_pending_range_is_never_an_executable_kind(self) -> None:
        assert LineKind.PENDING_RANGE not in EXECUTABLE_KINDS
        assert {
            LineKind.BUY_ON_TRIGGER,
            LineKind.SELL_AT_OPEN,
            LineKind.RAISE_GTT_STOP,
        } == EXECUTABLE_KINDS

    def test_a_pending_range_line_reserves_one_of_the_sessions_new_entry_slots(self) -> None:
        """`04` §5.3's three a session: a gap scoring above two flags takes the first slot, the
        flags the next two, and the fourth name is `SESSION_CAP` — the gap crowded it out."""
        watch = [
            item("F1", score="70"),
            item("F2", score="65"),
            item("F3", score="60"),
            gap(score="80"),
        ]
        lines, skipped = entries(watch)
        assert [(line.symbol, line.kind) for line in lines] == [
            ("GAP", LineKind.PENDING_RANGE),
            ("F1", LineKind.BUY_ON_TRIGGER),
            ("F2", LineKind.BUY_ON_TRIGGER),
        ]
        assert [(s.symbol, s.reason) for s in skipped] == [("F3", SkipReason.SESSION_CAP)]

    def test_a_reserved_slot_counts_with_entries_already_taken(self) -> None:
        """The monitor's SIGNAL plan passes today's entries; a reserved slot plus two taken
        entries is the whole session, and the next trigger is `SESSION_CAP`."""
        lines, skipped = entries([gap(score="80"), item("F1", score="70")], already=2)
        assert [line.kind for line in lines] == [LineKind.PENDING_RANGE]
        assert [(s.symbol, s.reason) for s in skipped] == [("F1", SkipReason.SESSION_CAP)]

    def test_a_pending_range_line_does_not_count_against_the_rungs_position_count(self) -> None:
        """Rung 0 allows two positions; a reserved slot holds nothing, so two flags are still
        lined beside the gap. The session cap is what the reservation spends."""
        rung0 = ExposureTier(0, 2, 25.0, new_entries_allowed=True)
        lines, skipped = entries(
            [gap(score="80"), item("F1", score="70"), item("F2", score="65")], tier=rung0
        )
        assert [line.kind for line in lines] == [
            LineKind.PENDING_RANGE,
            LineKind.BUY_ON_TRIGGER,
            LineKind.BUY_ON_TRIGGER,
        ]
        assert skipped == []

    def test_a_pending_range_line_is_reserved_even_when_the_rung_is_full_of_planned_buys(
        self,
    ) -> None:
        """Rung 0 allows two positions and two flags are lined; the gap still reserves its
        session slot — it holds no position, and one of the two buys may never trigger. The
        SIGNAL plan at range close is where TIER_FULL is answered, against the book then."""
        rung0 = ExposureTier(0, 2, 25.0, new_entries_allowed=True)
        lines, skipped = entries(
            [item("F1", score="90"), item("F2", score="85"), gap(score="60")], tier=rung0
        )
        assert [(line.symbol, line.kind) for line in lines] == [
            ("F1", LineKind.BUY_ON_TRIGGER),
            ("F2", LineKind.BUY_ON_TRIGGER),
            ("GAP", LineKind.PENDING_RANGE),
        ]
        assert skipped == []

    def test_a_pending_range_line_spends_no_cash_and_no_exposure(self) -> None:
        tight = SwingAccount(D("1000000"), D("125000"), frozenset(), D("0"))
        lines, _ = entries([gap(score="80"), item("F1", score="70")], account=tight)
        assert lines[0].kind is LineKind.PENDING_RANGE
        assert lines[1].quantity == 1250, "the flag still has the whole ₹1.25 lakh"

    def test_a_live_gap_is_still_refused_by_the_gate_and_the_lockout_and_a_held_name(self) -> None:
        _, red = entries([gap()], gate=MarketGate.RED)
        assert [s.reason for s in red] == [SkipReason.GATE_RED]
        locked = replace(TIER, drawdown_locked=True)
        _, dd = entries([gap()], tier=locked)
        assert [s.reason for s in dd] == [SkipReason.DRAWDOWN_LOCKOUT]
        held = replace(ACCOUNT, open_symbols=frozenset({"GAP"}))
        _, already = entries([gap()], account=held)
        assert [s.reason for s in already] == [SkipReason.ALREADY_HELD]

    def test_the_signal_plan_makes_it_real_with_stop_min_range_low_lod_or_skips_it_wide(
        self,
    ) -> None:
        """The same watch item, once the range has set a stop: inside 1 ADR it is a BUY line;
        wider than 1 ADR it is `SIZE_REFUSED / STOP_TOO_WIDE` and the slot is free again (the
        skip is a skip, not a line, so nothing is reserved)."""
        inside = item("GAP", setup=Setup.EP, trigger="112.50", stop="107.50", adr="5")
        lines, skipped = entries([inside])
        assert lines[0].kind is LineKind.BUY_ON_TRIGGER and lines[0].stop == D("107.50")
        wide = item("GAP", setup=Setup.EP, trigger="112.50", stop="105.00", adr="5")
        lines, skipped = entries([wide, item("F1", score="60")])
        assert [line.symbol for line in lines] == ["F1"]
        assert [(s.symbol, s.reason, s.detail) for s in skipped] == [
            ("GAP", SkipReason.SIZE_REFUSED, "STOP_TOO_WIDE")
        ]

    def test_assemble_keeps_pending_lines_out_of_the_totals(self) -> None:
        lines, skipped = entries([gap(score="80"), item("F1", score="70")])
        plan = assemble(
            as_of=AS_OF, gate=MarketGate.GREEN, tier=TIER, entries=lines, exits=[], skipped=skipped
        )
        assert plan.total_risk_inr == D("5000.00")
        assert plan.total_new_exposure_inr == D("125000.00")
        assert (
            plan.plan_hash()
            == assemble(
                as_of=AS_OF,
                gate=MarketGate.GREEN,
                tier=TIER,
                entries=lines,
                exits=[],
                skipped=skipped,
            ).plan_hash()
        )

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(st.booleans(), st.integers(0, 100), st.booleans()),
            min_size=1,
            max_size=8,
        ),
        st.integers(0, 3),
    )
    def test_pending_range_lines_never_carry_a_quantity_a_stop_or_cash_over_any_watchlist(
        self, shapes: list[tuple[bool, int, bool]], already: int
    ) -> None:
        watch = [
            gap(f"G{i}", score=str(score), locked=locked)
            if pending
            else item(f"F{i}", score=str(score), locked=locked)
            for i, (pending, score, locked) in enumerate(shapes)
        ]
        lines, skipped = entries(watch, already=already)
        cap = CONFIG.sizing.max_new_entries_per_session
        assert already + len(lines) <= cap or len(lines) == 0 or already >= cap
        for line in lines:
            if line.kind is LineKind.PENDING_RANGE:
                assert line.quantity == 0 and line.stop is None
                assert line.risk_inr == 0 and line.position_value == 0
                assert line.kind not in EXECUTABLE_KINDS
            else:
                assert line.quantity > 0 and line.stop is not None
        answered = {line.symbol for line in lines} | {s.symbol for s in skipped}
        assert answered == {w.symbol for w in watch}


# --- A9: half risk at plan time ------------------------------------------------------------


class TestRiskMultiplier:
    def test_first_live_multiplier_is_half_only_while_counting_down_and_live(self) -> None:
        sizing = CONFIG.sizing
        assert first_live_multiplier(sessions_left=5, execution_enabled=True, config=sizing) == D(
            "0.5"
        )
        assert first_live_multiplier(sessions_left=1, execution_enabled=True, config=sizing) == D(
            "0.5"
        )
        assert first_live_multiplier(sessions_left=0, execution_enabled=True, config=sizing) == D(1)
        assert first_live_multiplier(sessions_left=5, execution_enabled=False, config=sizing) == D(
            1
        ), "a paper plan is full size (A9)"
        assert CONFIG.sizing.first_live_sessions == 5

    def test_sizing_at_scales_the_risk_and_nothing_else(self) -> None:
        scaled = sizing_at(SizingConfig(risk_per_trade_pct=0.5), D("0.5"))
        assert scaled.risk_per_trade_pct == pytest.approx(0.25)
        assert scaled.max_position_pct == SizingConfig().max_position_pct
        assert sizing_at(SizingConfig(), D(1)) == SizingConfig()

    def test_risk_multiplier_halves_the_line_at_plan_time(self) -> None:
        """0.5% of ₹10 lakh over a ₹4 stop is 1,250 shares; at half risk it is 625, and the
        rupees at risk halve with it. The line *is* the size — nothing halves it later."""
        (full,), _ = entries([item("A")])
        (half,), _ = entries([item("A")], multiplier=D("0.5"))
        assert (full.quantity, full.risk_inr) == (1250, D("5000.00"))
        assert (half.quantity, half.risk_inr) == (625, D("2500.00"))
        assert half.position_value == D("62500.00")

    def test_risk_multiplier_default_is_one_and_the_plan_is_unchanged(self) -> None:
        watch = [item("A"), item("B", score="60"), item("C", score="50")]
        assert entries(watch) == entries(watch, multiplier=D(1))

    def test_the_first_live_preview_is_at_the_same_half_risk(self) -> None:
        (full,), _ = entries([gap()])
        (half,), _ = entries([gap()], multiplier=D("0.5"))
        assert "≈ 892 shares" in full.note and "≈ 446 shares" in half.note

    def test_sell_and_raise_lines_are_identical_with_and_without_the_multiplier(self) -> None:
        """A9: only new entries start small. The exit lines come from `manage`'s actions and
        never see a risk budget; the same actions produce the same lines whatever the plan's
        multiplier — asserted by building both plans and comparing the exits byte for byte."""
        actions = [
            Action(ActionKind.SELL_PARTIAL, ActionReason.PARTIAL_INTO_STRENGTH, 100),
            Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AFTER_PARTIAL, new_stop=D("100")),
        ]
        exits = exit_lines("HELD", actions)
        full_lines, full_skips = entries([item("A")])
        half_lines, half_skips = entries([item("A")], multiplier=D("0.5"))
        full = assemble(
            as_of=AS_OF,
            gate=MarketGate.GREEN,
            tier=TIER,
            entries=full_lines,
            exits=exits,
            skipped=full_skips,
        )
        half = assemble(
            as_of=AS_OF,
            gate=MarketGate.GREEN,
            tier=TIER,
            entries=half_lines,
            exits=exits,
            skipped=half_skips,
        )
        assert full.lines[:2] == half.lines[:2] == tuple(exits)
        assert full.lines[0].quantity == half.lines[0].quantity == 100
        assert full.lines[1].stop == half.lines[1].stop == D("100.00")
        assert full.lines[2].quantity == 2 * half.lines[2].quantity

    @settings(max_examples=200, deadline=None)
    @given(st.decimals(min_value="0.1", max_value="1", places=2), st.integers(1, 5))
    def test_risk_scales_linearly_and_every_refusal_still_applies(
        self, multiplier: Decimal, count: int
    ) -> None:
        watch = [item(f"F{i}", score=str(90 - i)) for i in range(count)]
        full_lines, full_skips = entries(watch)
        scaled_lines, scaled_skips = entries(watch, multiplier=multiplier)
        assert [s.reason for s in full_skips] == [s.reason for s in scaled_skips]
        for full, scaled in zip(full_lines, scaled_lines, strict=True):
            assert scaled.quantity <= full.quantity
            assert scaled.risk_inr <= full.risk_inr
            budget = ACCOUNT.equity * D(str(CONFIG.sizing.risk_per_trade_pct)) / 100 * multiplier
            assert scaled.risk_inr <= budget


# --- A8: the marketable limit ------------------------------------------------------------


class TestMarketableLimit:
    def test_the_marketable_limit_is_the_lower_of_the_chase_and_the_range_reach(self) -> None:
        """Trigger 100.80 on a range high of 100.00 with a 5% ADR: the chase cap is 101.30
        (x 1.005 → 101.304, snapped down), the range reach is 100.00 + 0.25 x 5.04 = 101.26 →
        101.25. The lower wins, and it is a LIMIT — the caller never sends MARKET."""
        limit = marketable_limit(
            trigger=D("100.80"), range_high=D("100.00"), adr_pct=D("5"), config=CONFIG.opening_range
        )
        assert limit == D("101.25")

    def test_the_chase_cap_binds_when_the_range_is_far_below(self) -> None:
        limit = marketable_limit(
            trigger=D("100.00"), range_high=D("99.90"), adr_pct=D("10"), config=CONFIG.opening_range
        )
        assert limit == D("100.50")

    def test_without_a_range_the_trigger_is_the_range_high(self) -> None:
        with_range = marketable_limit(
            trigger=D("210.50"), range_high=D("210.50"), adr_pct=D("4"), config=CONFIG.opening_range
        )
        without = marketable_limit(
            trigger=D("210.50"), range_high=None, adr_pct=D("4"), config=CONFIG.opening_range
        )
        assert with_range == without

    def test_the_limit_is_snapped_down_to_the_tick_and_never_above_either_cap(self) -> None:
        limit = marketable_limit(
            trigger=D("57.37"), range_high=D("57.00"), adr_pct=D("6.3"), config=CONFIG.opening_range
        )
        assert limit % D("0.05") == 0
        assert limit <= D("57.37") * D("1.005")
        assert limit <= D("57.00") + D("0.25") * D("6.3") / 100 * D("57.37")

    def test_the_two_marketable_numbers_are_config_fields(self) -> None:
        assert CONFIG.opening_range.entry_limit_buffer_pct == 0.5
        assert CONFIG.opening_range.entry_limit_max_adr == 0.25
        assert CONFIG.opening_range.fill_poll_seconds == 10.0
        assert CONFIG.opening_range.fill_poll_interval_seconds == 0.5


class TestMarketProtection:
    """4 Sep 2026: the entry cap is A8's, but it reaches the exchange as a percentage.

    `marketable_limit` is unchanged and still says the most a setup is worth paying.
    `market_protection_pct` turns that ceiling into the number Kite wants on a MARKET order,
    and — the case that matters — returns ``None`` when there is no room left at all.
    """

    CFG = DEFAULT_SWING_CONFIG.opening_range

    def test_the_percentage_is_the_room_between_the_live_price_and_the_cap(self) -> None:
        # 101.25 cap, 100.80 live: (101.25 / 100.80 - 1) x 100 = 0.4464…, truncated to 0.44.
        pct = market_protection_pct(
            cap=Decimal("101.25"), last_price=Decimal("100.80"), config=self.CFG
        )
        assert pct == Decimal("0.44")

    def test_a_price_at_or_above_the_cap_refuses_rather_than_protecting_at_zero(self) -> None:
        """The whole point. A zero-width protection is not a refusal — it is an order that
        may still fill at the cap. `None` is the caller's instruction not to send one."""
        for price in (Decimal("101.25"), Decimal("101.30"), Decimal("140.00")):
            assert (
                market_protection_pct(cap=Decimal("101.25"), last_price=price, config=self.CFG)
                is None
            )

    def test_a_nonsense_price_refuses_too(self) -> None:
        for price in (Decimal("0"), Decimal("-5")):
            assert (
                market_protection_pct(cap=Decimal("101.25"), last_price=price, config=self.CFG)
                is None
            )

    def test_it_never_hands_the_exchange_more_than_the_configured_maximum(self) -> None:
        """A cap far above the price (a very wide ADR on a cheap name) must not become a
        licence to fill anywhere. 3% is the most this strategy ever sends."""
        pct = market_protection_pct(
            cap=Decimal("500.00"), last_price=Decimal("100.00"), config=self.CFG
        )
        assert pct == Decimal(str(self.CFG.market_protection_max_pct))

    def test_a_sliver_of_room_is_raised_to_the_floor_kite_accepts(self) -> None:
        """Kite wants a number greater than zero. A cap 0.001% away would round to 0.00 and
        be rejected as malformed; the floor is what keeps it a legal order."""
        pct = market_protection_pct(
            cap=Decimal("100.001"), last_price=Decimal("100.00"), config=self.CFG
        )
        assert pct == Decimal(str(self.CFG.market_protection_floor_pct))
        assert pct > 0

    def test_the_cap_it_reads_is_still_a8s_arithmetic(self) -> None:
        """The two functions compose: nothing about the entry ceiling moved on 4 Sep."""
        cap = marketable_limit(
            trigger=Decimal("100.80"),
            range_high=Decimal("100.00"),
            adr_pct=Decimal("5"),
            config=self.CFG,
        )
        assert cap == Decimal("101.25")
        assert market_protection_pct(
            cap=cap, last_price=Decimal("100.80"), config=self.CFG
        ) == Decimal("0.44")
