#!/usr/bin/env python
"""Replay a recorded morning through the opening-range monitor and say what it raised (SW6).

`docs/swing/06` SW6: "Replay harness: `tools/swing/replay.py` feeds a recorded morning
(`bus.last_tick` journal, or a minute-candle CSV) through the strategy and asserts the
signals; the pack ships one synthetic morning fixture."

    cd kite-momentum-rebalancer
    .venv/bin/python ../tools/swing/replay.py ../tools/swing/fixtures/morning-synthetic.csv \\
        --watchlist ../tools/swing/fixtures/morning-synthetic.watchlist.json \\
        --expect ../tools/swing/fixtures/morning-synthetic.expected.json

Exit code 0 when the signals match the expectation exactly (symbol, state, minute, entry,
stop), 1 when they do not, and the diff is printed either way. Without `--expect` it just
prints what the morning raised.

HOW A CANDLE BECOMES TICKS
--------------------------
Kite's ticker delivers ~1 snapshot a second, not a tape, and a minute candle is four numbers.
The replay turns each candle into four ticks — **open, low, high, close** at +0, +15, +30 and
+45 seconds — and that order is the convention the expectations are written against: a break
that happens inside a candle is seen at the candle's high tick, and the low is seen first so
the low of the day the stop reads is the candle's own low. The `ohlc.low` on every tick is the
running low of the session, which is what Kite's full-mode tick carries.

A `bus.last_tick` journal (one JSON tick per line, as the ticker delivered them) is replayed as
recorded — `--journal` — with no synthesis at all.

WHAT IT PROVES
--------------
The strategy is driven with no broker, no bus and no database: the candle source is the CSV,
the store is a list. The verdicts are `baskfy_core.swing.opening_range.evaluate_trigger`'s,
so a replay that raises the expected signals is the whole chain from candle to signal,
minus only the wire.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESK = ROOT / "kite-momentum-rebalancer"
if str(DESK) not in sys.path:
    sys.path.insert(0, str(DESK))

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SwingConfig  # noqa: E402
from baskfy_core.swing.opening_range import Candle  # noqa: E402

from app.strategies.swing_breakout import Signal, SwingBreakout, WatchedName  # noqa: E402

#: The four ticks a candle becomes, and when in the minute each is seen.
TICK_ORDER: tuple[tuple[str, int], ...] = (("open", 0), ("low", 15), ("high", 30), ("close", 45))


# --- the recording -----------------------------------------------------------------------


def read_candles(path: Path) -> dict[int, list[Candle]]:
    """`token,symbol,start,open,high,low,close,volume` → candles per token, in time order."""
    out: dict[int, list[Candle]] = defaultdict(list)
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            out[int(row["token"])].append(
                Candle(
                    start=dt.datetime.fromisoformat(row["start"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(row.get("volume") or 0),
                )
            )
    for candles in out.values():
        candles.sort(key=lambda c: c.start)
    return dict(out)


def ticks_from_candles(candles: dict[int, list[Candle]]) -> list[dict]:
    """Every candle as four ticks, merged across tokens in time order."""
    ticks: list[dict] = []
    for token, series in candles.items():
        running_low: Decimal | None = None
        for candle in series:
            running_low = candle.low if running_low is None else min(running_low, candle.low)
            for name, offset in TICK_ORDER:
                price = getattr(candle, name)
                ticks.append(
                    {
                        "instrument_token": token,
                        "last_price": float(price),
                        "exchange_timestamp": candle.start + dt.timedelta(seconds=offset),
                        "ohlc": {"low": float(running_low)},
                    }
                )
    ticks.sort(key=lambda t: (t["exchange_timestamp"], t["instrument_token"]))
    return ticks


def read_journal(path: Path) -> list[dict]:
    """One JSON tick per line, as `bus.last_tick` recorded it."""
    ticks: list[dict] = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            tick = json.loads(line)
            for key in ("exchange_timestamp", "last_trade_time", "timestamp"):
                if isinstance(tick.get(key), str):
                    tick[key] = dt.datetime.fromisoformat(tick[key])
            ticks.append(tick)
    ticks.sort(key=lambda t: t.get("exchange_timestamp") or t.get("timestamp") or dt.datetime.min)
    return ticks


def read_watchlist(path: Path) -> tuple[dt.date, int | None, list[WatchedName]]:
    payload = json.loads(path.read_text())
    names = [
        WatchedName(
            watch_id=int(entry["watch_id"]),
            instrument_id=int(entry["instrument_id"]),
            symbol=str(entry["symbol"]),
            token=int(entry["token"]),
            setup=Setup(entry["setup"]),
            pivot_high=Decimal(entry["pivot_high"]) if entry.get("pivot_high") else None,
            upper_circuit=Decimal(entry["upper_circuit"]) if entry.get("upper_circuit") else None,
        )
        for entry in payload["names"]
    ]
    return dt.date.fromisoformat(payload["day"]), payload.get("window_minutes"), names


# --- the seams ---------------------------------------------------------------------------


class CsvCandles:
    """The strategy's candle source, backed by the recording."""

    def __init__(self, candles: dict[int, list[Candle]]) -> None:
        self.candles = candles
        self.requests: list[tuple[int, dt.datetime]] = []

    def minute_candles(self, token: int, day: dt.date, until: dt.datetime) -> list[Candle]:
        self.requests.append((token, until))
        return [c for c in self.candles.get(token, []) if c.start.date() == day and c.start <= until]


