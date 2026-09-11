"""TW7 — ``docs/twt/04`` §7.4, the fill-day rule, boundary by boundary.

    "A position whose **entry session's own low** is at or below its initial stop is out that
     session (``STOP_DAY0``), filled at the stop, or at the open when the open was already below
     it."

**Why this file exists when ``test_twt_exits.py`` already has a fill-day class.** That class
asserts the rule; this one asserts its *edges*, and the edges are where an off-by-one costs real
money. A stop is floored to the tick (``04`` §7.1), so the level a live GTT rests at is not
``fill x 0.8`` — it is the tick below it, and a bar that misses that level by one tick is a
position the book still owns. Each case below gets its own fixture with its own fill price, so
that a fixture edited for one case cannot quietly move another.

The three the module plan names, plus the one that proves they are edges at all:

* an **open already below** the stop fills at the open — the market never offered the stop;
* a bar that **touches exactly** the stop fills at the stop — ``<=``, not ``<``;
* a bar that **misses by one tick** does not close — the rule is a level, not a neighbourhood;
* and the session low that breaches it closes **that session**, reason ``STOP_DAY0``, at the stop,
  read both through :func:`~baskfy_core.twt.exits.fill_day_stop` and through the backtest engine
  that TW2's goldens grade.

**The equivalence that is the whole of TW7** is asserted in :class:`TestTheRuleAndTheGttAreOne`:
the price a ``STOP_DAY0`` fills at in the backtest is, to the paisa, the trigger the live book's
same-session GTT is armed at (non-negotiable 4). They are one rule measured two ways, and a test
that lets them drift is a backtest describing a book nobody owns.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

import numpy as np
import pytest
from twt_fixtures import sessions

from baskfy_core.twt.backtest import (
    BacktestParams,
    BacktestResult,
    BacktestTrade,
    Panel,
    run_backtest,
)
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TICK_INR
from baskfy_core.twt.exits import (
    Action,
    Bar,
    ExitReason,
    OpenPosition,
    fill_day_stop,
    initial_stop,
    manage,
    tick,
)

EXITS: Final = DEFAULT_TWT_CONFIG.exits
#: NSE cash equities' own tick. The fill-day boundary is a tick wide, so the sleeve's tick — not
#: the study's paisa — is the one these cases are written against.
TICK: Final = tick()
ENTRY_DAY: Final = dt.date(2024, 6, 3)
NEXT_DAY: Final = dt.date(2024, 6, 4)

#: A fill that is **not** a round number, on purpose. ``247.35 x 0.8 = 197.88`` floors to
#: ``197.85``, so the stop is three paisa below the arithmetic and every boundary below is a
#: statement about the floored level rather than about the multiplication.
AWKWARD_FILL: Final = Decimal("247.35")
AWKWARD_STOP: Final = Decimal("197.85")

#: A turnover so large that ``04`` §6.2's 1 %-of-turnover cap can never be the binding one.
DEEP: Final = 1e12


def position(*, fill: Decimal, stop: Decimal, entry: dt.date = ENTRY_DAY) -> OpenPosition:
    """One line of the book, as :mod:`baskfy_core.twt.exits` wants it."""
    return OpenPosition(
        instrument_id=1,
        entry_date=entry,
        fill_price=fill,
        quantity=100,
        stop_price=stop,
        initial_stop=stop,
        high_since=fill,
    )


def bar(
    *,
    session: dt.date = ENTRY_DAY,
    open_: Decimal | None,
    high: Decimal | None = None,
    low: Decimal | None,
    close: Decimal | None = None,
) -> Bar:
    """One session. ``high``/``close`` default to the open, which no case below reads."""
    return Bar(
        session=session,
        open=open_,
        high=open_ if high is None else high,
        low=low,
        close=open_ if close is None else close,
    )


class TestTheStopIsFlooredToTheTickBeforeAnyBoundaryIsRead:
    """The premise the three boundary cases are boundaries *of*.

    If the stop were ``fill x 0.8`` unfloored, "exactly" and "one tick out" would be statements
    about a level no exchange quotes.
    """

    def test_the_awkward_fills_stop_is_the_tick_below_the_arithmetic(self) -> None:
        exact = AWKWARD_FILL * (Decimal(1) - EXITS.stop_pct / Decimal(100))
        assert exact == Decimal("197.880")
        assert initial_stop(AWKWARD_FILL, EXITS, TICK) == AWKWARD_STOP
        assert exact > AWKWARD_STOP, "a stop is floored, never rounded up"

    def test_the_tick_these_cases_are_written_against_is_the_exchanges(self) -> None:
        assert TICK == Decimal(TICK_INR) == Decimal("0.05")


class TestTheSessionLowThroughTheStop:
    """``04`` §7.4's headline: out **that session**, reason ``STOP_DAY0``, at the stop."""

    def test_a_low_through_the_stop_closes_the_entry_session_at_the_stop(self) -> None:
        action = fill_day_stop(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(open_=Decimal("246.00"), low=Decimal("190.10"), close=Decimal("193.00")),
        )
        assert action is not None
        assert action.action is Action.STOPPED_OUT
        assert action.reason is ExitReason.STOP_DAY0
        assert action.price == AWKWARD_STOP, "the stop, not the low it traded through"

    def test_manage_reaches_the_fill_day_rule_before_the_ordinary_stop(self) -> None:
        """``manage``'s precedence, which is what the backtest and the desk both call.

        Without the fill-day branch the same bar would come back ``STOP_HIT`` — the same money
        and the wrong reason, and ``STOP_DAY0`` is the count TW2's goldens grade (2 of 164).
        """
        action = manage(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(open_=Decimal("246.00"), low=Decimal("190.10"), close=Decimal("193.00")),
            config=EXITS,
        )
        assert action.reason is ExitReason.STOP_DAY0

    def test_it_is_the_entry_session_and_no_other(self) -> None:
        """The same bar one session later is an ordinary ``STOP_HIT``, not a day zero."""
        later = bar(
            session=NEXT_DAY,
            open_=Decimal("246.00"),
            low=Decimal("190.10"),
            close=Decimal("193.00"),
        )
        line = position(fill=AWKWARD_FILL, stop=AWKWARD_STOP)
        assert fill_day_stop(line, later) is None
        assert manage(line, later, config=EXITS).reason is ExitReason.STOP_HIT


