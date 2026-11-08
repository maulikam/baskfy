"""SW10: the Track-C claims about the pure core, as theorems over random inputs.

`docs/swing/06` SW10: "the Track-B and Track-C claims are theorems, not intentions." Three of
those claims are about functions in `baskfy_core.swing`, and each is asserted here over random
watchlists, accounts, positions and bars rather than over a hand-picked example:

* `docs/swing/02` Track C §1 — "`PARABOLIC_SHORT` is never a plan line (`TRADEABLE_SETUPS`)".
  Over random watchlists mixing every setup, every score, every gate and every account state,
  `build_entries` never emits a line carrying it and always answers it with
  `NOT_TRADEABLE_SETUP` — before the gate, the book or the money are consulted (`04` §9.1).
* `docs/swing/04` §6.5 — "**A stop never falls:** `apply` takes `max(stop, new_stop)`." Over
  random sequences of actions (including a `RAISE_STOP` to a level *below* the resting stop)
  and over random walks of `manage` → `apply`, the stop is monotone.
* Track C §5 — "the swing sleeve never sells a holding it did not buy." Over the plan builder:
  `build_entries` never produces a `SELL`, never lines a held name, and never spends more than
  the sleeve has; `exit_lines` never asks for more shares than the position holds.

WHY HYPOTHESIS, AND WHY A FIXED SEED
------------------------------------
`docs/02-tech-stack-adr.md` locks pytest + hypothesis, and `test_swing_sizing.py` already uses
it. Each property runs 500 examples from a fixed seed (`SEED`), so a run is reproducible and a
failure is a specific example hypothesis prints in full, together with the seed the assertion
messages carry — not a "flaky" run somebody re-runs until it passes.

Prices are drawn on the exchange tick (multiples of ₹0.05), which is the domain the rules are
written for: `to_tick` is the identity on them, so a level the plan snaps is the level the rule
produced, and a test that drew arbitrary paise would be testing the snap rather than the rule.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    TRADEABLE_SETUPS,
    Setup,
    StopConfig,
    SwingConfig,
)
from baskfy_core.swing.market import ExposureTier, MarketGate
from baskfy_core.swing.plan import (
    LineKind,
    Skipped,
    SkipReason,
    SwingAccount,
    WatchItem,
    assemble,
    build_entries,
    exit_lines,
)
from baskfy_core.swing.stops import (
    Action,
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    TrailMa,
    apply,
    manage,
)

#: One seed for the whole file. Printed in every assertion message so a red run says how to
#: reproduce itself without reading the hypothesis banner.
SEED: Final = 20260902
EXAMPLES: Final = 500

AS_OF: Final = dt.date(2026, 9, 2)
CONFIG: Final[SwingConfig] = DEFAULT_SWING_CONFIG
STOPS: Final[StopConfig] = CONFIG.stops

_TICK: Final = Decimal("0.05")
_PCT: Final = Decimal(100)
_ZERO: Final = Decimal(0)

_SUITE = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _why(what: str) -> str:
    return f"{what} (hypothesis seed {SEED})"


# --- strategies ------------------------------------------------------------------------


def ticks(low: str, high: str) -> st.SearchStrategy[Decimal]:
    """A price on the exchange tick between ``low`` and ``high`` rupees."""
    lo = int(Decimal(low) / _TICK)
    hi = int(Decimal(high) / _TICK)
    return st.integers(min_value=lo, max_value=hi).map(lambda n: n * _TICK)


prices = ticks("20", "5000")
symbols = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=6)
pct = st.decimals(min_value=Decimal("0"), max_value=Decimal("100"), places=2)


def _snap(price: Decimal) -> Decimal:
    return (price / _TICK).to_integral_value() * _TICK


@st.composite
def watch_items(draw: st.DrawFn) -> WatchItem:
    """Any name the detectors or a person could put on the list — including bad ones.

    The stop may sit anywhere from 15% below the trigger to 2% *above* it: `size_position`
    refuses the wide and the inverted ones, and a plan built over them must still answer.
    """
    trigger = draw(prices)
    distance_pct = draw(st.decimals(min_value=Decimal("-2"), max_value=Decimal("15"), places=2))
    stop = _snap(trigger * (1 - distance_pct / _PCT))
    return WatchItem(
        symbol=draw(symbols),
        setup=draw(st.sampled_from(list(Setup))),
        trigger=trigger,
        stop_ref=stop,
        adr_pct=draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("15"), places=2)),
        avg_turnover_inr=draw(
            st.one_of(
                st.none(),
                st.decimals(
                    min_value=Decimal(1_000_000), max_value=Decimal(10_000_000_000), places=0
                ),
            )
        ),
        score=draw(pct),
        locked_upper_circuit=draw(st.booleans()),
    )


watchlists = st.lists(watch_items(), min_size=0, max_size=12, unique_by=lambda w: w.symbol)


@dataclass(frozen=True, slots=True)
class Scenario:
    """Everything `build_entries` is handed, drawn together so the parts can disagree."""

    watch: list[WatchItem]
    account: SwingAccount
    gate: MarketGate
    tier: ExposureTier


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    watch = draw(watchlists)
    equity = draw(st.decimals(min_value=Decimal(0), max_value=Decimal(50_000_000), places=0))
    cash = draw(st.decimals(min_value=Decimal(0), max_value=max(equity, _ZERO), places=0))
    held_from_watch = draw(st.lists(st.sampled_from([w.symbol for w in watch] or ["NONE"])))
    held_elsewhere = draw(st.lists(symbols, max_size=3))
    open_symbols = frozenset(held_from_watch + held_elsewhere) - {"NONE"}
    exposure = draw(
        st.decimals(min_value=Decimal(0), max_value=max(equity - cash, _ZERO), places=2)
    )
    account = SwingAccount(
        equity=equity,
        cash_available=cash,
        open_symbols=open_symbols,
        open_exposure_inr=exposure if open_symbols else _ZERO,
    )
    ladder = [
        ExposureTier(level, positions, exposure_pct, True)
        for level, (positions, exposure_pct) in enumerate(CONFIG.market.tiers)
    ]
    tier = draw(
        st.one_of(
            st.sampled_from(ladder),
            st.builds(
                ExposureTier,
                level=st.integers(min_value=0, max_value=3),
                max_open_positions=st.integers(min_value=0, max_value=8),
                max_exposure_pct=st.floats(min_value=0.0, max_value=100.0),
                new_entries_allowed=st.booleans(),
            ),
        )
    )
    return Scenario(
        watch=watch, account=account, gate=draw(st.sampled_from(list(MarketGate))), tier=tier
    )


@st.composite
def positions(draw: st.DrawFn) -> OpenPosition:
    """An open position as the book could hold it: stop between the initial stop and the entry."""
    entry = draw(prices)
    distance_pct = draw(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("12"), places=2))
    initial_stop = _snap(entry * (1 - distance_pct / _PCT))
    if initial_stop >= entry:
        initial_stop = entry - _TICK
    stop = _snap(draw(st.decimals(min_value=initial_stop, max_value=entry, places=2)))
    stop = min(max(stop, initial_stop), entry)
    return OpenPosition(
        symbol=draw(symbols),
        entry_date=AS_OF - dt.timedelta(days=draw(st.integers(min_value=0, max_value=30))),
        entry=entry,
        initial_stop=initial_stop,
        stop=stop,
        quantity=draw(st.integers(min_value=1, max_value=5000)),
        partial_done=draw(st.booleans()),
        trail=draw(st.sampled_from(list(TrailMa))),
        is_ep_gap_day=draw(st.booleans()),
    )


@st.composite
def action_lists(draw: st.DrawFn, position: OpenPosition) -> list[Action]:
    """Random actions — including a RAISE below the resting stop, which `apply` must refuse."""
    out: list[Action] = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        kind = draw(st.sampled_from(list(ActionKind)))
        reason = draw(st.sampled_from(list(ActionReason)))
        quantity = draw(st.integers(min_value=0, max_value=position.quantity))
        new_stop = draw(st.one_of(st.none(), ticks("1", "6000")))
        out.append(Action(kind=kind, reason=reason, quantity=quantity, new_stop=new_stop))
    return out


@st.composite
def bars_after(draw: st.DrawFn, position: OpenPosition) -> list[DailyBar]:
    """A random walk of daily bars from the entry day on, with optional averages."""
    bars: list[DailyBar] = []
    last = position.entry
    for index in range(draw(st.integers(min_value=1, max_value=12))):
        move = draw(st.decimals(min_value=Decimal("-20"), max_value=Decimal("30"), places=2))
        close = max(_snap(last * (1 + move / _PCT)), _TICK)
        open_ = max(
            _snap(
                last
                * (
                    1
                    + draw(st.decimals(min_value=Decimal("-5"), max_value=Decimal("5"), places=2))
                    / _PCT
                )
            ),
            _TICK,
        )
        wick = draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("8"), places=2))
        high = _snap(max(open_, close) * (1 + wick / _PCT))
        low = max(_snap(min(open_, close) * (1 - wick / _PCT)), _TICK)
        averages = st.one_of(st.none(), ticks("1", "6000"))
        bars.append(
            DailyBar(
                date=position.entry_date + dt.timedelta(days=index),
                open=open_,
                high=high,
                low=low,
                close=close,
                ma10=draw(averages),
                ma20=draw(averages),
                bars_since_entry=index,
            )
        )
        last = close
    return bars


# --- Track C §1: PARABOLIC_SHORT is never a plan line ------------------------------------


class TestNoParabolicShortBecomesAPlanLine:
    def test_parabolic_short_is_the_one_setup_outside_tradeable_setups(self) -> None:
        """The constant the whole claim rests on, asserted as the spec states it."""
        assert frozenset({Setup.FLAG, Setup.EP}) == TRADEABLE_SETUPS
        assert set(Setup) - TRADEABLE_SETUPS == {Setup.PARABOLIC_SHORT}

    @seed(SEED)
    @_SUITE
    @given(scenario=scenarios())
    def test_parabolic_short_never_becomes_a_line_over_random_watchlists(
        self, scenario: Scenario
    ) -> None:
        """Whatever the score, the gate, the tier or the money: never a line, always the
        `NOT_TRADEABLE_SETUP` skip, and that reason before any other (`04` §9.1)."""
        lines, skipped = build_entries(
            as_of=AS_OF,
            watch=scenario.watch,
            account=scenario.account,
            gate=scenario.gate,
            tier=scenario.tier,
            config=CONFIG,
        )
        for line in lines:
            assert line.setup in TRADEABLE_SETUPS, _why(f"{line} carries {line.setup}")
            assert line.setup is not Setup.PARABOLIC_SHORT, _why(f"{line} is a short")
        answered = {skip.symbol: skip for skip in skipped}
        for item in scenario.watch:
            if item.setup is not Setup.PARABOLIC_SHORT:
                continue
            assert item.symbol in answered, _why(f"{item.symbol} was neither lined nor skipped")
            assert answered[item.symbol].reason is SkipReason.NOT_TRADEABLE_SETUP, _why(
                f"{item.symbol} skipped for {answered[item.symbol].reason}, not the setup"
            )
        # Every watched name is answered exactly once, so a short cannot slip through as
        # "neither".
        assert len(lines) + len(skipped) == len(scenario.watch), _why("a name went unanswered")
        plan = assemble(
            as_of=AS_OF,
            gate=scenario.gate,
            tier=scenario.tier,
            entries=lines,
            exits=[],
            skipped=skipped,
        )
        assert all(line.setup is not Setup.PARABOLIC_SHORT for line in plan.lines), _why(
            "assemble let a short through"
        )

    def test_parabolic_short_alone_under_the_best_possible_conditions_is_still_skipped(
        self,
    ) -> None:
        """The strongest counter-example a person could construct: a perfect score, a rich
        sleeve, a GREEN gate, the top rung, a tight stop, deep turnover — and no line."""
        item = WatchItem(
            symbol="RUNNER",
            setup=Setup.PARABOLIC_SHORT,
            trigger=Decimal("100.00"),
            stop_ref=Decimal("97.00"),
            adr_pct=Decimal("8.00"),
            avg_turnover_inr=Decimal(1_000_000_000),
            score=Decimal("100.00"),
            locked_upper_circuit=False,
        )
        account = SwingAccount(
            equity=Decimal(10_000_000),
            cash_available=Decimal(10_000_000),
            open_symbols=frozenset(),
            open_exposure_inr=_ZERO,
        )
        lines, skipped = build_entries(
            as_of=AS_OF,
            watch=[item],
            account=account,
            gate=MarketGate.GREEN,
            tier=ExposureTier(3, 8, 100.0, True),
            config=CONFIG,
        )
        assert lines == []
        assert skipped == [Skipped("RUNNER", SkipReason.NOT_TRADEABLE_SETUP, "PARABOLIC_SHORT")]


# --- 04 §6.5: a stop never falls --------------------------------------------------------


class TestAStopNeverFalls:
    @seed(SEED)
    @_SUITE
    @given(data=st.data())
    def test_stop_never_falls_through_apply_over_random_action_sequences(
        self, data: st.DataObject
    ) -> None:
        """`apply` takes `max(stop, new_stop)`: the stop after any sequence of actions is at
        least the stop before, at least every level a RAISE asked for, and never a level
        nobody named."""
        position = data.draw(positions())
        actions = data.draw(action_lists(position))
        after = apply(position, actions)
        assert after.stop >= position.stop, _why(f"stop fell {position.stop} → {after.stop}")
        raised_to = [
            action.new_stop
            for action in actions
            if action.kind is ActionKind.RAISE_STOP and action.new_stop is not None
        ]
        for level in raised_to:
            assert after.stop >= level, _why(f"a RAISE to {level} left the stop at {after.stop}")
        assert after.stop in {position.stop, *raised_to}, _why("the stop was invented")
        assert after.stop >= after.initial_stop
        assert after.entry == position.entry and after.initial_stop == position.initial_stop
        # Re-applying the same raises moves nothing: the rule is idempotent.
        again = apply(after, [a for a in actions if a.kind is ActionKind.RAISE_STOP])
        assert again.stop == after.stop, _why("a second apply of the same raises moved the stop")

    @seed(SEED)
    @_SUITE
    @given(data=st.data())
    def test_stop_never_falls_across_a_random_walk_of_manage_and_apply(
        self, data: st.DataObject
    ) -> None:
        """Day after day: every RAISE the rules emit is to the entry and above the resting
        stop, and the stop the book carries is monotone until the position is gone."""
        position = data.draw(positions())
        for bar in data.draw(bars_after(position)):
            actions = manage(position, bar, STOPS)
            assert actions, _why("manage answered nothing — `04` §6.4.6 says HOLD explicitly")
            for action in actions:
                if action.kind is ActionKind.RAISE_STOP:
                    assert action.new_stop == position.entry, _why(
                        f"a RAISE to {action.new_stop} is not a breakeven raise"
                    )
                    assert action.new_stop > position.stop, _why(
                        f"a RAISE to {action.new_stop} would not raise {position.stop}"
                    )
            after = apply(position, actions)
            assert after.stop >= position.stop, _why(f"{position.stop} → {after.stop} on {bar}")
            position = after
            if position.quantity == 0:
                break


# --- Track C §5: the sleeve never sells what it does not own ----------------------------


class TestASellNeverExceedsWhatTheSleeveOwns:
    @seed(SEED)
    @_SUITE
    @given(data=st.data())
    def test_a_sell_line_never_exceeds_what_the_sleeve_owns(self, data: st.DataObject) -> None:
        """Over `manage` → `exit_lines`: every SELL is for shares the position holds, a partial
        is strictly less than the whole, a close-out is exactly the whole, and the book after
        `apply` never goes negative."""
        position = data.draw(positions())
        bar = data.draw(bars_after(position))[0]
        actions = manage(position, bar, STOPS)
        lines = exit_lines(position.symbol, actions)
        sold = 0
        for line in lines:
            if line.kind is LineKind.SELL_AT_OPEN:
                assert 0 < line.quantity <= position.quantity, _why(
                    f"SELL {line.quantity} against {position.quantity} held"
                )
                sold += line.quantity
            else:
                assert line.kind is LineKind.RAISE_GTT_STOP and line.quantity == 0
                assert line.stop is not None and line.stop >= position.stop, _why(
                    f"a RAISE line to {line.stop} sits below the resting {position.stop}"
                )
        assert sold <= position.quantity, _why(f"{sold} sold of {position.quantity}")
        for action in actions:
            if action.kind is ActionKind.SELL_PARTIAL:
                assert 0 < action.quantity < position.quantity, _why("a partial is not partial")
            if action.kind in (ActionKind.SELL_ALL, ActionKind.STOPPED_OUT):
                assert action.quantity == position.quantity
        assert apply(position, actions).quantity >= 0, _why("the book went negative")

    @seed(SEED)
    @_SUITE
    @given(scenario=scenarios())
    def test_build_entries_never_emits_a_sell_and_never_buys_a_held_name(
        self, scenario: Scenario
    ) -> None:
        """The entry side has no SELL in its vocabulary, and a name the sleeve holds is never
        lined again (`04` §6.5: never averaged down; §9.1: `ALREADY_HELD`)."""
        lines, skipped = build_entries(
            as_of=AS_OF,
            watch=scenario.watch,
            account=scenario.account,
            gate=scenario.gate,
            tier=scenario.tier,
            config=CONFIG,
        )
        for line in lines:
            assert line.kind is LineKind.BUY_ON_TRIGGER, _why(f"{line.kind} from build_entries")
            assert line.quantity > 0, _why("a line for no shares")
            assert line.symbol not in scenario.account.open_symbols, _why(
                f"{line.symbol} is held and was lined again"
            )
        entries_open = scenario.gate is not MarketGate.RED and scenario.tier.new_entries_allowed
        answered = {skip.symbol: skip.reason for skip in skipped}
        for item in scenario.watch:
            held = item.symbol in scenario.account.open_symbols
            if held and item.setup in TRADEABLE_SETUPS and entries_open:
                assert answered.get(item.symbol) is SkipReason.ALREADY_HELD, _why(
                    f"{item.symbol} is held; skipped as {answered.get(item.symbol)}"
                )

    @seed(SEED)
    @_SUITE
    @given(scenario=scenarios())
    def test_entries_never_spend_more_than_the_sleeve_has(self, scenario: Scenario) -> None:
        """Track C §2 (exposure ≤ the sleeve) and §5 (never sized against the whole account):
        the lines' value fits in the cash, the tier's ceiling and the position count, and each
        line risks no more than the budget (`04` §5.2)."""
        lines, _ = build_entries(
            as_of=AS_OF,
            watch=scenario.watch,
            account=scenario.account,
            gate=scenario.gate,
            tier=scenario.tier,
            config=CONFIG,
        )
        account, tier = scenario.account, scenario.tier
        spent = sum((line.position_value for line in lines), _ZERO)
        assert spent <= account.cash_available, _why(f"spent {spent} of {account.cash_available}")
        ceiling = account.equity * Decimal(str(tier.max_exposure_pct)) / _PCT
        # A book can already sit above its ceiling (the ladder stepped down under it); the
        # claim is that no *new* line is what puts it there.
        if lines:
            assert account.open_exposure_inr + spent <= ceiling, _why(
                f"exposure {account.open_exposure_inr + spent} over the "
                f"{tier.max_exposure_pct}% ceiling"
            )
        room = max(tier.max_open_positions - len(account.open_symbols), 0)
        assert len(lines) <= room, _why(f"{len(lines)} lines with room for {room}")
        budget = account.equity * Decimal(str(CONFIG.sizing.risk_per_trade_pct)) / _PCT
        for line in lines:
            assert line.risk_inr <= budget, _why(f"{line.symbol} risks {line.risk_inr} > {budget}")
            assert line.trigger is not None and line.stop is not None
            assert line.stop < line.trigger, _why(f"{line.symbol} has a stop at or above entry")


class TestEntriesAlreadyTodayCountAgainstTheSessionCap:
    """`04` §5.3 (SW10.4, STANDING-ANSWERS A5): the per-session cap counts entries the session
    has already taken, not only the lines in this plan. The desk's confirm-time gate sizes one
    line at a time and hands `build_entries` the number confirmed earlier in the morning."""

    @seed(SEED)
    @_SUITE
    @given(scenario=scenarios(), already=st.integers(min_value=0, max_value=6))
    def test_lines_plus_entries_already_today_never_exceed_the_cap(
        self, scenario: Scenario, already: int
    ) -> None:
        cap = CONFIG.sizing.max_new_entries_per_session
        lines, skipped = build_entries(
            as_of=AS_OF,
            watch=scenario.watch,
            account=scenario.account,
            gate=scenario.gate,
            tier=scenario.tier,
            config=CONFIG,
            entries_already_today=already,
        )
        # What was taken cannot be untaken; the claim is that no line is ADDED past the cap.
        assert len(lines) <= max(cap - already, 0), _why(
            f"{already} taken today + {len(lines)} lined > the cap of {cap}"
        )
        if already >= cap:
            assert lines == [], _why(f"{already} entries today and still {len(lines)} lines")
            for item in scenario.watch:
                if item.setup not in TRADEABLE_SETUPS or scenario.tier.drawdown_locked:
                    continue
                if scenario.gate is MarketGate.RED or not scenario.tier.new_entries_allowed:
                    continue
                if item.symbol in scenario.account.open_symbols or item.locked_upper_circuit:
                    continue
                reasons = {s.reason for s in skipped if s.symbol == item.symbol}
                assert SkipReason.SESSION_CAP in reasons, _why(
                    f"{item.symbol} refused as {reasons}, not SESSION_CAP, with the session full"
                )

    def test_the_default_is_zero_and_the_plan_is_unchanged_by_it(self) -> None:
        """The evening and the morning pass nothing: `entries_already_today=0` is the same
        plan as before the argument existed."""
        watch = [
            WatchItem(
                symbol=f"N{i}",
                setup=Setup.FLAG,
                trigger=Decimal("100.00"),
                stop_ref=Decimal("97.00"),
                adr_pct=Decimal("5.00"),
                avg_turnover_inr=Decimal(100_000_000),
                score=Decimal(90 - i),
            )
            for i in range(5)
        ]
        account = SwingAccount(
            equity=Decimal(1_000_000),
            cash_available=Decimal(1_000_000),
            open_symbols=frozenset(),
            open_exposure_inr=_ZERO,
        )
        tier = ExposureTier(3, 10, 100.0, True)
        plain = build_entries(
            as_of=AS_OF,
            watch=watch,
            account=account,
            gate=MarketGate.GREEN,
            tier=tier,
            config=CONFIG,
        )
        explicit = build_entries(
            as_of=AS_OF,
            watch=watch,
            account=account,
            gate=MarketGate.GREEN,
            tier=tier,
            config=CONFIG,
            entries_already_today=0,
        )
        assert plain == explicit
        assert [line.symbol for line in plain[0]] == ["N0", "N1", "N2"]
        # two taken this morning: one more line, and the rest say why
        lines, skipped = build_entries(
            as_of=AS_OF,
            watch=watch,
            account=account,
            gate=MarketGate.GREEN,
            tier=tier,
            config=CONFIG,
            entries_already_today=2,
        )
        assert [line.symbol for line in lines] == ["N0"]
        assert [(s.symbol, s.reason) for s in skipped] == [
            (f"N{i}", SkipReason.SESSION_CAP) for i in range(1, 5)
        ]
        assert {s.detail for s in skipped} == {"3 new entries per session"}
