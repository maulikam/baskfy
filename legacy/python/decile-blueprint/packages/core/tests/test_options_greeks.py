"""``docs/options/04`` §2 — the chain, the parity forward, Black-76 greeks and the IV solver.

Independent anchors, not the code checked against itself: Hull's worked Black-76 example (a
futures put, *Options, Futures, and Other Derivatives*, "Black's model"), put-call parity to
1e-9, delta parity, and finite differences for every greek.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from options_fixtures import LOT, TICK, quote, strikes_around

from baskfy_core.options.chain import (
    Illiquid,
    Level,
    OptionQuote,
    atm,
    depth_available,
    is_liquid,
    is_stale_index,
    is_stale_quote,
    is_stale_scan,
    parity_forward,
    quote_greeks,
    snapshot_strikes,
    strike_step,
)
from baskfy_core.options.config import DEFAULT_OPTIONS_CONFIG, ChainConfig, OiUnit, OptionType, Side
from baskfy_core.options.greeks import (
    Greeks,
    black76_greeks,
    black76_price,
    implied_vol,
    intrinsic,
    solve,
    year_fraction,
)

CHAIN = DEFAULT_OPTIONS_CONFIG.chain
SETTLE = DEFAULT_OPTIONS_CONFIG.calendar.market_close
RATE = CHAIN.rate
CE, PE = OptionType.CE, OptionType.PE
EXPIRY = dt.date(2026, 10, 27)
NOW = dt.datetime(2026, 10, 20, 10, 0)
T = year_fraction(NOW, EXPIRY, SETTLE)

prices = st.floats(min_value=15000, max_value=35000)
vols = st.floats(min_value=0.05, max_value=1.5)
years = st.floats(min_value=1 / (365 * 24), max_value=10 / 365)


def _solve(price: float, forward: float, strike: float, t: float, kind: OptionType) -> float | None:
    return implied_vol(
        price, forward, strike, t, RATE, kind,
        lower=CHAIN.iv_lower, upper=CHAIN.iv_upper, tolerance=CHAIN.iv_tolerance,
    )  # fmt: skip


class TestTime:
    def test_calendar_minutes_to_1530_on_expiry(self) -> None:
        """``04`` §2.3: ``T = minutes / (365 * 24 * 60)``."""
        now = dt.datetime(2026, 10, 27, 15, 0)
        assert year_fraction(now, EXPIRY, SETTLE) == pytest.approx(30 / 525_600, rel=1e-12)

    def test_seven_days_is_seven_calendar_days(self) -> None:
        now = dt.datetime(2026, 10, 20, 15, 30)
        assert year_fraction(now, EXPIRY, SETTLE) == pytest.approx(7 / 365, rel=1e-12)

    def test_after_settlement_is_negative(self) -> None:
        assert year_fraction(dt.datetime(2026, 10, 27, 15, 31), EXPIRY, SETTLE) < 0


class TestBlack76:
    def test_hull_worked_example(self) -> None:
        """Hull: F=20, K=20, r=9 %, T=4 months, sigma=25 % → a European futures put worth 1.1166."""
        assert black76_price(20, 20, 4 / 12, 0.09, 0.25, PE) == pytest.approx(1.1166, abs=5e-4)

    @settings(max_examples=300, deadline=None)
    @given(prices, st.floats(min_value=0.8, max_value=1.2), years, vols)
    def test_put_call_parity_to_1e9(
        self, forward: float, moneyness: float, t: float, vol: float
    ) -> None:
        strike = forward * moneyness
        call = black76_price(forward, strike, t, RATE, vol, CE)
        put = black76_price(forward, strike, t, RATE, vol, PE)
        assert abs(call - put - math.exp(-RATE * t) * (forward - strike)) <= 1e-9

    @settings(max_examples=200, deadline=None)
    @given(prices, st.floats(min_value=0.9, max_value=1.1), years, vols)
    def test_delta_parity(self, forward: float, moneyness: float, t: float, vol: float) -> None:
        strike = forward * moneyness
        call = black76_greeks(forward, strike, t, RATE, vol, CE)
        put = black76_greeks(forward, strike, t, RATE, vol, PE)
        assert call.delta - put.delta == pytest.approx(math.exp(-RATE * t), abs=1e-12)
        assert call.gamma == pytest.approx(put.gamma, rel=1e-12)
        assert call.vega == pytest.approx(put.vega, rel=1e-12)

    @pytest.mark.parametrize("kind", [CE, PE])
    @pytest.mark.parametrize("strike", [24500.0, 25000.0, 25600.0])
    def test_greeks_match_finite_differences(self, kind: OptionType, strike: float) -> None:
        f, t, vol = 25010.0, T, 0.14
        g = black76_greeks(f, strike, t, RATE, vol, kind)
        h = 0.5
        up, down = (black76_price(f + d, strike, t, RATE, vol, kind) for d in (h, -h))
        assert g.delta == pytest.approx((up - down) / (2 * h), abs=1e-6)
        mid = black76_price(f, strike, t, RATE, vol, kind)
        assert g.gamma == pytest.approx((up - 2 * mid + down) / (h * h), rel=1e-3)
        dv = 1e-4
        vega = (
            black76_price(f, strike, t, RATE, vol + dv, kind)
            - black76_price(f, strike, t, RATE, vol - dv, kind)
        ) / (2 * dv)
        assert g.vega == pytest.approx(vega / 100, rel=1e-6)  # per vol point
        dt_ = 1e-6
        theta = -(
            black76_price(f, strike, t + dt_, RATE, vol, kind)
            - black76_price(f, strike, t - dt_, RATE, vol, kind)
        ) / (2 * dt_)
        assert g.theta == pytest.approx(theta / 365, rel=1e-4)  # per calendar day

    def test_greeks_refuse_degenerate_inputs(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            black76_greeks(25000, 25000, 0.0, RATE, 0.2, CE)

    def test_degenerate_price_is_discounted_intrinsic(self) -> None:
        assert black76_price(25100, 25000, 0.0, RATE, 0.2, CE) == pytest.approx(100.0)
        assert black76_price(25100, 25000, 0.0, RATE, 0.2, PE) == 0.0
        # After settlement nothing is discounted: the value is the intrinsic itself.
        assert black76_price(25100, 25000, -0.01, RATE, 0.2, CE) == 100.0

    def test_intrinsic_on_the_forward(self) -> None:
        assert intrinsic(25100, 25000, CE) == 100
        assert intrinsic(25100, 25000, PE) == 0
        assert intrinsic(24900, 25000, PE) == 100


class TestImpliedVol:
    @settings(max_examples=300, deadline=None)
    @given(
        prices, st.floats(min_value=0.95, max_value=1.05), years, vols, st.sampled_from([CE, PE])
    )
    def test_round_trips(
        self, forward: float, moneyness: float, t: float, vol: float, kind: OptionType
    ) -> None:
        strike = forward * moneyness
        price = black76_price(forward, strike, t, RATE, vol, kind)
        assume(price >= float(CHAIN.min_premium))
        assume(price - intrinsic(forward, strike, kind) > 0.05)
        solved = _solve(price, forward, strike, t, kind)
        assert solved is not None
        assert solved == pytest.approx(vol, abs=1e-5)

    def test_refuses_a_price_above_the_highest_vol(self) -> None:
        ceiling = black76_price(25000, 25000, T, RATE, CHAIN.iv_upper, CE)
        assert _solve(ceiling * 1.01, 25000, 25000, T, CE) is None

    def test_refuses_a_price_below_the_lowest_vol(self) -> None:
        floor = black76_price(25000, 25500, T, RATE, CHAIN.iv_lower, CE)
        assert _solve(floor / 2, 25000, 25500, T, CE) is None

    def test_refuses_degenerate_inputs(self) -> None:
        assert _solve(0.0, 25000, 25000, T, CE) is None
        assert _solve(100.0, 25000, 25000, 0.0, CE) is None


class TestSolveRefusals:
    """``04`` §2.3's three refusals, each producing ``None`` — no IV, no greeks."""

    def _solve(
        self, mid: float, forward: float = 25000.0, strike: float = 25000.0, t: float = T
    ) -> Greeks | None:
        return solve(
            mid, forward, strike, t, RATE, CE,
            min_premium=float(CHAIN.min_premium), tick=float(TICK),
            lower=CHAIN.iv_lower, upper=CHAIN.iv_upper, tolerance=CHAIN.iv_tolerance,
        )  # fmt: skip

    def test_below_min_premium(self) -> None:
        assert self._solve(0.45, strike=26500.0) is None

    def test_below_intrinsic_plus_a_tick(self) -> None:
        assert self._solve(100.04, forward=25100.0) is None

    def test_no_convergence(self) -> None:
        assert self._solve(20000.0) is None

    def test_after_settlement(self) -> None:
        assert self._solve(100.0, t=-0.001) is None

    def test_a_good_quote_solves(self) -> None:
        price = black76_price(25000, 25000, T, RATE, 0.13, CE)
        solved = self._solve(price)
        assert solved is not None


