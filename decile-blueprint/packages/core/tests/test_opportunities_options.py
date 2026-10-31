from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from baskfy_core.opportunities.config import Direction, QuoteRules, StrategyKind
from baskfy_core.opportunities.options import (
    IST,
    AcceptedQuote,
    ContractQuote,
    Leg,
    OptionsRequest,
    Rejection,
    analyze,
    build_candidate,
    expiry_pnl_per_unit,
    validate_legs,
    validate_quote,
)
from baskfy_core.options.config import CostRates, OptionType, Side

NOW = dt.datetime(2026, 10, 30, 11, 0, tzinfo=IST)
EXPIRY = dt.date(2026, 11, 24)
RULES = QuoteRules(max_spread_pct=Decimal("10"), max_quote_age_s=60, min_oi=100, min_volume=1)
RATES = CostRates()


def _quote(  # noqa: PLR0913
    strike: str,
    kind: OptionType,
    bid: str | None,
    ask: str | None,
    *,
    age_s: int = 5,
    lot: int = 500,
    oi: int = 10_000,
    volume: int = 50,
    expiry: dt.date = EXPIRY,
    underlying: str = "INFY",
) -> ContractQuote:
    return ContractQuote(
        tradingsymbol=f"{underlying}{expiry:%y%b}{strike}{kind.value}".upper(),
        underlying=underlying,
        expiry=expiry,
        strike=Decimal(strike),
        option_type=kind,
        lot_size=lot,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        volume=volume,
        oi=oi,
        quote_ts=NOW - dt.timedelta(seconds=age_s),
    )


def _accepted(quote: ContractQuote) -> AcceptedQuote:
    verdict = validate_quote(quote, underlying=quote.underlying, now=NOW, rules=RULES)
    assert isinstance(verdict, AcceptedQuote)
    return verdict


BASE_REQUEST = OptionsRequest(
    underlying="INFY",
    direction=Direction.BULLISH,
    strategies=frozenset(StrategyKind),
    max_loss_inr=None,
    min_dte=1,
    max_dte=60,
    rules=RULES,
)


def _request(
    *,
    direction: Direction = Direction.BULLISH,
    max_loss_inr: Decimal | None = None,
    min_dte: int = 1,
    max_dte: int = 60,
) -> OptionsRequest:
    return replace(
        BASE_REQUEST,
        direction=direction,
        max_loss_inr=max_loss_inr,
        min_dte=min_dte,
        max_dte=max_dte,
    )


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (_quote("1500", OptionType.CE, "20", "19"), "crossed"),
        (_quote("1500", OptionType.CE, "19", "20", age_s=600), "stale"),
        (_quote("1500", OptionType.CE, "10", "20"), "spread"),
        (_quote("1500", OptionType.CE, None, "20"), "incomplete"),
        (_quote("1500", OptionType.CE, "0", "20"), "incomplete"),
        (_quote("1500", OptionType.CE, "19", "20", oi=10), "open interest"),
        (_quote("1500", OptionType.CE, "19", "20", volume=0), "volume"),
        (_quote("1500", OptionType.CE, "19", "20", underlying="TCS"), "underlying"),
    ],
)
def test_bad_quotes_are_rejected_with_a_reason(quote: ContractQuote, reason: str) -> None:
    verdict = validate_quote(quote, underlying="INFY", now=NOW, rules=RULES)
    assert isinstance(verdict, Rejection)
    assert reason in verdict.reason


def test_bull_call_spread_uses_ask_to_buy_and_bid_to_sell() -> None:
    long_leg = Leg(_accepted(_quote("1500", OptionType.CE, "40", "41")), Side.BUY, None, None)
    short_leg = Leg(_accepted(_quote("1550", OptionType.CE, "20", "21")), Side.SELL, None, None)
    built = build_candidate(
        StrategyKind.BULL_CALL_SPREAD,
        [long_leg, short_leg],
        spot=Decimal("1510"),
        now=NOW,
        request=_request(),
        rates=RATES,
    )
    assert not isinstance(built, Rejection)
    assert built.net_debit_per_unit == Decimal("21")
    assert built.max_loss_per_unit == Decimal("21")
    assert built.max_profit_per_unit == Decimal("29")
    assert built.breakevens == (Decimal("1521.00"),)
    assert built.net_debit_per_lot == Decimal("10500")
    assert built.max_profit_per_lot == Decimal("14500")
    assert built.max_loss_after_costs_per_lot == built.max_loss_per_lot + built.round_trip_cost_inr
    assert built.round_trip_cost_inr > built.entry_cost_inr > 0
    payoff = {p.underlying: p.pnl_per_lot for p in built.payoff}
    assert payoff[Decimal("1500.00")] == Decimal("-10500.00")
    assert payoff[Decimal("1550.00")] == Decimal("14500.00")
    assert payoff[Decimal("1521.00")] == Decimal("0.00")
    assert min(payoff.values()) == Decimal("-10500.00")
    assert max(payoff.values()) == Decimal("14500.00")


