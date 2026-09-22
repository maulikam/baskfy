"""OP0's six deferred live Kite reads, as one read-only probe (``docs/options/STATUS.md`` OP0 §4).

    python -m baskfy_worker.options_cli probe          # inside the worker container, on the box

Prints one JSON report. It **places, modifies and cancels nothing**: every call is a read on the
box's shared limiter for its endpoint family (`baskfy_worker.options.reads`), and the one POST —
``basket_order_margins`` — is Kite's margin *calculator*, asked with ``consider_positions=False``
about a fixture basket that is never sent anywhere. The module imports no order path, and a test
scans its source for order verbs.

The six reads (OP0.3):

(a) the earliest date ``historical_data(interval="minute")`` serves for NIFTY 50 and INDIA VIX —
    probed back year by year on a two-week January window, then month by month inside the year
    before the first year that answered;
(b) that the most recently expired NIFTY contract in ``op_contract`` is absent from today's
    ``instruments("NFO")`` and what ``historical_data`` says when asked for it (the error text);
(c) the master's next eight NIFTY expiries: weekday, lot size(s), tick size(s), the modal strike
    step near ATM, and which is the monthly (the last listed in its month — no weekday rule);
(d) ``margins()`` — **shape only**: segment names and field names, never an account figure;
(e) ``quote()`` on five NIFTY options near ATM: the fields, depth levels a side, OI, the
    OI-day high/low, both timestamps, and a verdict on **the OI unit** (``04`` §2.4, OP1.3);
(f) ``basket_order_margins`` for a fixture four-leg iron condor (one lot a leg, MIS).

Each read is independent: one failing is recorded with its error and the rest still run.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from baskfy_core.models.base import JsonObject
from baskfy_core.options.calendar import Contract, kind
from baskfy_core.options.chain import atm, strike_step
from baskfy_core.options.config import OptionType
from baskfy_providers.records import (
    MarginLegRecord,
    MinuteBarRecord,
    OptionContractRecord,
    OptionQuoteRecord,
)
from baskfy_worker.options.collector import SPOT_KEY
from baskfy_worker.options.master import to_contract
from baskfy_worker.options.reads import OptionsKite

#: How far back (a) looks. Kite's minute history is documented from about 2015 (``07`` §1);
#: starting earlier costs a handful of empty calls and cannot miss an earlier start.
EARLIEST_YEAR_PROBED: Final = 2010
#: (c): how many expiries, and (e): how many option quotes.
EXPIRIES_REPORTED: Final = 8
QUOTES_PROBED: Final = 5
#: (c)'s strike-step window: this many strikes either side of ATM.
STEP_WINDOW: Final = 10
#: (f): wings this many strike steps outside the shorts, shorts this many steps outside ATM.
CONDOR_SHORT_STEPS: Final = 4
CONDOR_WING_STEPS: Final = 3


class HistoryProbe(Protocol):
    """``historical_data`` for an arbitrary token, raising Kite's error for an unknown one."""

    def minute_bars(
        self, token: int, start: dt.datetime | dt.date, end: dt.datetime | dt.date
    ) -> list[MinuteBarRecord]: ...


@dataclass(frozen=True, slots=True)
class ProbeInputs:
    """What the probe knows before it calls Kite, read from the database by the CLI."""

    today: dt.date
    #: ``instrument`` rows: symbol -> Kite token (NIFTY 50, INDIA VIX).
    index_tokens: dict[str, int]
    #: The most recently expired NIFTY contract ``op_contract`` holds, if any.
    expired: Contract | None


def _step(label: str, fn: Callable[[], object]) -> JsonObject:
    """Run one read; an exception becomes ``{"error": ...}`` with its type — never swallowed
    silently, always reported, and the next read still runs."""
    try:
        return {"ok": True, "result": fn()}
    except Exception as exc:  # reported in the JSON, not swallowed: the probe's whole job
        return {"ok": False, "read": label, "error_type": type(exc).__name__, "error": str(exc)}


# --- (a) earliest minute history -----------------------------------------------------------------


