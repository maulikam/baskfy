"""docs/swing/07 §"SW9.5" step 5 — the five acceptance facts of the primary-source corrections.

Each test below is one sentence of that acceptance, asserted against `04` as amended by `07`
(his own words, quoted there), never against what the code happened to do:

1. no `BUY_ON_TRIGGER` line ever has `stop_distance_pct > widest_stop_pct(adr)` — a property
   over random watchlists, because the rule is "a wider stop is skipped, never sized down";
2. a sleeve 15% below its peak plans zero entries and every skip says `DRAWDOWN_LOCKOUT`;
3. the same sleeve at 9.9% after a lock-out plans entries again, at rung 0;
4. a watchlist of ten qualifying flags yields exactly three lines and seven `SESSION_CAP` skips;
5. the plan takes `min(rung, sizing.max_open_positions)`.

Every number is re-derived in the test that uses it, and the docstring says why it is what it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from typing import Final

from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    MarketConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.market import ExposureTier, MarketGate, drawdown_locked, exposure_tier
from baskfy_core.swing.plan import (
    LineKind,
    SkipReason,
    SwingAccount,
    WatchItem,
    build_entries,
)
from baskfy_core.swing.stops import widest_stop_pct

#: One seed for the file, in every assertion message, so a red run reproduces itself.
SEED: Final = 20260902
EXAMPLES: Final = 500

AS_OF: Final = dt.date(2026, 9, 2)
CONFIG: Final[SwingConfig] = DEFAULT_SWING_CONFIG
MARKET: Final[MarketConfig] = CONFIG.market
SIZING: Final[SizingConfig] = CONFIG.sizing

_TICK: Final = Decimal("0.05")
_PCT: Final = Decimal(100)
_TWO_DP: Final = Decimal("0.01")

#: A ₹1 crore sleeve, all of it free: big enough that neither cash nor the exposure ceiling
#: refuses anything in the count-based tests, so the count is what decides.
SLEEVE: Final = SwingAccount(Decimal(10_000_000), Decimal(10_000_000), frozenset(), Decimal(0))

_SUITE = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _why(what: str) -> str:
    return f"{what} (hypothesis seed {SEED})"


def _distance_pct(item: WatchItem) -> Decimal:
    """`04` §5.1's own arithmetic: `(entry - stop) / entry x 100`, 2 dp."""
    return ((item.trigger - item.stop_ref) / item.trigger * _PCT).quantize(_TWO_DP)


def _flag(symbol: str, *, score: str = "70", adr: str = "5", stop: str = "96") -> WatchItem:
    """A qualifying flag: trigger ₹100, stop 4% under it (inside a 5% ADR), deep turnover."""
    return WatchItem(
        symbol=symbol,
        setup=Setup.FLAG,
        trigger=Decimal(100),
        stop_ref=Decimal(stop),
        adr_pct=Decimal(adr),
        avg_turnover_inr=Decimal(1_000_000_000),
        score=Decimal(score),
    )


def _tier(level: int, *, config: MarketConfig = MARKET) -> ExposureTier:
    positions, exposure = config.tiers[level]
    return ExposureTier(level, positions, exposure, new_entries_allowed=True)


# --- strategies ---------------------------------------------------------------------------


def _snap(price: Decimal) -> Decimal:
    return (price / _TICK).to_integral_value() * _TICK


prices = st.integers(min_value=int(Decimal(20) / _TICK), max_value=int(Decimal(5000) / _TICK)).map(
    lambda n: n * _TICK
)
symbols = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=6)


@st.composite
def watch_items(draw: st.DrawFn) -> WatchItem:
    """Any tradeable name with any stop — from 2% *above* the trigger to 15% below it, on any
    ADR from 0 to 15% — so the property is asked about stops the rule must refuse as well as
    the ones it must admit."""
    trigger = draw(prices)
    distance_pct = draw(st.decimals(min_value=Decimal("-2"), max_value=Decimal("15"), places=2))
    return WatchItem(
        symbol=draw(symbols),
        setup=draw(st.sampled_from([Setup.FLAG, Setup.EP])),
        trigger=trigger,
        stop_ref=_snap(trigger * (1 - distance_pct / _PCT)),
        adr_pct=draw(st.decimals(min_value=Decimal("0"), max_value=Decimal("15"), places=2)),
        avg_turnover_inr=draw(
            st.one_of(
                st.none(),
                st.decimals(min_value=Decimal(0), max_value=Decimal(5_000_000_000), places=0),
            )
        ),
        score=draw(st.decimals(min_value=Decimal(0), max_value=Decimal(100), places=2)),
        locked_upper_circuit=draw(st.booleans()),
    )