def _parity_quotes(
    forward: float, strikes: tuple[int, ...], vol: float = 0.13
) -> list[OptionQuote]:
    out: list[OptionQuote] = []
    for k in strikes:
        for kind in (CE, PE):
            p = Decimal(repr(black76_price(forward, k, T, RATE, vol, kind)))
            out.append(quote(k, kind, p - Decimal("0.5"), p + Decimal("0.5"), expiry=EXPIRY))
    return out


class TestParityForward:
    def test_recovers_the_forward_from_the_atm_pair(self) -> None:
        quotes = _parity_quotes(25012.5, (24950, 25000, 25050))
        found = parity_forward(quotes, Decimal("25010"), Decimal(50), T, RATE)
        assert found is not None
        assert float(found) == pytest.approx(25012.5, abs=1e-6)

    def test_falls_back_to_the_next_nearest_pair(self) -> None:
        quotes = _parity_quotes(25012.5, (24950, 25000, 25050))
        broken = [
            q
            if not (q.strike == 25000 and q.option_type is PE)
            else quote(25000, PE, None, "90", expiry=EXPIRY)
            for q in quotes
        ]
        found = parity_forward(broken, Decimal("25010"), Decimal(50), T, RATE)
        assert found is not None
        assert float(found) == pytest.approx(25012.5, abs=1e-6)

    def test_no_pair_no_forward(self) -> None:
        only_calls = [quote(25000, CE, "100", "101", expiry=EXPIRY)]
        assert parity_forward(only_calls, Decimal(25000), Decimal(50), T, RATE) is None

    def test_one_expiry_only(self) -> None:
        mixed = [
            quote(25000, CE, "1", "2", expiry=EXPIRY),
            quote(25000, PE, "1", "2", expiry=dt.date(2026, 11, 3)),
        ]
        with pytest.raises(ValueError, match="one expiry"):
            parity_forward(mixed, Decimal(25000), Decimal(50), T, RATE)

    def test_quote_greeks_on_the_forward(self) -> None:
        price = Decimal(repr(black76_price(25012.5, 25100, T, RATE, 0.13, CE)))
        q = quote(25100, CE, price - Decimal("0.5"), price + Decimal("0.5"), expiry=EXPIRY)
        g = quote_greeks(q, Decimal("25012.5"), now=NOW, tick=TICK, config=CHAIN, settle=SETTLE)
        assert g is not None
        assert g.iv == pytest.approx(0.13, abs=1e-5)
        assert 0 < g.delta < 0.5

    def test_quote_greeks_refuse_without_a_mid(self) -> None:
        q = quote(25100, CE, None, "10", expiry=EXPIRY)
        assert (
            quote_greeks(q, Decimal(25000), now=NOW, tick=TICK, config=CHAIN, settle=SETTLE) is None
        )


