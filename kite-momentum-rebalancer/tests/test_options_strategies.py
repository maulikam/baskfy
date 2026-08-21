from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.strategies.options import (
    BUY,
    CE,
    PE,
    BreakoutBuyerPlanner,
    IronCondorPlanner,
    OptionInstrument,
    OptionMarketSnapshot,
    OptionPlanRejected,
    OptionQuote,
    black_scholes_price,
    evaluate_exit,
    implied_volatility,
    instruments_from_kite,
)


EXPIRY = dt.date(2026, 8, 18)
NOW = dt.datetime(2026, 8, 17, 10, 15)


def quote(strike, kind, bid, ask, delta, *, volume=20_000, oi=100_000, lot=65):
    symbol = f"NIFTY26AUG{int(strike)}{kind}"
    ins = OptionInstrument(token=abs(hash((strike, kind))) % 10_000_000,
                           symbol=symbol, underlying="NIFTY", expiry=EXPIRY,
                           strike=float(strike), kind=kind, lot_size=lot, tick_size=0.05)
    return OptionQuote(ins, float(bid), float(ask), (bid + ask) / 2,
                       volume, oi, delta=delta)


def chain():
    return (
        quote(23600, PE, 24, 25, -0.05),
        quote(23800, PE, 68, 69, -0.16),
        quote(24000, PE, 150, 152, -0.50),
        quote(24000, CE, 148, 150, 0.60),
        quote(24200, CE, 70, 71, 0.16),
        quote(24400, CE, 25, 26, 0.05),
    )


def market(*, spot=24000, signal_price=None, vwap=23995, high=24030, low=23970,
           fast=24010, slow=23990, volume_ratio=1.5, capital=1_000_000):
    return OptionMarketSnapshot(as_of=NOW, spot=spot, vwap=vwap,
                                opening_range_high=high, opening_range_low=low,
                                ema_fast=fast, ema_slow=slow, volume_ratio=volume_ratio,
                                available_capital=capital, quotes=chain(),
                                signal_price=signal_price)


def test_live_contract_metadata_is_not_hard_coded():
    rows = [
        {"instrument_token": 1, "tradingsymbol": "NIFTY26AUG24000CE", "name": "NIFTY",
         "expiry": EXPIRY, "strike": 24000, "instrument_type": "CE",
         "lot_size": 65, "tick_size": 0.05, "exchange": "NFO"},
        {"instrument_token": 2, "tradingsymbol": "BANKNIFTY26AUG50000CE",
         "name": "BANKNIFTY", "expiry": EXPIRY, "strike": 50000,
         "instrument_type": "CE", "lot_size": 30, "tick_size": 0.05},
    ]
    got = instruments_from_kite(rows)
    assert len(got) == 1 and got[0].lot_size == 65 and got[0].expiry == EXPIRY


def test_iron_condor_is_defined_risk_and_buys_wings_first():
    plan = IronCondorPlanner().plan(market())
    assert plan.strategy == "intraday_defined_risk_iron_condor"
    assert [x.side for x in plan.entry_legs] == ["BUY", "BUY", "SELL", "SELL"]
    assert [x.role for x in plan.entry_legs[:2]] == ["call_wing", "put_wing"]
    assert all(x.instrument.lot_size == 65 for x in plan.entry_legs)
    assert plan.max_loss <= 1_000_000 * 0.0075
    assert plan.entry_credit > 0 and plan.entry_debit == 0
    assert plan.paper_only is True and plan.product == "MIS"
    assert all(x.limit_price is None for x in plan.exit_legs), "exit must demand fresh quotes"


def test_iron_condor_rejects_a_trending_market():
    with pytest.raises(OptionPlanRejected, match="TRENDING"):
        IronCondorPlanner().plan(market(spot=24200, vwap=24000))


def test_iron_condor_rejects_capital_that_cannot_hold_one_defined_risk_lot():
    with pytest.raises(OptionPlanRejected, match="INSUFFICIENT_CAPITAL"):
        IronCondorPlanner().plan(market(capital=100_000))


def test_breakout_buyer_requires_price_vwap_ema_and_volume_agreement():
    with pytest.raises(OptionPlanRejected, match="NO_BREAKOUT"):
        BreakoutBuyerPlanner().plan(market())
    with pytest.raises(OptionPlanRejected, match="WEAK_VOLUME"):
        BreakoutBuyerPlanner().plan(market(spot=24080, high=24050, vwap=24000,
                                                  volume_ratio=1.0))


def test_breakout_buyer_selects_a_long_call_and_leaves_upside_uncapped():
    snap = market(spot=24080, high=24050, low=23950, vwap=24000,
                  fast=24040, slow=23990)
    plan = BreakoutBuyerPlanner().plan(snap)
    assert len(plan.entry_legs) == 1
    assert plan.entry_legs[0].side == BUY and plan.entry_legs[0].instrument.kind == CE
    assert plan.entry_legs[0].instrument.strike <= snap.spot
    assert plan.stop_loss == pytest.approx(plan.entry_debit * 0.25)
    assert plan.diagnostics["trail_activation_pct"] == 15.0
    assert plan.diagnostics["trail_drawdown_pct"] == 8.0
    assert plan.target_profit == pytest.approx(plan.entry_debit * 0.15)


def test_cash_spot_prices_options_while_nearest_future_drives_signal():
    snap = market(spot=24000, signal_price=24080, high=24050, low=23950,
                  vwap=24000, fast=24040, slow=23990)
    plan = BreakoutBuyerPlanner().plan(snap)
    assert plan.entry_legs[0].instrument.kind == CE
    assert plan.diagnostics["spot"] == 24000
    assert plan.diagnostics["signal_price"] == 24080