def earliest_minute(bars: HistoryProbe, token: int, today: dt.date) -> JsonObject:
    """Year by year on 1-14 January, then month by month in the year before the first hit."""
    calls = 0

    def first_bar(lo: dt.date, hi: dt.date) -> dt.datetime | None:
        nonlocal calls
        calls += 1
        got = bars.minute_bars(token, lo, hi)
        return got[0].ts if got else None

    first_year: int | None = None
    first_seen: dt.datetime | None = None
    for year in range(EARLIEST_YEAR_PROBED, today.year + 1):
        hit = first_bar(dt.date(year, 1, 1), min(dt.date(year, 1, 14), today))
        if hit is not None:
            first_year, first_seen = year, hit
            break
    if first_year is None:
        return {"earliest": None, "calls": calls, "note": "no minute bars in any January window"}
    if first_year > EARLIEST_YEAR_PROBED:
        prior = first_year - 1
        for month in range(1, 13):
            lo = dt.date(prior, month, 1)
            hi = dt.date(prior, month, 28)
            hit = first_bar(lo, hi)
            if hit is not None:
                first_seen = hit
                break
    return {
        "earliest": first_seen.isoformat() if first_seen else None,
        "first_january_with_data": first_year,
        "calls": calls,
        "method": "first bar of the first window that answered (Jan 1-14 by year, then by month)",
    }


# --- (b) an expired contract ---------------------------------------------------------------------


def expired_contract(
    bars: HistoryProbe, expired: Contract | None, listed_tokens: set[int]
) -> JsonObject:
    """Is last expiry's contract gone from the master, and what does history say for it?"""
    if expired is None:
        return {
            "note": "op_contract holds no expired NIFTY contract yet (the nightly master has not "
            "seen an expiry pass); re-run the probe after the next Tuesday expiry"
        }
    out: JsonObject = {
        "tradingsymbol": expired.tradingsymbol,
        "instrument_token": expired.instrument_token,
        "expiry": expired.expiry.isoformat(),
        "in_todays_master": expired.instrument_token in listed_tokens,
    }
    try:
        got = bars.minute_bars(
            expired.instrument_token, expired.expiry - dt.timedelta(days=3), expired.expiry
        )
        out["history"] = {"bars": len(got)}
    except Exception as exc:  # the error text IS the finding (06 OP0 (b))
        out["history"] = {"error_type": type(exc).__name__, "error": str(exc)}
    return out


# --- (c) the next eight expiries -----------------------------------------------------------------


def next_expiries(
    contracts: Sequence[Contract], spot: Decimal | None, today: dt.date
) -> JsonObject:
    """Weekday, lot and tick sizes, strike step near ATM and kind, for the next eight expiries."""
    by_expiry: dict[dt.date, list[Contract]] = defaultdict(list)
    for c in contracts:
        if c.expiry >= today:
            by_expiry[c.expiry].append(c)
    rows: list[object] = []
    for expiry in sorted(by_expiry)[:EXPIRIES_REPORTED]:
        group = by_expiry[expiry]
        strikes = [c.strike for c in group]
        step = strike_step(strikes, spot, STEP_WINDOW) if spot is not None else None
        rows.append(
            {
                "expiry": expiry.isoformat(),
                "weekday": expiry.strftime("%A"),
                "kind": kind(contracts, expiry).value,
                "lot_sizes": sorted({c.lot_size for c in group}),
                "tick_sizes": sorted({str(c.tick_size) for c in group}),
                "strike_step_near_atm": str(step) if step is not None else None,
                "contracts": len(group),
            }
        )
    return {"spot_used": str(spot) if spot is not None else None, "expiries": rows}


# --- (e) five option quotes, and the OI unit -----------------------------------------------------


def five_quotes(contracts: Sequence[Contract], spot: Decimal, today: dt.date) -> list[Contract]:
    """The nearest expiry's five CE strikes around ATM (ATM ± 2 steps)."""
    upcoming = sorted({c.expiry for c in contracts if c.expiry >= today})
    if not upcoming:
        return []
    group = [c for c in contracts if c.expiry == upcoming[0] and c.option_type is OptionType.CE]
    step = strike_step((c.strike for c in group), spot, STEP_WINDOW)
    if step is None:
        return []
    centre = atm(spot, step)
    wanted = {centre + step * k for k in range(-2, 3)}
    return sorted((c for c in group if c.strike in wanted), key=lambda c: c.strike)[:QUOTES_PROBED]