class TestTheOpenBelowTheStop:
    """Boundary one: the market never offered the stop, so the fill is the open."""

    def test_an_open_below_the_stop_fills_at_the_open_and_not_at_the_stop(self) -> None:
        gapped = bar(open_=Decimal("180.00"), low=Decimal("176.50"), close=Decimal("179.00"))
        action = fill_day_stop(position(fill=AWKWARD_FILL, stop=AWKWARD_STOP), gapped)
        assert action is not None
        assert action.reason is ExitReason.STOP_DAY0
        assert action.price == Decimal("180.00")
        assert action.price < AWKWARD_STOP, "a gap fills worse than the stop; that is the point"

    def test_an_open_exactly_at_the_stop_is_an_open_below_it(self) -> None:
        """``open <= stop``, not ``<``. A GTT resting at the stop fills at the open that printed
        there, and a rule that read ``<`` would book the same money at a price nobody traded."""
        action = fill_day_stop(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(open_=AWKWARD_STOP, low=Decimal("195.00"), close=Decimal("196.00")),
        )
        assert action is not None
        assert action.price == AWKWARD_STOP

    def test_an_open_one_tick_above_the_stop_fills_at_the_stop(self) -> None:
        """The other side of the same edge: the open was offered above the stop, so the stop is
        the price — which is what the resting GTT would have got."""
        action = fill_day_stop(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(open_=AWKWARD_STOP + TICK, low=Decimal("195.00"), close=Decimal("196.00")),
        )
        assert action is not None
        assert action.price == AWKWARD_STOP


class TestTheBarThatTouchesExactlyTheStop:
    """Boundary two: ``low <= stop``. Touching **is** breaching."""

    def test_a_low_exactly_at_the_stop_closes_the_session_at_the_stop(self) -> None:
        action = fill_day_stop(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(open_=Decimal("246.00"), low=AWKWARD_STOP, close=Decimal("240.00")),
        )
        assert action is not None
        assert action.reason is ExitReason.STOP_DAY0
        assert action.price == AWKWARD_STOP

    def test_a_low_exactly_at_the_stop_is_a_close_even_when_the_session_ends_far_above_it(
        self,
    ) -> None:
        """The bar recovered to the fill. It does not matter: a GTT that was touched has fired,
        and a backtest that waited for the close would be reporting trades nobody had."""
        action = fill_day_stop(
            position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
            bar(
                open_=Decimal("246.00"),
                high=Decimal("250.00"),
                low=AWKWARD_STOP,
                close=Decimal("249.00"),
            ),
        )
        assert action is not None
        assert action.price == AWKWARD_STOP


