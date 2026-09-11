#!/usr/bin/env python
"""The research's own reading of the tight-close scan, re-implemented (TW2).

    cd decile-blueprint
    uv run python ../tools/twt/twt_scan.py            # report the four numbers
    uv run python ../tools/twt/twt_scan.py --write    # rebuild the committed fixtures
    uv run python ../tools/twt/twt_scan.py --verify   # rebuild and assert byte-identity

**A re-implementation of** ``research/tight-close/tscan.py``, line for line, in its own idiom:
float64 matrices, pandas rolling windows, no ``Decimal``, no Polars, no ``baskfy_core``. It is not
the sleeve and must never be mistaken for it — it exists so that TW2's recall measurement has the
*research's* reading to score, and so that the sleeve's reading, when TW1's core lands, can be
scored by the same scorer and compared against the same answer key.

Why a re-implementation and not an import: ``tscan.py`` and ``tscan_verify.py`` write
``research/tight-close/data/state.pkl`` as a side effect of running, and ``research/`` is the
answer key — read-only, always. Re-implementing also makes the faithfulness *measurable*: this
module is faithful exactly insofar as it reproduces STRATEGY §1's four published numbers, and it
does, to the decimal place the note prints:

    point-in-time   64.9 % recall at 61.5 % precision
    look-ahead      83.1 % recall at 97.8 % precision

**The look-ahead reading is not in the research's code.** ``tscan.py`` has an
``include_current_week`` switch, but ``False`` there means *the three completed weeks before this
one* — a lag, not a look-ahead. The reading STRATEGY §1 describes in prose ("the current week's
*final* close used on every day of that week") exists nowhere in ``research/`` and was rebuilt
here from the sentence. That it lands on 83.1 / 97.8 exactly is the evidence the sentence was read
right (DECISIONS-TW TW2.3).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd
from twt_panel import CHARTINK, PANEL, ResearchPanel, read_panel
from twt_recall import MissReason, MissReasons, score

_Matrix = npt.NDArray[np.float64]
_Mask = npt.NDArray[np.bool_]

#: Where the committed fixtures live. Small, text, and the only thing TW2's recall test reads.
FIXTURES: Final = (
    Path(__file__).resolve().parents[2]
    / "decile-blueprint"
    / "packages"
    / "core"
    / "tests"
    / "fixtures"
    / "twt"
)

# ---------------------------------------------------------------------------------------------
# The research's thresholds, spelled out. These are `tscan.py`'s defaults and `04` §3.1's values;
# they are duplicated here rather than imported from `baskfy_core.twt.config` on purpose, so that
# a change to the sleeve's configuration cannot silently move the *research's* reading. The two
# being equal is a thing `test_twt_lookahead_recall.py` asserts, not a thing this file assumes.
MIN_CLOSE_RAW: Final = 30.0
MIN_VOL_SMA: Final = 10_000.0
VOL_SMA_BARS: Final = 50
TIGHT_PCT: Final = 3.01
TIGHT_WEEKS: Final = 3
LOW_MULTIPLE: Final = 1.3
MONTHS_AGO: Final = 3
ROLLING_MIN_SHARE: Final = 0.9
TURNOVER_AVG_BARS: Final = 20
ENTRY_MIN_SESSIONS_OUT: Final = 5
RESEARCH_MIN_TURNOVER_INR: Final = 2e7

#: ``tscan_verify.py`` drops these rows of Chartink's export: index rows are not instruments.
CHARTINK_EXCLUDED_SECTOR: Final = "Indices"


class Reading(StrEnum):
    """Which close stands for the **current** week (``docs/twt/04`` §3.2, ``01`` §2)."""

    #: Today's close. What a person running the scan at 15:30 sees, and what the sleeve does.
    POINT_IN_TIME = "point_in_time"
    #: The current week's *final* close, on every day of that week. What Chartink's **backtester**
    #: sees, because it evaluates a weekly candle as a completed candle. Look-ahead: on Tuesday it
    #: knows Friday. Never reachable from ``baskfy_core.twt``.
    LOOK_AHEAD = "look_ahead"


@dataclass(frozen=True, slots=True)
class Window:
    """The sessions the recall measurement is taken over — Chartink's export's own span."""

    first: dt.date
    last: dt.date

    def holds(self, session: dt.date) -> bool:
        return self.first <= session <= self.last


def rolling_mean(matrix: _Matrix, bars: int) -> _Matrix:
    """``vbt/scan.py``'s ``_roll(..., "mean")``: a window is valid at 90 % of its bars.

    An illiquid name's occasional no-trade day is tolerated, the way a screener that only sees
    traded bars computes an average (``04`` §2.2, and the research's own comment).
    """
    frame = pd.DataFrame(matrix.T)
    # `vbt/scan.py` writes `int(round(...))`; `round` of a float is already an int.
    minimum = max(2, round(bars * ROLLING_MIN_SHARE))
    rolled = frame.rolling(bars, min_periods=minimum).mean()
    return np.asarray(rolled.to_numpy(), dtype=np.float64).T


def weekly_closes(panel: ResearchPanel, reading: Reading, weeks: int = TIGHT_WEEKS) -> _Matrix:
    """``(weeks, instruments, sessions)`` — the weekly closes ``04`` §3.2's rule reads.

    ``w0`` is today's close under :attr:`Reading.POINT_IN_TIME` and the current week's last close
    under :attr:`Reading.LOOK_AHEAD`; ``w1`` and ``w2`` are the last close of each of the two
    preceding week buckets **the data holds**, which is what a screener sees.
    """
    dates = pd.to_datetime(np.asarray(panel.sessions))
    iso = dates.isocalendar()
    keys = iso.year.to_numpy() * 100 + iso.week.to_numpy()
    closes = pd.DataFrame(panel.close.T, index=dates)
    last_of_week = closes.groupby(keys).last()
    week_ids = last_of_week.index.to_numpy()
    position = np.searchsorted(week_ids, keys)
    weekly = np.asarray(last_of_week.to_numpy(), dtype=np.float64)
    out = np.empty((weeks, *panel.close.shape), dtype=np.float64)
    out[0] = panel.close if reading is Reading.POINT_IN_TIME else weekly[position].T
    for back in range(1, weeks):
        index = position - back
        out[back] = np.where(index[:, None] >= 0, weekly[np.clip(index, 0, None)], np.nan).T
    return out


def month_low_back(panel: ResearchPanel, months: int = MONTHS_AGO) -> _Matrix:
    """The low of the calendar month ``months`` before each session's own month (``04`` §3.3)."""
    dates = pd.to_datetime(np.asarray(panel.sessions))
    keys = dates.year.to_numpy() * 12 + dates.month.to_numpy() - 1
    lows = pd.DataFrame(panel.low.T, index=dates)
    monthly = lows.groupby(keys).min()
    month_ids = monthly.index.to_numpy()
    values = np.asarray(monthly.to_numpy(), dtype=np.float64)
    target = keys - months
    index = np.searchsorted(month_ids, target)
    clipped = np.clip(index, 0, len(month_ids) - 1)
    present = (index < len(month_ids)) & (month_ids[clipped] == target)
    return np.asarray(np.where(present[:, None], values[clipped], np.nan), dtype=np.float64).T


@dataclass(frozen=True, slots=True)
class StateParts:
    """Each line of the scan on its own, so a miss can be attributed to the line that refused it."""

    state: _Mask
    tight: _Mask
    above_month_low: _Mask
    floors: _Mask
    has_bar: _Mask
    has_volume_average: _Mask


def tight_state_parts(panel: ResearchPanel, reading: Reading) -> StateParts:
    """``tscan.tight_state`` — the five lines of ``04`` §3.1 under the given weekly reading."""
    weekly = weekly_closes(panel, reading)
    volume_average = rolling_mean(panel.volume, VOL_SMA_BARS)
    finite = np.isfinite(weekly)
    with np.errstate(invalid="ignore", divide="ignore"):
        # `nanmax` over an all-NaN column is the same answer and a RuntimeWarning; the sentinels
        # give it silently, and `complete` discards every cell they could reach.
        highest = np.max(np.where(finite, weekly, -np.inf), axis=0)
        lowest = np.min(np.where(finite, weekly, np.inf), axis=0)
        complete = finite.all(axis=0)
        tight = complete & (np.abs(highest / lowest - 1) * 100 <= TIGHT_PCT)
        above = panel.close >= month_low_back(panel) * LOW_MULTIPLE
        floors = (
            (panel.close_raw > MIN_CLOSE_RAW)
            & (volume_average >= MIN_VOL_SMA)
            & ~panel.is_etf[:, None]
        )
    as_bool = np.asarray(np.nan_to_num(floors & tight & above, nan=False), dtype=bool)
    return StateParts(
        state=as_bool,
        tight=np.asarray(np.nan_to_num(tight, nan=False), dtype=bool),
        above_month_low=np.asarray(np.nan_to_num(above, nan=False), dtype=bool),
        floors=np.asarray(np.nan_to_num(floors, nan=False), dtype=bool),
        has_bar=np.isfinite(panel.close),
        has_volume_average=np.isfinite(volume_average),
    )


def tight_state(panel: ResearchPanel, reading: Reading) -> _Mask:
    """The state alone. :func:`tight_state_parts` when the individual lines are wanted."""
    return tight_state_parts(panel, reading).state


def diagnose(
    panel: ResearchPanel, missed: Iterable[tuple[dt.date, str]], parts: StateParts
) -> dict[str, int]:
    """Why each missed stock-day is missed, in ``tscan_verify.py``'s own buckets.

    **Explains, never excuses.** Every missed stock-day lands in exactly one bucket and the buckets
    sum to the miss count; nothing here can take one out of the recall denominator.
    """
    row_of = {symbol: row for row, symbol in enumerate(panel.symbols)}
    column_of = {session: column for column, session in enumerate(panel.sessions)}
    counts: dict[str, int] = {reason.value: 0 for reason in MissReason}
    for session, symbol in missed:
        row = row_of.get(symbol)
        column = column_of.get(session)
        if row is None or column is None:
            counts[MissReason.UNKNOWN_SYMBOL.value] += 1
        elif not parts.has_bar[row, column]:
            counts[MissReason.NO_BAR.value] += 1
        elif not parts.has_volume_average[row, column]:
            counts[MissReason.NO_VOLUME_AVERAGE.value] += 1
        elif not parts.tight[row, column]:
            counts[MissReason.NOT_TIGHT.value] += 1
        elif not parts.above_month_low[row, column]:
            counts[MissReason.NOT_ABOVE_MONTH_LOW.value] += 1
        elif not parts.floors[row, column]:
            counts[MissReason.BELOW_A_FLOOR.value] += 1
        else:
            counts[MissReason.UNEXPLAINED.value] += 1
    return {name: count for name, count in counts.items() if count}


def entry_events(state: _Mask, sessions_out: int = ENTRY_MIN_SESSIONS_OUT) -> _Mask:
    """``tscan.entries`` — the first tight session after ``sessions_out`` out of the state.

    **Faithful to the research, including the part TW1 changed.** The counter is seeded at 10,000,
    so a name's *listing day* fires an entry: "was false for five sessions" is taken as a claim
    about five sessions that need not exist. ``04`` §3.4 and DECISIONS-TW TW0.6 make the sleeve
    require the five sessions; the size of that difference is TW2's to measure, and this function
    is the half it is measured against.
    """
    out = np.zeros_like(state)
    was_in = np.zeros(state.shape[0], dtype=bool)
    since_out = np.full(state.shape[0], 10_000, dtype=np.int64)
    for column in range(state.shape[1]):
        today = state[:, column]
        out[:, column] = today & ~was_in & (since_out >= sessions_out)
        since_out = np.where(today, 0, since_out + 1)
        was_in = today
    return out


def turnover_avg(panel: ResearchPanel, bars: int = TURNOVER_AVG_BARS) -> _Matrix:
    """``close_raw x volume``, averaged over ``bars`` sessions — the study's liquidity filter."""
    return rolling_mean(panel.close_raw * panel.volume, bars)


def stock_days(state: _Mask, panel: ResearchPanel, window: Window) -> list[tuple[dt.date, str]]:
    """``(session, symbol)`` for every true cell inside ``window``, sorted."""
    rows, columns = np.nonzero(state)
    sessions = np.asarray(panel.sessions)
    symbols = np.asarray(panel.symbols, dtype=object)
    inside = [
        (sessions[column], str(symbols[row]))
        for row, column in zip(rows, columns, strict=True)
        if window.holds(sessions[column])
    ]
    return sorted(inside)


def chartink_stock_days(path: Path, window: Window) -> list[tuple[dt.date, str]]:
    """Chartink's export, filtered as ``tscan_verify.py`` filters it, inside ``window``."""
    export = pd.read_csv(path, encoding="utf-8-sig")
    export = export[export["Sector"] != CHARTINK_EXCLUDED_SECTOR]
    dates = pd.to_datetime(export["Date"], format="%d-%m-%Y").dt.date
    pairs = [
        (session, str(symbol))
        for session, symbol in zip(dates, export["Symbol"], strict=True)
        if window.holds(session)
    ]
    return sorted(set(pairs))


def chartink_window(path: Path, last_session: dt.date) -> Window:
    """``tscan_verify.py``'s own window: the export's span, truncated to the panel's last bar."""
    export = pd.read_csv(path, encoding="utf-8-sig")
    export = export[export["Sector"] != CHARTINK_EXCLUDED_SECTOR]
    dates = pd.to_datetime(export["Date"], format="%d-%m-%Y").dt.date
    return Window(first=min(dates), last=min(max(dates), last_session))


# ---------------------------------------------------------------------------------------------
# The fixtures: what gets committed, and how it is proved to be re-derivable.

_PAIR_HEADER: Final = "date,symbol\n"


def render_pairs(pairs: Iterable[tuple[dt.date, str]]) -> bytes:
    lines = [_PAIR_HEADER]
    lines += [f"{session.isoformat()},{symbol}\n" for session, symbol in pairs]
    return "".join(lines).encode("utf-8")


def parse_pairs(raw: bytes) -> list[tuple[dt.date, str]]:
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != _PAIR_HEADER.strip():
        raise ValueError(f"a stock-day fixture must start with {_PAIR_HEADER!r}")
    pairs = []
    for line in lines[1:]:
        session, _, symbol = line.partition(",")
        pairs.append((dt.date.fromisoformat(session), symbol))
    return pairs


def read_pairs(path: Path) -> list[tuple[dt.date, str]]:
    """A committed ``*.csv.gz`` stock-day fixture."""
    with gzip.open(path, "rb") as handle:
        return parse_pairs(handle.read())


def _gzip(payload: bytes) -> bytes:
    """Deterministic gzip: no mtime, no filename, so the committed bytes are stable."""
    return gzip.compress(payload, mtime=0)


def build(path: Path = PANEL, chartink: Path = CHARTINK) -> dict[str, bytes]:
    """Every fixture this module owns, as ``{filename: bytes}``. Reads only; writes nothing."""
    panel = read_panel(path)
    window = chartink_window(chartink, panel.sessions[-1])
    answer_key = chartink_stock_days(chartink, window)
    payloads = {"recall_chartink.csv.gz": _gzip(render_pairs(answer_key))}
    counts: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    for reading in Reading:
        parts = tight_state_parts(panel, reading)
        pairs = stock_days(parts.state, panel, window)
        payloads[f"recall_{reading.value}.csv.gz"] = _gzip(render_pairs(pairs))
        counts[reading.value] = len(pairs)
        reasons[reading.value] = diagnose(panel, score(pairs, answer_key).missed, parts)
    manifest = {
        "what": (
            "The stock-days the tight-close scan is true on, inside the window Chartink's own "
            "export covers, under each of the two weekly readings -- plus Chartink's own rows "
            "over the same window. TW2's recall measurement scores these against each other "
            "without needing the 1.7 GB panel or a running core."
        ),
        "source_panel": str(path.relative_to(FIXTURES.parents[5])),
        "source_chartink": str(chartink.relative_to(FIXTURES.parents[5])),
        "rebuild": "uv run python ../tools/twt/twt_scan.py --verify",
        "window": {"first": window.first.isoformat(), "last": window.last.isoformat()},
        "chartink_stock_days": len(answer_key),
        "reading_stock_days": counts,
        "miss_reasons": reasons,
        "panel": {
            "instruments": panel.instruments,
            "sessions": panel.sessions_count,
            "bars": panel.bars,
            "first_session": panel.sessions[0].isoformat(),
            "last_session": panel.sessions[-1].isoformat(),
        },
        "sha256": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(payloads.items())
        },
    }
    payloads["recall_manifest.json"] = (json.dumps(manifest, indent=1) + "\n").encode("utf-8")
    return payloads