@st.composite
def watchlists(draw: st.DrawFn) -> list[WatchItem]:
    items = draw(st.lists(watch_items(), min_size=1, max_size=12))
    seen: set[str] = set()
    unique: list[WatchItem] = []
    for item in items:
        if item.symbol not in seen:
            seen.add(item.symbol)
            unique.append(item)
    return unique


# --- 1. the stop is one ADR or tighter, and a wider stop is skipped -----------------------


@seed(SEED)
@_SUITE
@given(
    watch=watchlists(),
    level=st.integers(min_value=0, max_value=len(MARKET.tiers) - 1),
    equity=st.decimals(min_value=Decimal(100_000), max_value=Decimal(50_000_000), places=0),
)
def test_no_buy_line_ever_carries_a_stop_wider_than_one_adr(
    watch: list[WatchItem], level: int, equity: Decimal
) -> None:
    """`04` §6 (SW9.5), his words: "stop should not be wider than the ATR or ADR of the stock".

    The widest stop is `widest_stop_pct(adr) = min(adr x max_stop_adr_multiple [1.0],
    max_stop_distance_pct [10])`. Over 500 random watchlists at every rung and sleeve size:
    every `BUY_ON_TRIGGER` line's stop distance is at or inside that number, and every name
    whose stop is wider is answered `SIZE_REFUSED / STOP_TOO_WIDE` — refused, never sized
    down to fit. A name with an ADR of 0 (unmeasured) admits no stop at all.
    """
    account = SwingAccount(equity, equity, frozenset(), Decimal(0))
    lines, skipped = build_entries(
        as_of=AS_OF,
        watch=watch,
        account=account,
        gate=MarketGate.GREEN,
        tier=_tier(level),
        config=CONFIG,
    )
    by_symbol = {item.symbol: item for item in watch}
    for line in lines:
        assert line.kind is LineKind.BUY_ON_TRIGGER
        item = by_symbol[line.symbol]
        widest = widest_stop_pct(item.adr_pct, CONFIG.stops)
        assert _distance_pct(item) <= widest, _why(
            f"{line.symbol}: stop {_distance_pct(item)}% below on an ADR of {item.adr_pct}%"
        )
        assert line.quantity > 0
    for skip in skipped:
        item = by_symbol[skip.symbol]
        too_wide = _distance_pct(item) > widest_stop_pct(item.adr_pct, CONFIG.stops)
        if skip.reason is SkipReason.SIZE_REFUSED and skip.detail == "STOP_TOO_WIDE":
            assert too_wide, _why(f"{skip.symbol} refused STOP_TOO_WIDE with a stop inside its ADR")
    lined = {line.symbol for line in lines}
    for item in watch:
        if item.symbol in lined:
            assert _distance_pct(item) <= widest_stop_pct(item.adr_pct, CONFIG.stops), _why(
                f"{item.symbol} was lined with a stop wider than its ADR"
            )


def test_a_stop_wider_than_one_adr_is_refused_not_shrunk_to_fit() -> None:
    """The counter-example in one line: 6% under on a 5% ADR name is `STOP_TOO_WIDE`, and the
    plan carries no smaller position in its place; 5% under — exactly one ADR — is lined."""
    lines, skipped = build_entries(
        as_of=AS_OF,
        watch=[_flag("WIDE", stop="94"), _flag("EDGE", stop="95")],
        account=SLEEVE,
        gate=MarketGate.GREEN,
        tier=_tier(3),
        config=CONFIG,
    )
    assert [line.symbol for line in lines] == ["EDGE"]
    assert [(s.symbol, s.reason, s.detail) for s in skipped] == [
        ("WIDE", SkipReason.SIZE_REFUSED, "STOP_TOO_WIDE")
    ]
    assert widest_stop_pct(Decimal(5), CONFIG.stops) == Decimal("5.00")
    assert widest_stop_pct(Decimal(14), CONFIG.stops) == Decimal("10.00"), "the absolute cap"


