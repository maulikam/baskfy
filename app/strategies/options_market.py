"""Read-only Kite adapter for the option research engines.

This module downloads instruments, quotes, candles and basket margin estimates.  It has
no order methods and deliberately does not import :mod:`app.core.gateway`.  The NIFTY
cash index supplies the option-pricing spot; the nearest NIFTY future supplies the
tradable opening-range/VWAP/EMA/volume signal.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .options import (
    BuyingConfig,
    OptionInstrument,
    OptionMarketSnapshot,
    OptionPlan,
    OptionQuote,
    SellingConfig,
)


IST = ZoneInfo("Asia/Kolkata")
INDEX_KEY = "NSE:NIFTY 50"
SESSION_OPEN = dt.time(9, 15)
OPENING_RANGE_END = dt.time(9, 30)
QUOTE_CHUNK = 400                 # Kite quote API permits 500 instruments per request.
CHAIN_SPAN_PCT = 12.0             # Wide enough to retain liquid low-delta wings.


class LiveSnapshotUnavailable(RuntimeError):
    """The broker feed cannot supply a complete, internally consistent snapshot."""


@dataclass(frozen=True)
class LiveSnapshot:
    market: OptionMarketSnapshot
    future_symbol: str
    option_expiry: dt.date
    minute_bars: int

    def as_dict(self, *, include_chain: bool = True) -> dict[str, Any]:
        m = self.market
        out: dict[str, Any] = {
            "as_of": m.as_of.isoformat(),
            "spot": m.spot,
            "signal_price": m.trend_price,
            "signal_source": m.signal_source,
            "vwap": m.vwap,
            "opening_range_high": m.opening_range_high,
            "opening_range_low": m.opening_range_low,
            "ema_fast": m.ema_fast,
            "ema_slow": m.ema_slow,
            "volume_ratio": m.volume_ratio,
            "available_capital": m.available_capital,
            "future_symbol": self.future_symbol,
            "option_expiry": self.option_expiry.isoformat(),
            "minute_bars": self.minute_bars,
            "option_quotes": len(m.quotes),
        }
        if include_chain:
            # The FULL microstructure record: five depth levels, resting quantity at the
            # touch, a receive stamp and last-trade staleness. Top-of-book alone cannot
            # answer the two questions a fill model asks — how long is the queue ahead of
            # me, and is this quote stale — and neither can be reconstructed later from a
            # log that never held them.
            out["chain"] = [
                {"token": q.instrument.token,
                 "expiry": q.instrument.expiry.isoformat(),
                 "lot_size": q.instrument.lot_size,
                 "tick_size": q.instrument.tick_size,
                 **q.microstructure()}
                for q in m.quotes
            ]
        return out


def _aware(value: dt.datetime | None) -> dt.datetime:
    value = value or dt.datetime.now(IST)
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def _row_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _bar_time(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _ema(values: Sequence[float], periods: int) -> float:
    if not values:
        raise LiveSnapshotUnavailable("no closes available for EMA")
    alpha = 2.0 / (periods + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def _chunks(values: Sequence[str], size: int = QUOTE_CHUNK) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def _pick_contracts(rows: Sequence[Mapping[str, Any]], cfg: SellingConfig | BuyingConfig,
                    today: dt.date, spot: float) -> tuple[Mapping[str, Any], dt.date,
                                                          list[OptionInstrument]]:
    futures = [row for row in rows
               if str(row.get("name") or "") == cfg.underlying
               and str(row.get("instrument_type") or "").upper() == "FUT"
               and _row_date(row.get("expiry")) >= today]
    if not futures:
        raise LiveSnapshotUnavailable(f"no live {cfg.underlying} future in NFO dump")
    future = min(futures, key=lambda row: _row_date(row.get("expiry")))

    expiries = sorted({_row_date(row.get("expiry")) for row in rows
                       if str(row.get("name") or "") == cfg.underlying
                       and str(row.get("instrument_type") or "").upper() in ("CE", "PE")
                       and cfg.min_dte_days <= (_row_date(row.get("expiry")) - today).days
                       <= cfg.max_dte_days})
    if not expiries:
        raise LiveSnapshotUnavailable(
            f"no {cfg.underlying} option expiry between {cfg.min_dte_days} and "
            f"{cfg.max_dte_days} DTE")
    expiry = expiries[0]
    span = spot * CHAIN_SPAN_PCT / 100.0
    options = []
    for row in rows:
        if str(row.get("name") or "") != cfg.underlying:
            continue
        if str(row.get("instrument_type") or "").upper() not in ("CE", "PE"):
            continue
        if _row_date(row.get("expiry")) != expiry:
            continue
        strike = float(row.get("strike") or 0.0)
        if abs(strike - spot) <= span:
            options.append(OptionInstrument.from_kite(row))
    if not options:
        raise LiveSnapshotUnavailable("selected expiry has no strikes near live spot")
    if len(options) > 500:
        # Keep the closest contracts while retaining both option kinds.  The quote API's
        # documented request limit is 500, and one coherent timestamp is preferable to
        # silently pulling an unbounded chain.
        options.sort(key=lambda ins: (abs(ins.strike - spot), ins.kind))
        options = options[:500]
    return future, expiry, options


def _signals(rows: Sequence[Mapping[str, Any]], as_of: dt.datetime) -> dict[str, float]:
    completed = []
    for row in rows:
        stamp = _bar_time(row["date"])
        if stamp.date() != as_of.date() or not SESSION_OPEN <= stamp.time():
            continue
        if stamp + dt.timedelta(minutes=1) <= as_of:
            completed.append((stamp, row))
    completed.sort(key=lambda item: item[0])
    opening = [row for stamp, row in completed if stamp.time() < OPENING_RANGE_END]
    if len(opening) < 15:
        raise LiveSnapshotUnavailable("15 completed opening-range minutes are unavailable")
    if len(completed) < 20:
        raise LiveSnapshotUnavailable("at least 20 completed future candles are required")

    volumes = [float(row.get("volume") or 0.0) for _, row in completed]
    typical = [(float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
               for _, row in completed]
    total_volume = sum(volumes)
    if total_volume <= 0:
        raise LiveSnapshotUnavailable("future candles contain no traded volume")
    vwap = sum(price * volume for price, volume in zip(typical, volumes)) / total_volume

    # Five-minute volume blocks aligned to 09:15.  Only full blocks are compared, so an
    # in-progress candle cannot manufacture a weak-volume rejection or false breakout.
    blocks: dict[int, list[float]] = {}
    open_minutes = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
    for stamp, row in completed:
        minute = stamp.hour * 60 + stamp.minute
        bucket = (minute - open_minutes) // 5
        blocks.setdefault(bucket, []).append(float(row.get("volume") or 0.0))
    full = [(bucket, sum(vals)) for bucket, vals in sorted(blocks.items()) if len(vals) == 5]
    if len(full) < 4:
        raise LiveSnapshotUnavailable("four full five-minute volume blocks are required")
    current_volume = full[-1][1]
    baseline_values = [value for _, value in full[-7:-1] if value > 0]
    if not baseline_values:
        raise LiveSnapshotUnavailable("future volume baseline is zero")
    volume_ratio = current_volume / statistics.median(baseline_values)

    closes = [float(row["close"]) for _, row in completed]
    return {
        "vwap": vwap,
        "opening_range_high": max(float(row["high"]) for row in opening),
        "opening_range_low": min(float(row["low"]) for row in opening),
        "ema_fast": _ema(closes, 5),
        "ema_slow": _ema(closes, 13),
        "volume_ratio": volume_ratio,
        "minute_bars": float(len(completed)),
    }


def build_live_snapshot(kite, cfg: SellingConfig | BuyingConfig, *,
                        as_of: dt.datetime | None = None) -> LiveSnapshot:
    """Build one read-only live snapshot from an authenticated :class:`app.Kite`.

    The method makes no assumptions about expiry weekday, strike interval, lot size or
    tick size.  Every one is resolved from the current NFO instrument dump.
    """
    now = _aware(as_of)
    # One stamp for the whole batch: the quotes arrive in a single response,
    # so per-quote wall-clock times would imply a precision that is not there.
    received = dt.datetime.now(IST)
    kc = kite.kc
    rows = list(kc.instruments("NFO") or [])

    first = kc.quote([INDEX_KEY])
    spot_row = first.get(INDEX_KEY) or {}
    spot = float(spot_row.get("last_price") or 0.0)
    if spot <= 0:
        raise LiveSnapshotUnavailable(f"{INDEX_KEY} has no live last price")

    future, expiry, instruments = _pick_contracts(rows, cfg, now.date(), spot)
    future_symbol = str(future["tradingsymbol"])
    future_key = f"NFO:{future_symbol}"
    future_quote = (kc.quote([future_key]).get(future_key) or {})
    signal_price = float(future_quote.get("last_price") or 0.0)
    if signal_price <= 0:
        raise LiveSnapshotUnavailable(f"{future_key} has no live last price")

    option_rows: dict[str, Mapping[str, Any]] = {}
    keys = [f"NFO:{ins.symbol}" for ins in instruments]
    for chunk in _chunks(keys):
        option_rows.update(kc.quote(chunk) or {})
    quotes = []
    for ins in instruments:
        row = option_rows.get(f"NFO:{ins.symbol}")
        if row:
            quotes.append(OptionQuote.from_kite(ins, row, received_at=received))
    if not quotes:
        raise LiveSnapshotUnavailable("Kite returned no quotes for the selected chain")

    session_start = dt.datetime.combine(now.date(), SESSION_OPEN, tzinfo=IST)
    candles = kc.historical_data(int(future["instrument_token"]), session_start, now,
                                 "minute", continuous=False, oi=False)
    signal = _signals(candles or [], now)
    capital = float(kite.available_cash())
    market = OptionMarketSnapshot(
        as_of=now,
        spot=spot,
        signal_price=signal_price,
        signal_source=f"nearest NIFTY future ({future_symbol})",
        vwap=float(signal["vwap"]),
        opening_range_high=float(signal["opening_range_high"]),
        opening_range_low=float(signal["opening_range_low"]),
        ema_fast=float(signal["ema_fast"]),
        ema_slow=float(signal["ema_slow"]),
        volume_ratio=float(signal["volume_ratio"]),
        available_capital=capital,
        quotes=tuple(quotes),
    )
    market.validate()
    return LiveSnapshot(market=market, future_symbol=future_symbol,
                        option_expiry=expiry, minute_bars=int(signal["minute_bars"]))


def basket_margin_estimate(kc, plan: OptionPlan) -> Mapping[str, Any]:
    """Ask Kite for the read-only basket margin; this cannot place an order."""
    params = [
        {
            "exchange": leg.instrument.exchange,
            "tradingsymbol": leg.instrument.symbol,
            "transaction_type": leg.side,
            "variety": "regular",
            "product": plan.product,
            "order_type": "LIMIT",
            "quantity": leg.quantity,
            "price": leg.limit_price,
        }
        for leg in plan.entry_legs
    ]
    return kc.basket_order_margins(params, consider_positions=True, mode="compact")