def write(directory: Path = FIXTURES, path: Path = PANEL, chartink: Path = CHARTINK) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in build(path, chartink).items():
        (directory / name).write_bytes(payload)
        print(f"wrote {directory / name} ({len(payload)} bytes)")


def verify(directory: Path = FIXTURES, path: Path = PANEL, chartink: Path = CHARTINK) -> bool:
    """Rebuild every fixture from the panel and compare byte for byte with what is committed."""
    ok = True
    for name, payload in build(path, chartink).items():
        target = directory / name
        if not target.exists():
            print(f"MISSING  {target}")
            ok = False
        elif target.read_bytes() != payload:
            print(f"DIFFERS  {target}")
            ok = False
        else:
            print(f"identical {target} ({len(payload)} bytes)")
    return ok


def report(path: Path = PANEL, chartink: Path = CHARTINK) -> None:  # pragma: no cover - operator
    """STRATEGY §1's own numbers, recomputed. The check that this re-implementation is faithful."""
    panel = read_panel(path)
    window = chartink_window(chartink, panel.sessions[-1])
    answer_key = chartink_stock_days(chartink, window)
    print(f"window {window.first} -> {window.last}: {len(answer_key)} Chartink stock-days")
    for reading in Reading:
        parts = tight_state_parts(panel, reading)
        pairs = stock_days(parts.state, panel, window)
        card = score(pairs, answer_key)
        print(f"  {reading.value:14s} {card.render()}")
        print(f"      why missed: {MissReasons(diagnose(panel, card.missed, parts)).render()}")
    state = tight_state(panel, Reading.POINT_IN_TIME)
    events = entry_events(state)
    liquid = np.nan_to_num(turnover_avg(panel) >= RESEARCH_MIN_TURNOVER_INR, nan=False)
    print(f"  state stock-days over the whole history: {int(state.sum())}")
    liquid_events = int((events & liquid).sum())
    print(f"  entry events: {int(events.sum())}  of them >= Rs 2 cr: {liquid_events}")


def main() -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rebuild the committed fixtures")
    parser.add_argument("--verify", action="store_true", help="rebuild and assert byte-identity")
    parsed = parser.parse_args()
    if parsed.write:
        write()
        return 0
    if parsed.verify:
        return 0 if verify() else 1
    report()
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