# --- 2 and 3. the drawdown lock-out, and its release --------------------------------------


def test_a_sleeve_fifteen_percent_below_its_peak_plans_zero_entries_and_says_why() -> None:
    """`04` §8.5 (SW9.5): "I try to contain them at 15-20%." `max_drawdown_pct` [15]: at
    exactly 15% below its peak the sleeve is locked — rung 0, no entries — and the plan answers
    every qualifying name `DRAWDOWN_LOCKOUT`, before the gate, the count or the money are
    consulted. A GREEN tape and a top-rung record do not help; the drawdown outranks them."""
    tier = exposure_tier(
        current_level=len(MARKET.tiers) - 1,
        closed_r_multiples=[Decimal(3)] * MARKET.lookback_trades,
        gate=MarketGate.GREEN,
        config=MARKET,
        drawdown_pct=MARKET.max_drawdown_pct,
        was_drawdown_locked=False,
    )
    assert (tier.level, tier.new_entries_allowed, tier.drawdown_locked) == (0, False, True)
    watch = [_flag(f"FLAG{n}", score=str(90 - n)) for n in range(10)]
    lines, skipped = build_entries(
        as_of=AS_OF, watch=watch, account=SLEEVE, gate=MarketGate.GREEN, tier=tier, config=CONFIG
    )
    assert lines == []
    assert len(skipped) == 10
    assert {s.reason for s in skipped} == {SkipReason.DRAWDOWN_LOCKOUT}
    assert sorted(s.symbol for s in skipped) == sorted(item.symbol for item in watch)


def test_the_same_sleeve_back_within_ten_percent_plans_entries_again_at_rung_zero() -> None:
    """`04` §8.5: locked at 15%, released only once the drawdown is back inside
    `resume_drawdown_pct` [10] — hysteresis, so a sleeve oscillating around 15% does not flap.

    At 9.9% after a lock-out the sleeve trades again, and it starts over at rung 0 whatever
    rung it held before: `exposure_tier` is asked from rung 0 (the rung the lock-out settled),
    and its rung-0 ceiling of two positions is what the plan lines. At 12% after a lock-out it
    is still locked; at 12% with no lock-out behind it, it never was.
    """
    assert drawdown_locked(drawdown_pct=9.9, was_locked=True, config=MARKET) is False
    assert drawdown_locked(drawdown_pct=12.0, was_locked=True, config=MARKET) is True
    assert drawdown_locked(drawdown_pct=12.0, was_locked=False, config=MARKET) is False
    assert drawdown_locked(drawdown_pct=10.0, was_locked=True, config=MARKET) is False, "<= 10"
    assert drawdown_locked(drawdown_pct=15.0, was_locked=False, config=MARKET) is True, ">= 15"

    tier = exposure_tier(
        current_level=0,
        closed_r_multiples=[],
        gate=MarketGate.GREEN,
        config=MARKET,
        drawdown_pct=9.9,
        was_drawdown_locked=True,
    )
    assert (tier.level, tier.new_entries_allowed, tier.drawdown_locked) == (0, True, False)
    assert tier.max_open_positions == MARKET.tiers[0][0] == 2
    watch = [_flag(f"FLAG{n}", score=str(90 - n)) for n in range(10)]
    lines, skipped = build_entries(
        as_of=AS_OF, watch=watch, account=SLEEVE, gate=MarketGate.GREEN, tier=tier, config=CONFIG
    )
    assert [line.symbol for line in lines] == ["FLAG0", "FLAG1"]
    assert {s.reason for s in skipped} == {SkipReason.TIER_FULL}
    assert SkipReason.DRAWDOWN_LOCKOUT not in {s.reason for s in skipped}


# --- 4. at most three new entries a session -----------------------------------------------