@dataclass
class ListStore:
    """The strategy's signal store, as a list."""

    signals: list[Signal] = field(default_factory=list)

    def raise_signal(self, signal: Signal) -> None:
        self.signals.append(signal)


# --- the replay --------------------------------------------------------------------------


@dataclass(frozen=True)
class Raised:
    symbol: str
    state: str
    at: str
    entry: str | None
    stop: str | None

    @classmethod
    def of(cls, signal: Signal) -> Raised:
        return cls(
            symbol=signal.watch.symbol,
            state=signal.verdict.state.value,
            at=signal.at.replace(second=0, microsecond=0).isoformat(),
            entry=f"{signal.verdict.entry:.2f}" if signal.verdict.entry is not None else None,
            stop=f"{signal.verdict.stop:.2f}" if signal.verdict.stop is not None else None,
        )

    @classmethod
    def from_json(cls, entry: dict) -> Raised:
        return cls(
            symbol=str(entry["symbol"]),
            state=str(entry["state"]),
            at=str(entry["at"]),
            entry=entry.get("entry"),
            stop=entry.get("stop"),
        )


def replay(
    *,
    ticks: list[dict],
    candles: dict[int, list[Candle]],
    watchlist: list[WatchedName],
    day: dt.date,
    window_minutes: int | None = None,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> list[Signal]:
    """Drive the strategy with the recording. Returns the signals in the order raised."""
    store = ListStore()
    strategy = SwingBreakout(
        None,
        watchlist=watchlist,
        store=store,
        candles=CsvCandles(candles),
        day=day,
        window_minutes=window_minutes,
        config=config,
    )

    async def _drive() -> None:
        for tick in ticks:
            await strategy.on_tick(tick)

    asyncio.run(_drive())
    return store.signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("recording", type=Path, help="minute-candle CSV (or a journal with --journal)")
    parser.add_argument("--watchlist", type=Path, required=True, help="the morning's names, JSON")
    parser.add_argument("--journal", type=Path, help="a bus.last_tick journal to replay instead of synthesising ticks")
    parser.add_argument("--expect", type=Path, help="the expected signals, JSON; exit 1 on a mismatch")
    parser.add_argument("--window", type=int, help="opening-range window in minutes (default: the watchlist's, else 5)")
    args = parser.parse_args(argv)

    day, window, watchlist = read_watchlist(args.watchlist)
    candles = read_candles(args.recording)
    ticks = read_journal(args.journal) if args.journal else ticks_from_candles(candles)
    signals = replay(
        ticks=ticks, candles=candles, watchlist=watchlist, day=day, window_minutes=args.window or window
    )
    raised = [Raised.of(s) for s in signals]

    for r in raised:
        print(f"{r.at[11:16]}  {r.state:<22} {r.symbol:<12} entry={r.entry or '-':>8} stop={r.stop or '-':>8}")
    if not raised:
        print("no signals raised")
    if args.expect is None:
        return 0

    expected = [Raised.from_json(e) for e in json.loads(args.expect.read_text())]
    if raised == expected:
        print(f"\nOK — {len(raised)} signals, exactly as expected")
        return 0
    print("\nMISMATCH")
    for label, rows in (("expected", expected), ("raised", raised)):
        print(f"  {label}:")
        for r in rows:
            print(f"    {r.at[11:16]} {r.state} {r.symbol} entry={r.entry} stop={r.stop}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