class TestTheBarThatMissesByOneTick:
    """Boundary three, and the expensive one: one tick above the stop is a position still owned."""

    def test_a_low_one_tick_above_the_stop_does_not_close(self) -> None:
        held = bar(open_=Decimal("246.00"), low=AWKWARD_STOP + TICK, close=Decimal("240.00"))
        line = position(fill=AWKWARD_FILL, stop=AWKWARD_STOP)
        assert fill_day_stop(line, held) is None
        assert manage(line, held, config=EXITS).action is Action.HOLD

    def test_the_tick_is_the_whole_difference_between_holding_and_not(self) -> None:
        """One fixture, one subtraction of a tick, two different outcomes. This is the assertion
        the module is for: nothing else about the two bars differs."""
        line = position(fill=AWKWARD_FILL, stop=AWKWARD_STOP)
        missed = bar(open_=Decimal("246.00"), low=AWKWARD_STOP + TICK, close=Decimal("240.00"))
        touched = bar(open_=Decimal("246.00"), low=AWKWARD_STOP, close=Decimal("240.00"))
        assert fill_day_stop(line, missed) is None
        assert fill_day_stop(line, touched) is not None

    def test_a_session_with_no_low_is_not_a_breach(self) -> None:
        """A name that did not print cannot have traded through anything. ``04`` §7.5's write-off
        is the rule for that, and it takes five sessions."""
        assert (
            fill_day_stop(
                position(fill=AWKWARD_FILL, stop=AWKWARD_STOP),
                Bar(session=ENTRY_DAY, open=None, high=None, low=None, close=None),
            )
            is None
        )


# ---------------------------------------------------------------------------
# The same rule, read through the engine TW2's goldens grade
# ---------------------------------------------------------------------------


def one_name(*, count: int = 6, price: float = 100.0, **edits: dict[int, float | None]) -> Panel:
    """One instrument, flat at ``price``, signalling on session 0 so it fills on session 1.

    The low is a hair under the open rather than equal to it: a bar with ``open == high == low``
    is a name locked at a circuit, which ``04`` §5.2 refuses to fill, and a flat fixture would
    silently test the refusal instead of the rule.
    """
    series: dict[str, list[float | None]] = {
        "open": [price] * count,
        "high": [price] * count,
        "low": [round(price * 0.99, 4)] * count,
        "close": [price] * count,
    }
    for field, changes in edits.items():
        for column, value in changes.items():
            series[field][column] = value

    def matrix(field: str) -> np.ndarray:
        row = [np.nan if value is None else value for value in series[field]]
        return np.array([row], dtype=float)

    signal = np.zeros((1, count), dtype=bool)
    signal[0, 0] = True
    return Panel(
        symbols=("AAA",),
        instrument_ids=(1,),
        sessions=tuple(sessions(count)),
        open=matrix("open"),
        high=matrix("high"),
        low=matrix("low"),
        close=matrix("close"),
        turnover_inr=np.full((1, count), DEEP),
        turnover_avg_20=np.full((1, count), DEEP),
        signal=signal,
    )


def run(panel: Panel) -> BacktestResult:
    """The sleeve's own parameters — ``04`` §12's capital and the exchange's tick."""
    return run_backtest(
        panel,
        np.ones(panel.sessions_count, dtype=bool),
        BacktestParams(sleeve_inr=Decimal("1000000"), tick=TICK),
    )


def only_trade(result: BacktestResult) -> BacktestTrade:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