def test_buying_profit_target_activates_a_trail_instead_of_forcing_an_exit():
    plan = BreakoutBuyerPlanner().plan(
        market(spot=24080, high=24050, low=23950, vwap=24000,
               fast=24040, slow=23990))
    leg = plan.entry_legs[0]
    at_twenty = {leg.instrument.symbol: leg.limit_price * 1.20}
    keep = evaluate_exit(plan, at_twenty, dt.time(11, 0), peak_return_pct=20.0)
    assert keep.exit is False
    at_eleven = {leg.instrument.symbol: leg.limit_price * 1.11}
    trail = evaluate_exit(plan, at_eleven, dt.time(11, 5), peak_return_pct=20.0)
    assert trail.exit is True and trail.reason == "TRAILING_PROFIT"


def test_every_plan_has_a_hard_intraday_square_off():
    plan = IronCondorPlanner().plan(market())
    prices = {x.instrument.symbol: float(x.limit_price) for x in plan.entry_legs}
    decision = evaluate_exit(plan, prices, dt.time(15, 12), spot=24000)
    assert decision.exit is True and decision.reason == "INTRADAY_SQUARE_OFF"


def test_black_scholes_implied_volatility_round_trip():
    price = black_scholes_price(24000, 24000, 7 / 365, 0.06, 0.18, CE)
    iv = implied_volatility(price, 24000, 24000, 7 / 365, 0.06, CE)
    assert iv == pytest.approx(0.18, rel=1e-6)


def test_option_config_builders_validate_the_environment_defaults():
    from app import config as C
    assert C.option_selling_config().monthly_return_benchmark_pct == 5.0
    assert C.option_buying_config().target_return_benchmark_pct == 15.0


def test_nfo_mis_needs_both_options_and_intraday_gates(monkeypatch):
    from app import config as C
    from app.core.gateway import OrderGateway
    from app.core.risk import RiskConfig, RiskManager

    class RecordingKC:
        def __init__(self):
            self.sent = []

        def place_order(self, **kwargs):
            self.sent.append(kwargs)
            return "O1"

    kc = RecordingKC()
    gw = OrderGateway(kc, RiskManager(RiskConfig(max_position_value=10_000_000)))
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "OPTIONS_ENABLED", True)
    monkeypatch.setattr(C, "INTRADAY_ENABLED", False)
    blocked = asyncio.run(gw.place(symbol="NIFTY26AUG24000CE", qty=65, side="BUY",
                                   product="MIS", exchange="NFO", price=150.05,
                                   tick_size=0.05))
    assert blocked["status"] == "BLOCKED" and not kc.sent

    monkeypatch.setattr(C, "INTRADAY_ENABLED", True)
    placed = asyncio.run(gw.place(symbol="NIFTY26AUG24000CE", qty=65, side="BUY",
                                  product="MIS", exchange="NFO", price=150.05,
                                  tick_size=0.05))
    assert placed["status"] == "PLACED"
    assert kc.sent[0]["price"] == 150.05


def test_read_only_live_adapter_uses_dynamic_future_and_complete_volume_blocks():
    from app.strategies.options_market import build_live_snapshot

    future_expiry = dt.date(2026, 8, 27)
    instrument_rows = [
        {"instrument_token": 999, "tradingsymbol": "NIFTY26AUGFUT", "name": "NIFTY",
         "expiry": future_expiry, "strike": 0, "instrument_type": "FUT",
         "lot_size": 65, "tick_size": 0.05, "exchange": "NFO"},
    ]
    for q in chain():
        ins = q.instrument
        instrument_rows.append({
            "instrument_token": ins.token, "tradingsymbol": ins.symbol, "name": "NIFTY",
            "expiry": ins.expiry, "strike": ins.strike, "instrument_type": ins.kind,
            "lot_size": ins.lot_size, "tick_size": ins.tick_size, "exchange": "NFO",
        })

    option_quotes = {}
    for q in chain():
        option_quotes[f"NFO:{q.instrument.symbol}"] = {
            "last_price": q.last, "volume": q.volume, "oi": q.oi,
            "depth": {"buy": [{"price": q.bid}], "sell": [{"price": q.ask}]},
        }

    candles = []
    start = dt.datetime(2026, 8, 17, 9, 15)
    for minute in range(60):
        close = 24050 + minute
        # The final complete five-minute block is twice the normal volume.
        volume = 200 if 55 <= minute < 60 else 100
        candles.append({"date": start + dt.timedelta(minutes=minute),
                        "open": close - 1, "high": close + 2, "low": close - 2,
                        "close": close, "volume": volume})

    class ReadOnlyKC:
        def __init__(self):
            self.margin_calls = []

        def instruments(self, exchange):
            assert exchange == "NFO"
            return instrument_rows

        def quote(self, keys):
            rows = {"NSE:NIFTY 50": {"last_price": 24000},
                    "NFO:NIFTY26AUGFUT": {"last_price": 24110}, **option_quotes}
            return {key: rows[key] for key in keys if key in rows}

        def historical_data(self, token, frm, to, interval, continuous=False, oi=False):
            assert token == 999 and interval == "minute"
            return candles

    class ReadOnlyKite:
        def __init__(self):
            self.kc = ReadOnlyKC()

        def available_cash(self):
            return 1_000_000

    live = build_live_snapshot(ReadOnlyKite(), IronCondorPlanner().cfg,
                               as_of=dt.datetime(2026, 8, 17, 10, 15))
    assert live.future_symbol == "NIFTY26AUGFUT"
    assert live.option_expiry == EXPIRY
    assert live.market.spot == 24000
    assert live.market.signal_price == 24110
    assert live.market.signal_source.endswith("(NIFTY26AUGFUT)")
    assert live.market.volume_ratio == pytest.approx(2.0)
    assert live.market.quotes[0].instrument.lot_size == 65