class TestStrikes:
    """``04`` §2.1."""

    @pytest.mark.parametrize(
        ("spot", "expected"),
        [("25024", 25000), ("25025", 25000), ("25026", 25050), ("24975", 24950)],
    )
    def test_atm_ties_go_to_the_lower_strike(self, spot: str, expected: int) -> None:
        assert atm(Decimal(spot), Decimal(50)) == expected

    def test_atm_refuses_a_zero_step(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            atm(Decimal(25000), Decimal(0))

    def test_the_step_is_read_near_the_money(self) -> None:
        assert strike_step(strikes_around(), Decimal(25000), CHAIN.snapshot_strikes) == 50

    def test_far_from_the_money_the_grid_is_wider(self) -> None:
        assert strike_step(strikes_around(), Decimal(26900), 3) == 100

    def test_a_modal_tie_goes_to_the_finer_grid(self) -> None:
        strikes = [Decimal(k) for k in (100, 150, 200, 300, 400)]
        assert strike_step(strikes, Decimal(250), 2) == 50

    def test_one_strike_has_no_step(self) -> None:
        assert strike_step([Decimal(25000)], Decimal(25000), 15) is None

    def test_snapshot_strikes_either_side_of_atm(self) -> None:
        chosen = snapshot_strikes(
            strikes_around(), Decimal("25010"), Decimal(50), CHAIN.snapshot_strikes
        )
        assert len(chosen) == 2 * CHAIN.snapshot_strikes + 1
        assert chosen[0] == 24250
        assert chosen[-1] == 25750


class TestLiquidity:
    """``04`` §2.4 — spread, depth on the side taken, OI; a wing needs only depth."""

    QTY = LOT * 2

    def test_mid_and_spread(self) -> None:
        q = quote(25000, CE, "99", "101")
        assert q.mid == 100
        assert q.spread_pct == 2

    def test_a_liquid_contract(self) -> None:
        assert is_liquid(quote(25000, CE, "99", "101"), self.QTY, Side.SELL, LOT, CHAIN).ok

    def test_spread_boundary(self) -> None:
        at_limit = quote(25000, CE, "98.5", "101.5")  # exactly 3.0 %
        assert is_liquid(at_limit, self.QTY, Side.BUY, LOT, CHAIN).ok
        wide = quote(25000, CE, "98.4", "101.6")
        assert is_liquid(wide, self.QTY, Side.BUY, LOT, CHAIN).reasons == (
            Illiquid.SPREAD_TOO_WIDE,
        )

    def test_depth_is_read_on_the_side_the_order_takes(self) -> None:
        thin_asks = quote(25000, CE, "99", "101", asks=(Level(Decimal(101), 10),))
        assert is_liquid(thin_asks, self.QTY, Side.SELL, LOT, CHAIN).ok
        assert is_liquid(thin_asks, self.QTY, Side.BUY, LOT, CHAIN).reasons == (
            Illiquid.DEPTH_TOO_THIN,
        )

    def test_depth_counts_only_the_first_levels(self) -> None:
        ladder = tuple(Level(Decimal(101) + i, 40) for i in range(5))
        q = quote(25000, CE, "99", "101", asks=ladder)
        assert depth_available(q, Side.BUY, CHAIN.depth_levels) == 120
        assert not is_liquid(q, 130, Side.BUY, LOT, CHAIN).ok
        assert is_liquid(q, 120, Side.BUY, LOT, CHAIN).ok

    def test_open_interest_floor(self) -> None:
        floor = CHAIN.min_oi_lots * LOT
        assert is_liquid(quote(25000, CE, "99", "101", oi=floor), self.QTY, Side.BUY, LOT, CHAIN).ok
        low = quote(25000, CE, "99", "101", oi=floor - 1)
        assert is_liquid(low, self.QTY, Side.BUY, LOT, CHAIN).reasons == (Illiquid.OI_TOO_LOW,)

    def test_open_interest_in_lots_is_converted(self) -> None:
        lots = ChainConfig(oi_unit=OiUnit.LOTS)
        q = quote(25000, CE, "99", "101", oi=CHAIN.min_oi_lots)
        assert is_liquid(q, self.QTY, Side.BUY, LOT, lots).ok
        assert not is_liquid(q, self.QTY, Side.BUY, LOT, CHAIN).ok

    def test_a_wing_needs_only_depth(self) -> None:
        wide_thin_oi = quote(25000, CE, "1", "2", oi=0)
        assert is_liquid(wide_thin_oi, self.QTY, Side.BUY, LOT, CHAIN, wing=True).ok
        no_depth = quote(25000, CE, "1", "2", asks=(), oi=0)
        verdict = is_liquid(no_depth, self.QTY, Side.BUY, LOT, CHAIN, wing=True)
        assert verdict.reasons == (Illiquid.DEPTH_TOO_THIN,)

    def test_every_reason_is_reported(self) -> None:
        q = quote(25000, CE, None, "101", asks=(), oi=0)
        verdict = is_liquid(q, self.QTY, Side.BUY, LOT, CHAIN)
        assert verdict.reasons == (Illiquid.NO_QUOTE, Illiquid.DEPTH_TOO_THIN, Illiquid.OI_TOO_LOW)


class TestStaleness:
    """``04`` §2.5."""

    def test_quote(self) -> None:
        ts = dt.datetime(2026, 10, 27, 10, 0, 0)
        assert not is_stale_quote(ts, ts + dt.timedelta(seconds=15), CHAIN)
        assert is_stale_quote(ts, ts + dt.timedelta(seconds=16), CHAIN)

    def test_index(self) -> None:
        ts = dt.datetime(2026, 10, 27, 14, 0, 0)
        assert not is_stale_index(ts, ts + dt.timedelta(seconds=30), CHAIN)
        assert is_stale_index(ts, ts + dt.timedelta(seconds=31), CHAIN)
        assert is_stale_index(None, ts, CHAIN)

    def test_scan_after_two_minutes(self) -> None:
        ts = dt.datetime(2026, 10, 27, 11, 0, 0)
        assert not is_stale_scan(ts, ts + dt.timedelta(minutes=2), CHAIN)
        assert is_stale_scan(ts, ts + dt.timedelta(minutes=2, seconds=1), CHAIN)