class TestTheBacktestReadsTheSameRule:
    """G1: in the engine, not only in the function the engine calls."""

    def test_a_fill_whose_session_low_breaches_the_stop_closes_that_session(self) -> None:
        # Session 1 is the fill (open 100.00 -> stop 80.00); its own low is 75.
        trade = only_trade(run(one_name(low={1: 75.0})))
        assert trade.reason is ExitReason.STOP_DAY0
        assert trade.exit_price == Decimal("80.00")
        assert trade.entry_date == trade.exit_date
        assert trade.hold_sessions == 0

    def test_the_exit_price_is_the_stop_and_never_the_low_it_traded_through(self) -> None:
        deep = only_trade(run(one_name(low={1: 1.0})))
        shallow = only_trade(run(one_name(low={1: 79.95})))
        assert deep.exit_price == shallow.exit_price == Decimal("80.00")

    def test_a_low_one_tick_above_the_stop_leaves_the_position_open(self) -> None:
        """80.05 against a stop of 80.00: the engine's own one-tick boundary, and the trade that
        survives it runs to the end of the panel instead."""
        trade = only_trade(run(one_name(low={1: 80.05})))
        assert trade.reason is ExitReason.END_OF_RUN
        assert trade.hold_sessions > 0

    def test_a_low_exactly_at_the_stop_is_out_on_the_fill_day(self) -> None:
        trade = only_trade(run(one_name(low={1: 80.0})))
        assert trade.reason is ExitReason.STOP_DAY0

    def test_the_engine_cannot_gap_through_its_own_fill_days_stop(self) -> None:
        """The open-below branch is a **live-book** branch and this asserts the seam rather than
        assuming it.

        The backtest's entry is the session open and its stop is ``stop_pct`` below that same
        open, so ``open <= stop`` cannot arise here. It arises live because the stop resting at
        the exchange is last night's ratcheted level against this morning's gap — which is why
        :class:`TestTheOpenBelowTheStop` above is read through ``fill_day_stop`` directly.
        """
        trade = only_trade(run(one_name(low={1: 0.0})))
        assert trade.exit_price == Decimal("80.00"), "the stop, never the open"


class TestTheRuleAndTheGttAreOne:
    """**The module, in one class.** ``04`` §7.4: in the live book the GTT armed in the same
    session as the fill *is* ``STOP_DAY0``.

    So the level the backtest fills a day-zero exit at and the trigger
    :func:`~baskfy_core.twt.exits.initial_stop` hands the desk to arm must be the same number.
    A drift between them is a backtest describing a book nobody owns, and it would be invisible
    in every other test in this tree.
    """

    @pytest.mark.parametrize(
        "fill",
        [Decimal("100.00"), Decimal("247.35"), Decimal("31.10"), Decimal("1999.95")],
    )
    def test_the_day_zero_fill_price_is_the_trigger_the_live_gtt_is_armed_at(
        self, fill: Decimal
    ) -> None:
        trigger = initial_stop(fill, EXITS, TICK)
        action = fill_day_stop(
            position(fill=fill, stop=trigger),
            bar(open_=fill, low=trigger, close=fill),
        )
        assert action is not None
        assert action.price == trigger

    def test_the_engines_stop_is_the_same_arithmetic_the_desk_would_arm(self) -> None:
        trade = only_trade(run(one_name(low={1: 75.0})))
        assert trade.exit_price == initial_stop(Decimal("100.00"), EXITS, TICK)

    def test_the_rule_is_the_level_and_nothing_else_about_the_bar(self) -> None:
        """Why non-negotiable 4 is not negotiable here, stated as an assertion.

        ``fill_day_stop`` reads exactly one thing about the position — the stop resting against
        it — and that is the number the desk arms as a GTT. A line whose ``gtt_id`` is null has
        no such level at the exchange, so it has no fill-day protection at all; TW7's sweep is
        the other half of that sentence, and ``tools/twt/sweep.py`` is where it is asserted.
        """
        # The session's low is exactly ``AWKWARD_STOP``. Against that stop it is a breach;
        # against a stop one tick lower it is not, and nothing else about the bar has moved.
        breach = bar(open_=Decimal("246.00"), low=AWKWARD_STOP, close=Decimal("198.00"))
        at_the_awkward_stop = fill_day_stop(position(fill=AWKWARD_FILL, stop=AWKWARD_STOP), breach)
        a_tick_lower = fill_day_stop(position(fill=AWKWARD_FILL, stop=AWKWARD_STOP - TICK), breach)
        assert at_the_awkward_stop is not None
        assert at_the_awkward_stop.price == AWKWARD_STOP
        assert a_tick_lower is None, "one tick of stop is one trade kept or lost"