def oi_unit_verdict(quotes: Sequence[tuple[OptionQuoteRecord, int]]) -> JsonObject:
    """Kite's OI unit, from divisibility: OI counted in units is always a multiple of the lot.

    If every non-zero OI is a multiple of its lot size, the answer is ``UNITS`` (the chance of
    five lot-counted OIs all landing on multiples of a ~65 lot by accident is ~1 in 10^9). If
    none is, ``LOTS``. Mixed or all-zero is ``UNDETERMINED`` and ``chain.oi_unit`` stays at its
    OP1.3 default until a later probe decides.
    """
    checked = [(q.oi, lot) for q, lot in quotes if q.oi]
    if not checked:
        return {"verdict": "UNDETERMINED", "why": "no non-zero OI in the sample"}
    multiples = [oi % lot == 0 for oi, lot in checked if oi is not None]
    if all(multiples):
        verdict = "UNITS"
    elif not any(multiples):
        verdict = "LOTS"
    else:
        verdict = "UNDETERMINED"
    return {
        "verdict": verdict,
        "sample": [{"oi": oi, "lot_size": lot} for oi, lot in checked],
        "rule": "UNITS if every OI is a multiple of its lot size; LOTS if none is",
    }


def describe_quote(record: OptionQuoteRecord) -> JsonObject:
    """What one quote carries — the fields, depth levels a side, OI and both timestamps."""
    return {
        "key": record.key,
        "fields_present": sorted(
            name
            for name, value in (
                ("last_price", record.last_price),
                ("oi", record.oi),
                ("oi_day_high", record.oi_day_high),
                ("oi_day_low", record.oi_day_low),
                ("timestamp", record.as_of),
                ("last_trade_time", record.last_trade_time),
            )
            if value is not None
        ),
        "depth_levels": {"buy": len(record.bids), "sell": len(record.asks)},
        "best_bid": str(record.bids[0].price) if record.bids else None,
        "best_ask": str(record.asks[0].price) if record.asks else None,
        "volume": record.volume,
        "oi": record.oi,
        "oi_day_high": record.oi_day_high,
        "oi_day_low": record.oi_day_low,
        "timestamp": record.as_of.isoformat() if record.as_of else None,
        "last_trade_time": record.last_trade_time.isoformat() if record.last_trade_time else None,
    }


# --- (f) a fixture condor's basket margin --------------------------------------------------------


def fixture_condor(
    contracts: Sequence[Contract], spot: Decimal, today: dt.date
) -> list[MarginLegRecord]:
    """A symmetric iron condor on the nearest expiry, one lot a leg — **never sent as orders**."""
    upcoming = sorted({c.expiry for c in contracts if c.expiry >= today})
    if not upcoming:
        return []
    group = [c for c in contracts if c.expiry == upcoming[0]]
    step = strike_step((c.strike for c in group), spot, STEP_WINDOW)
    if step is None:
        return []
    centre = atm(spot, step)
    by_key = {(c.strike, c.option_type): c for c in group}
    wanted = (
        (centre + step * (CONDOR_SHORT_STEPS + CONDOR_WING_STEPS), OptionType.CE, "BUY"),
        (centre - step * (CONDOR_SHORT_STEPS + CONDOR_WING_STEPS), OptionType.PE, "BUY"),
        (centre + step * CONDOR_SHORT_STEPS, OptionType.CE, "SELL"),
        (centre - step * CONDOR_SHORT_STEPS, OptionType.PE, "SELL"),
    )
    legs: list[MarginLegRecord] = []
    for strike, option_type, side in wanted:
        c = by_key.get((strike, option_type))
        if c is None:
            return []
        legs.append(
            MarginLegRecord(
                exchange="NFO",
                tradingsymbol=c.tradingsymbol,
                transaction_type="BUY" if side == "BUY" else "SELL",
                quantity=c.lot_size,
            )
        )
    return legs


