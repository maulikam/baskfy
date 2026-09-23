#!/usr/bin/env python
"""Write the OP9 replay fixtures and their expected exits (`docs/options/06` OP9).

    cd kite-momentum-rebalancer
    .venv/bin/python ../tools/options/make_fixtures.py

Each fixture is one JSON file under `tools/options/fixtures/`: the trading day, one open position
(what `op_position` + its plan carry), NIFTY 50 minute bars, and each leg's bid/ask every minute —
priced by Black-76 (`baskfy_core.options.greeks.black76_price`) at a flat vol on the spot, with a
spread of 1 % either side rounded to the 0.05 tick. Deterministic: re-running writes identical
files.

THE EXPECTATION IS NOT THE REPLAY'S. `expected(...)` below applies each rule's text directly to the
fixture's numbers — condor §7's `D ≤ 0.50 × C`, `04` §4.5's 45-minute time stop, §5.3's `V ≥ 0.80 ×
width`, §8.5's 30 silent seconds after 14:00 — minute by minute, with no call into
`baskfy_core.options.exits`. The replay test then asserts the monitor raised exactly that. A bug
in the monitor's composition therefore cannot also be a bug in its answer key.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from baskfy_core.options.config import OptionType
from baskfy_core.options.greeks import black76_price, year_fraction

HERE = Path(__file__).resolve().parent / "fixtures"
SETTLE = dt.time(15, 30)
TICK = Decimal("0.05")
LOT = 65


def ts(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm))


def minutes(day: dt.date, start: tuple[int, int], end: tuple[int, int]) -> list[dt.datetime]:
    out, t = [], ts(day, *start)
    while t <= ts(day, *end):
        out.append(t)
        t += dt.timedelta(minutes=1)
    return out


def to_tick(value: float, up: bool) -> Decimal:
    steps = Decimal(str(value)) / TICK
    whole = steps.to_integral_value(rounding="ROUND_CEILING" if up else "ROUND_FLOOR")
    return (whole * TICK).quantize(Decimal("0.01"))


def quote(spot: Decimal, strike: int, kind: OptionType, at: dt.datetime, expiry: dt.date,
          vol: float) -> tuple[Decimal, Decimal]:
    years = max(year_fraction(at, expiry, SETTLE), 1e-6)
    price = black76_price(float(spot), float(strike), years, 0.0, vol, kind)
    bid = max(to_tick(price * 0.99, up=False), TICK)
    ask = max(to_tick(price * 1.01, up=True), bid + TICK)
    return bid, ask


def zigzag_path(day: dt.date, centre: str, amp: str) -> dict[dt.datetime, Decimal]:
    c, a = Decimal(centre), Decimal(amp)
    return {t: c + (a if i % 2 == 0 else -a) for i, t in enumerate(minutes(day, (9, 15), (15, 29)))}


def bars_of(path: dict[dt.datetime, Decimal]) -> list[list[str]]:
    rows, prev = [], None
    for t, close in sorted(path.items()):
        open_ = prev if prev is not None else close
        rows.append([t.isoformat(), str(open_), str(max(open_, close) + 2), str(min(open_, close) - 2),
                     str(close)])
        prev = close
    return rows


def legs_of(path: dict[dt.datetime, Decimal], legs: list[dict], expiry: dt.date, vol: float,
            start: dt.datetime) -> list[list[str]]:
    rows = []
    for t, spot in sorted(path.items()):
        if t < start:
            continue
        for leg in legs:
            kind = OptionType.CE if leg["role"].endswith("CALL") else OptionType.PE
            bid, ask = quote(spot, int(leg["strike"]), kind, t, expiry, vol)
            rows.append([t.isoformat(), str(leg["instrument_token"]), str(bid), str(ask)])
    return rows


def fill_prices(path, legs, expiry, vol, at):
    """Entry fills at the conservative side: longs at the ask, shorts at the bid."""
    spot = path[at]
    out = []
    for leg in legs:
        kind = OptionType.CE if leg["role"].endswith("CALL") else OptionType.PE
        bid, ask = quote(spot, int(leg["strike"]), kind, at, expiry, vol)
        out.append({**leg, "quantity": LOT, "fill_price": str(ask if leg["role"].startswith("LONG") else bid)})
    return out


# --- the independent answer key ------------------------------------------------------------------


def _events(fixture: dict) -> list[tuple[dt.datetime, str, object]]:
    """The tick timeline the replay drives, rebuilt here from the fixture: each index minute's
    open/low/high/close at +0/15/30/45 s (none after `feed_stops`), each leg quote at +50 s."""
    out: list[tuple[dt.datetime, str, object]] = []
    stops = fixture.get("feed_stops")
    stop_at = dt.datetime.fromisoformat(stops) if stops else None
    for t_iso, o, h, low, c in fixture["index"]:
        t = dt.datetime.fromisoformat(t_iso)
        for sec, price in ((0, o), (15, low), (30, h), (45, c)):
            at = t + dt.timedelta(seconds=sec)
            if stop_at is None or at < stop_at:
                out.append((at, "index", Decimal(price)))
    for t_iso, token, bid, ask in fixture["legs"]:
        at = dt.datetime.fromisoformat(t_iso) + dt.timedelta(seconds=50)
        out.append((at, "leg", (int(token), Decimal(bid), Decimal(ask))))
    return sorted(out, key=lambda e: (e[0], e[1] == "leg"))


def expected(fixture: dict) -> list[dict]:  # noqa: PLR0912 - one branch per rule, as written
    """At every tick, the first rule that fires — each rule's text, applied to the numbers.

    Staleness (`04` §8.5): a profit rule (PROFIT, TARGET) needs every leg quoted within 15 s;
    the time stop, the hard exit and the feed rule do not. The feed is lost after 14:00 once no
    index tick has come for more than 30 s.
    """
    pos = fixture["position"]
    legs = {int(lg["instrument_token"]): lg for lg in pos["legs"]}
    entry = Decimal(pos["entry_points"])
    opened = dt.datetime.fromisoformat(pos["opened_at"])
    start = dt.datetime.fromisoformat(fixture.get("start") or pos["opened_at"])
    marks: dict[int, tuple[Decimal, Decimal, dt.datetime]] = {}
    spot: Decimal | None = None
    last_index: dt.datetime | None = None
    for at, kind, payload in _events(fixture):
        if at < start:
            continue
        if kind == "index":
            spot, last_index = payload, at
        else:
            token, bid, ask = payload
            marks[token] = (bid, ask, at)
        if last_index is not None and at.time() >= dt.time(14, 0) \
                and (at - last_index).total_seconds() > 30:
            return [{"session_id": pos["session_id"], "code": "HARD_EXIT", "reason": "FEED_LOST",
                     "at": at.isoformat()}]
        if len(marks) < len(legs) or spot is None:
            continue
        fresh = all((at - m[2]).total_seconds() <= 15 for m in marks.values())
        code = None
        if pos["structure"] == "IRON_CONDOR":
            cost = sum((marks[k][1] if not lg["role"].startswith("LONG") else -marks[k][0])
                       for k, lg in legs.items())
            shorts = {lg["role"]: Decimal(lg["strike"]) for lg in legs.values()}
            if spot >= shorts["SHORT_CALL"] or spot <= shorts["SHORT_PUT"]:
                code = "STRIKE_TOUCH"
            elif cost >= Decimal("1.50") * entry:
                code = "STOP"
            elif fresh and cost <= Decimal("0.50") * entry:
                code = "PROFIT"
        elif pos["structure"] == "LONG_OPTION":
            (k,) = legs
            bid = marks[k][0]
            if bid <= entry * Decimal("0.70"):
                code = "STOP"
            elif fresh and bid >= entry * Decimal("1.60"):
                code = "TARGET"
            elif at - opened >= dt.timedelta(minutes=45) and bid < entry * Decimal("1.10"):
                code = "TIME_STOP"
        else:
            value = sum((marks[k][0] if lg["role"].startswith("LONG") else -marks[k][1])
                        for k, lg in legs.items())
            if value <= Decimal("0.50") * entry:
                code = "STOP"
            elif fresh and value >= Decimal("0.80") * Decimal(pos["width_points"]):
                code = "TARGET"
        if code is not None:
            return [{"session_id": pos["session_id"], "code": code, "reason": "RULE",
                     "at": at.isoformat()}]
    return []


# --- the scenarios -------------------------------------------------------------------------------


def condor_fixture(name: str, opened: tuple[int, int], *, start: tuple[int, int] | None = None,
                   feed_stops: tuple[int, int] | None = None, near: bool = False) -> dict:
    """The quiet monthly expiry (27 Oct 2026). `near` puts the shorts 50 points out rather than
    150, so a condor opened after midday still carries a credit worth managing."""
    day = dt.date(2026, 10, 27)  # the monthly expiry
    path = zigzag_path(day, "25010", "8")
    k = (24950, 25050, 24850, 25150) if near else (24850, 25150, 24750, 25250)
    legs = [
        {"role": "LONG_PUT", "strike": str(k[2]), "instrument_token": 9001},
        {"role": "LONG_CALL", "strike": str(k[3]), "instrument_token": 9002},
        {"role": "SHORT_PUT", "strike": str(k[0]), "instrument_token": 9003},
        {"role": "SHORT_CALL", "strike": str(k[1]), "instrument_token": 9004},
    ]
    at = ts(day, *opened)
    filled = fill_prices(path, legs, day, 0.14, at)
    credit = sum(Decimal(lg["fill_price"]) * (1 if lg["role"].startswith("SHORT") else -1) for lg in filled)
    return {
        "name": name,
        "day": day.isoformat(),
        "position": {
            "session_id": 1, "sleeve": "O1M", "structure": "IRON_CONDOR", "entry_points": str(credit),
            "opened_at": at.isoformat(), "risk_budget_inr": "50000", "legs": filled,
        },
        "index": bars_of(path),
        "legs": legs_of(path, legs, day, 0.14, at),
        "start": ts(day, *start).isoformat() if start else None,
        "feed_stops": ts(day, *feed_stops).isoformat() if feed_stops else None,
    }


def o2_fixture() -> dict:
    day, expiry = dt.date(2026, 10, 19), dt.date(2026, 10, 20)
    path = {t: Decimal("25020") + (Decimal("10") if i % 2 == 0 else Decimal("-10"))
            for i, t in enumerate(minutes(day, (9, 15), (9, 59)))}
    path |= {t: Decimal("25045") + (Decimal("3") if i % 2 == 0 else Decimal("-3"))
             for i, t in enumerate(minutes(day, (10, 0), (15, 29)))}
    legs = [{"role": "LONG_CALL", "strike": "25000", "instrument_token": 9101}]
    at = ts(day, 10, 6)
    filled = fill_prices(path, legs, expiry, 0.14, at)
    return {
        "name": "o2-time-stop", "day": day.isoformat(),
        "position": {
            "session_id": 2, "sleeve": "O2", "structure": "LONG_OPTION",
            "entry_points": filled[0]["fill_price"], "opened_at": at.isoformat(),
            "risk_budget_inr": "50000", "direction": "UP", "range_high": "25032",
            "range_low": "25008", "legs": filled,
        },
        "index": bars_of(path), "legs": legs_of(path, legs, expiry, 0.14, at),
        "start": None, "feed_stops": None,
    }


def o3a_fixture() -> dict:
    day = dt.date(2026, 10, 20)
    first = [Decimal("25000") + Decimal(3) * (i + 1) for i in range(60)]
    rest = [first[-1] + Decimal(6) * (i + 1) for i in range(len(minutes(day, (10, 15), (15, 29))))]
    path = dict(zip(minutes(day, (9, 15), (15, 29)), first + rest, strict=True))
    legs = [
        {"role": "LONG_CALL", "strike": "25200", "instrument_token": 9201},
        {"role": "SHORT_CALL", "strike": "25300", "instrument_token": 9202},
    ]
    at = ts(day, 10, 22)
    filled = fill_prices(path, legs, day, 0.14, at)
    debit = Decimal(filled[0]["fill_price"]) - Decimal(filled[1]["fill_price"])
    return {
        "name": "o3a-target", "day": day.isoformat(),
        "position": {
            "session_id": 3, "sleeve": "O3A", "structure": "DEBIT_SPREAD", "entry_points": str(debit),
            "opened_at": at.isoformat(), "risk_budget_inr": "50000", "direction": "UP",
            "range_high": "25182", "range_low": "24998", "width_points": "100", "legs": filled,
        },
        "index": bars_of(path), "legs": legs_of(path, legs, day, 0.14, at),
        "start": None, "feed_stops": None,
    }


def main() -> int:
    HERE.mkdir(parents=True, exist_ok=True)
    scenarios = [
        condor_fixture("o1-profit", (10, 5)),
        condor_fixture("o1-restart-1130", (10, 5), start=(11, 30)),
        condor_fixture("o1-feed-lost", (13, 30), feed_stops=(14, 5), near=True),
        o2_fixture(),
        o3a_fixture(),
    ]
    for fx in scenarios:
        (HERE / f"{fx['name']}.json").write_text(json.dumps(fx, indent=1) + "\n")
        (HERE / f"{fx['name']}.expected.json").write_text(json.dumps(expected(fx), indent=1) + "\n")
        print(fx["name"], expected(fx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