def test_ten_qualifying_flags_yield_three_lines_and_seven_session_cap_skips() -> None:
    """`04` §5 / §9.1 (SW9.5): "1, 2, 3 stocks per day… no need to trade more than that" —
    `max_new_entries_per_session` [3]. Ten flags that qualify on every other count (the top
    rung allows ten, the sleeve is ₹1 cr, every stop is 4% under on a 5% ADR): the three best
    by score are lined, the other seven are `SESSION_CAP`, and the detail says the number."""
    watch = [_flag(f"FLAG{n}", score=str(90 - n)) for n in range(10)]
    top = len(MARKET.tiers) - 1
    lines, skipped = build_entries(
        as_of=AS_OF,
        watch=watch,
        account=SLEEVE,
        gate=MarketGate.GREEN,
        tier=_tier(top),
        config=CONFIG,
    )
    assert SIZING.max_new_entries_per_session == 3
    assert [line.symbol for line in lines] == ["FLAG0", "FLAG1", "FLAG2"]
    assert len(skipped) == 7
    assert {s.reason for s in skipped} == {SkipReason.SESSION_CAP}
    assert {s.detail for s in skipped} == {"3 new entries per session"}
    assert sorted(s.symbol for s in skipped) == [f"FLAG{n}" for n in range(3, 10)]


def test_the_session_cap_counts_lines_in_this_plan_not_positions_held() -> None:
    """Three names already held do not use up the session: the cap is on *new* entries, so a
    book of three at rung 3 still lines three more — and a fourth is `SESSION_CAP`, not
    `TIER_FULL` (six of ten positions)."""
    held = SwingAccount(
        Decimal(10_000_000), Decimal(9_000_000), frozenset({"H1", "H2", "H3"}), Decimal(1_000_000)
    )
    watch = [_flag(f"FLAG{n}", score=str(90 - n)) for n in range(4)]
    lines, skipped = build_entries(
        as_of=AS_OF,
        watch=watch,
        account=held,
        gate=MarketGate.GREEN,
        tier=_tier(len(MARKET.tiers) - 1),
        config=CONFIG,
    )
    assert [line.symbol for line in lines] == ["FLAG0", "FLAG1", "FLAG2"]
    assert [(s.symbol, s.reason) for s in skipped] == [("FLAG3", SkipReason.SESSION_CAP)]


# --- 5. the plan takes min(rung, sizing.max_open_positions) ------------------------------


def test_the_plan_takes_the_smaller_of_the_rung_and_the_traders_cap() -> None:
    """`04` §5 / §9.1 (SW9.5): the position count is `min(tier.max_open_positions,
    sizing.max_open_positions)`. The session cap is lifted to ten here so that only the two
    counts are in play: rung 1 (four) against a cap of ten lines four; rung 3 (ten) against a
    cap of two lines two; and the `TIER_FULL` detail names the number that bound."""
    roomy = replace(CONFIG, sizing=replace(SIZING, max_new_entries_per_session=10))
    watch = [_flag(f"FLAG{n}", score=str(90 - n)) for n in range(10)]

    lines, skipped = build_entries(
        as_of=AS_OF, watch=watch, account=SLEEVE, gate=MarketGate.GREEN, tier=_tier(1), config=roomy
    )
    assert len(lines) == min(MARKET.tiers[1][0], roomy.sizing.max_open_positions) == 4
    assert {s.reason for s in skipped} == {SkipReason.TIER_FULL}
    assert {s.detail for s in skipped} == {"rung 1 allows 4 positions"}

    capped = replace(roomy, sizing=replace(roomy.sizing, max_open_positions=2))
    lines, skipped = build_entries(
        as_of=AS_OF,
        watch=watch,
        account=SLEEVE,
        gate=MarketGate.GREEN,
        tier=_tier(3),
        config=capped,
    )
    assert len(lines) == min(MARKET.tiers[3][0], capped.sizing.max_open_positions) == 2
    assert {s.detail for s in skipped} == {"rung 3 allows 2 positions"}

    # And the defaults agree with each other: the top rung *is* the trader's default cap.
    assert MARKET.tiers[-1][0] == SIZING.max_open_positions == 10