# --- the probe -----------------------------------------------------------------------------------


def run_probe(kite: OptionsKite, inputs: ProbeInputs) -> JsonObject:
    """All six reads. Each is independent; the report says which answered and which failed."""
    report: JsonObject = {
        "probe": "OP3 options live reads (OP0 §4 a-f)",
        "today": inputs.today.isoformat(),
        "read_only": True,
    }
    master: list[OptionContractRecord] = []

    def read_master() -> object:
        master.extend(kite.general.option_contracts("NIFTY"))
        return {"contracts": len(master)}

    report["master"] = _step("instruments(NFO)", read_master)
    contracts = [to_contract(r) for r in master]
    spot_box: list[Decimal] = []

    def read_spot() -> object:
        got = kite.quotes.option_quotes([SPOT_KEY])
        level = next((q.last_price for q in got if q.key == SPOT_KEY), None)
        if level is not None:
            spot_box.append(level)
        return {"key": SPOT_KEY, "last_price": str(level) if level is not None else None}

    report["spot"] = _step("quote(NIFTY 50)", read_spot)
    spot = spot_box[0] if spot_box else None

    report["a_earliest_minute_history"] = {
        symbol: _step(f"historical_data({symbol})", _bind_earliest(kite, token, inputs.today))
        for symbol, token in sorted(inputs.index_tokens.items())
    } or {"note": "no NIFTY 50 / INDIA VIX rows with a Kite token in `instrument`"}
    listed = {c.instrument_token for c in contracts}
    report["b_expired_contract"] = _step(
        "expired contract", lambda: expired_contract(kite.bars, inputs.expired, listed)
    )
    report["c_next_expiries"] = _step(
        "next expiries", lambda: next_expiries(contracts, spot, inputs.today)
    )
    report["d_margins_shape"] = _step("margins()", kite.general.margins_shape)
    report["e_option_quotes"] = _step(
        "quote(5 options)", lambda: _five(kite, contracts, spot, inputs.today)
    )
    report["f_basket_margins"] = _step(
        "basket_order_margins", lambda: _basket(kite, contracts, spot, inputs.today)
    )
    return report


def _bind_earliest(kite: OptionsKite, token: int, today: dt.date) -> Callable[[], object]:
    def run() -> object:
        return earliest_minute(kite.bars, token, today)

    return run


def _five(
    kite: OptionsKite, contracts: Sequence[Contract], spot: Decimal | None, today: dt.date
) -> object:
    if spot is None:
        return {"note": "no spot, so no ATM to quote around"}
    picked = five_quotes(contracts, spot, today)
    if not picked:
        return {"note": "no listed CE strikes around ATM on the nearest expiry"}
    lots = {f"NFO:{c.tradingsymbol}": c.lot_size for c in picked}
    got = kite.quotes.option_quotes(list(lots))
    return {
        "quotes": [describe_quote(q) for q in got],
        "oi_unit": oi_unit_verdict([(q, lots[q.key]) for q in got if q.key in lots]),
        "asked": len(lots),
        "answered": len(got),
    }


def _basket(
    kite: OptionsKite, contracts: Sequence[Contract], spot: Decimal | None, today: dt.date
) -> object:
    if spot is None:
        return {"note": "no spot, so no ATM to build the fixture condor around"}
    legs = fixture_condor(contracts, spot, today)
    if not legs:
        return {"note": "the fixture condor's four strikes are not all listed"}
    answer = kite.general.basket_order_margins(legs, consider_positions=False)
    return {
        "legs": [
            {"tradingsymbol": leg.tradingsymbol, "side": leg.transaction_type, "qty": leg.quantity}
            for leg in legs
        ],
        "initial_total": str(answer.initial_total) if answer.initial_total is not None else None,
        "final_total": str(answer.final_total) if answer.final_total is not None else None,
        "shape": {key: list(value) for key, value in answer.shape.items()},
        "consider_positions": False,
    }