def test_long_put_has_bounded_profit_and_breakeven_below_strike() -> None:
    leg = Leg(_accepted(_quote("1500", OptionType.PE, "30", "31")), Side.BUY, None, None)
    built = build_candidate(
        StrategyKind.LONG_PUT,
        [leg],
        spot=Decimal("1490"),
        now=NOW,
        request=_request(direction=Direction.BEARISH),
        rates=RATES,
    )
    assert not isinstance(built, Rejection)
    assert built.max_profit_per_unit == Decimal("1469")
    assert built.breakevens == (Decimal("1469.00"),)
    assert expiry_pnl_per_unit([leg], Decimal("1600")) == Decimal("-31")


def test_long_call_profit_is_unlimited() -> None:
    leg = Leg(_accepted(_quote("1500", OptionType.CE, "30", "31")), Side.BUY, None, None)
    built = build_candidate(
        StrategyKind.LONG_CALL,
        [leg],
        spot=Decimal("1500"),
        now=NOW,
        request=_request(),
        rates=RATES,
    )
    assert not isinstance(built, Rejection)
    assert built.max_profit_per_lot is None
    assert built.breakevens == (Decimal("1531.00"),)


def test_lots_are_sized_inside_the_max_loss_after_costs() -> None:
    leg = Leg(_accepted(_quote("1500", OptionType.CE, "9.6", "10")), Side.BUY, None, None)
    built = build_candidate(
        StrategyKind.LONG_CALL,
        [leg],
        spot=Decimal("1500"),
        now=NOW,
        request=_request(max_loss_inr=Decimal("12000")),
        rates=RATES,
    )
    assert not isinstance(built, Rejection)
    assert built.max_loss_per_lot == Decimal("5000")
    assert built.lots == 2
    assert built.lots * built.max_loss_after_costs_per_lot <= Decimal("12000")
    too_small = build_candidate(
        StrategyKind.LONG_CALL,
        [leg],
        spot=Decimal("1500"),
        now=NOW,
        request=_request(max_loss_inr=Decimal("4000")),
        rates=RATES,
    )
    assert isinstance(too_small, Rejection)


def test_spread_legs_must_share_expiry_and_lot_size() -> None:
    a = Leg(_accepted(_quote("1500", OptionType.CE, "40", "41")), Side.BUY, None, None)
    other_lot = Leg(
        _accepted(_quote("1550", OptionType.CE, "20", "21", lot=400)), Side.SELL, None, None
    )
    other_expiry = Leg(
        _accepted(_quote("1550", OptionType.CE, "20", "21", expiry=dt.date(2026, 12, 29))),
        Side.SELL,
        None,
        None,
    )
    assert validate_legs(StrategyKind.BULL_CALL_SPREAD, [a, other_lot]) == (
        "legs with different lot sizes"
    )
    assert validate_legs(StrategyKind.BULL_CALL_SPREAD, [a, other_expiry]) == (
        "legs on different expiries"
    )
    inverted = Leg(_accepted(_quote("1450", OptionType.CE, "60", "61")), Side.SELL, None, None)
    assert validate_legs(StrategyKind.BULL_CALL_SPREAD, [a, inverted]) is not None


def test_debit_at_or_above_width_is_rejected() -> None:
    a = Leg(_accepted(_quote("1500", OptionType.CE, "60", "61")), Side.BUY, None, None)
    b = Leg(_accepted(_quote("1550", OptionType.CE, "10", "11")), Side.SELL, None, None)
    built = build_candidate(
        StrategyKind.BULL_CALL_SPREAD,
        [a, b],
        spot=Decimal("1500"),
        now=NOW,
        request=_request(),
        rates=RATES,
    )
    assert isinstance(built, Rejection)


def test_analyze_keeps_direction_consistent_and_reports_rejections() -> None:
    quotes = [
        _quote("1450", OptionType.CE, "70", "72"),
        _quote("1500", OptionType.CE, "40", "41"),
        _quote("1550", OptionType.CE, "20", "21"),
        _quote("1600", OptionType.CE, "9", "9.5"),
        _quote("1500", OptionType.PE, "30", "31"),
        _quote("1650", OptionType.CE, "5", "1"),
    ]
    result = analyze(_request(), quotes, spot=Decimal("1505"), now=NOW, rates=RATES)
    kinds = {c.strategy for c in result.candidates}
    assert kinds <= {StrategyKind.LONG_CALL, StrategyKind.BULL_CALL_SPREAD}
    assert StrategyKind.BULL_CALL_SPREAD in kinds
    assert any("crossed" in r.reason for r in result.rejected)
    assert any("BEARISH" not in w and "skipped" in w for w in result.warnings)
    scores = [c.score.total for c in result.candidates]
    assert scores == sorted(scores, reverse=True)
    for candidate in result.candidates:
        for leg in candidate.legs:
            expected = leg.quote.ask if leg.side is Side.BUY else leg.quote.bid
            assert leg.exec_price == expected


def test_no_expiry_in_window_is_a_warning_not_a_fabrication() -> None:
    quotes = [_quote("1500", OptionType.CE, "40", "41")]
    result = analyze(
        _request(min_dte=40, max_dte=60), quotes, spot=Decimal("1500"), now=NOW, rates=RATES
    )
    assert result.candidates == ()
    assert any("No listed expiry" in w for w in result.warnings)
