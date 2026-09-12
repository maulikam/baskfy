#!/usr/bin/env python
"""Would the TWT detector, if it ran, name the stocks Chartink's screen names? (leaf 6)

    cd decile-blueprint
    uv run python ../tools/twt/twt_chartink_gap.py weekdays          # no box, no network
    AWS_PROFILE=baskfy-poc bash ../tools/twt/twt-chartink-pull.sh    # read-only box pull
    uv run python ../tools/twt/twt_chartink_gap.py measure --session 2026-09-11

**The question this answers is not "did it run".** Every ``tw_*`` table on the box is 0 rows
because the detector has never run there, which explains an empty screen and explains nothing
about the rule. This measures the rule: given the plant's bars for a session, which of Chartink's
own names for that session does the detector's reading produce, and for each one it does not,
which line refused it.

**It is a scorer, not a scanner.** Every number here comes from ``tools/twt/twt_scan.py`` —
:func:`twt_scan.tight_state_parts`, :func:`twt_scan.stock_days`,
:func:`twt_scan.chartink_stock_days` — and from ``twt_recall.score``. Nothing in this file
re-implements a line of the scan, and nothing in it may tune one (``PLAN-SCAN-SYNC.md``, leaf 6:
*"may not change core scan logic"*).

**Why the panel needs topping up.** ``research/volume-breakout/data/panel.pkl`` is the study's own
1.7 GB matrix and it ends **2026-09-09**; the local plant databases on ``localhost:5433`` end
**2026-09-04**. Neither reaches 2026-09-11. The two missing sessions are read from the production
box — read-only, ``SELECT`` only, no write, no deploy — and spliced onto the panel's right edge.
``twt-chartink-pull.sh`` is that read; this module never touches a network itself.

**And why a second, narrower splice.** The panel is *thinner than the plant* for some names: it
carries 7 bars of SIGIND where the box carries 1,917. Scoring SIGIND as a miss would blame the
scan for the panel's own gap, so ``--repair`` overwrites the disputed names' recent bars with the
box's. A name that is still missed after that is missed on the plant's real data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import twt_scan as scan
from twt_panel import CHARTINK, ResearchPanel, read_panel
from twt_recall import score

REPO: Final = Path(__file__).resolve().parents[2]
#: Where ``twt-chartink-pull.sh`` leaves the box's bars. Under ``data/``, which is untracked.
CACHE: Final = REPO / "decile-blueprint" / "data" / "outputs" / "twt"
TOPUP: Final = CACHE / "box_topup.psv"
REPAIR: Final = CACHE / "box_repair.psv"
#: The first session ``--repair`` overwrites. Covers the 50-session volume window and month-3.
REPAIR_FROM: Final = dt.date(2026, 4, 1)

_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri")
#: ``len(_WEEKDAYS)``, spelled out so the weekend branch below is not a magic number.
_TRADING_WEEKDAYS: Final = len(_WEEKDAYS)

#: ``pandas`` columns a pipe-separated box row carries, in order.
_PSV: Final = ("symbol", "date", "low", "close", "close_raw", "volume")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    session: dt.date
    low: float
    close: float
    close_raw: float
    volume: float


def read_psv(path: Path) -> list[Bar]:
    """``symbol|date|low|close|close_raw|volume``, one line a bar. Absent file is an empty list."""
    if not path.exists():
        return []
    bars = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        symbol, session, low, close, close_raw, volume = line.split("|")
        bars.append(
            Bar(
                symbol,
                dt.date.fromisoformat(session),
                float(low),
                float(close),
                float(close_raw),
                float(volume),
            )
        )
    return bars


def extend(panel: ResearchPanel, topup: list[Bar], repair: list[Bar]) -> ResearchPanel:
    """The panel with the box's sessions appended, and the repaired names' recent bars replaced.

    Only ``low``, ``close``, ``close_raw`` and ``volume`` are carried: those are every input
    ``tight_state_parts`` reads. ``open`` and ``high`` stay ``NaN`` on the new columns, which is
    honest — they were not fetched, and nothing in the scan asks for them.
    """
    row_of = {symbol: row for row, symbol in enumerate(panel.symbols)}
    new_sessions = tuple(sorted({bar.session for bar in topup} - set(panel.sessions)))
    sessions = panel.sessions + new_sessions
    width = len(new_sessions)
    matrices = {
        name: np.hstack([getattr(panel, name), np.full((panel.instruments, width), np.nan)])
        for name in ("open", "high", "low", "close", "close_raw", "volume")
    }
    column_of = {session: column for column, session in enumerate(sessions)}

    def place(bars: list[Bar]) -> int:
        placed = 0
        for bar in bars:
            row = row_of.get(bar.symbol)
            column = column_of.get(bar.session)
            if row is None or column is None:
                continue
            matrices["low"][row, column] = bar.low
            matrices["close"][row, column] = bar.close
            matrices["close_raw"][row, column] = bar.close_raw
            matrices["volume"][row, column] = bar.volume
            placed += 1
        return placed

    place(topup)
    if repair:
        for symbol in {bar.symbol for bar in repair}:
            row = row_of.get(symbol)
            if row is None:
                continue
            for session, column in column_of.items():
                if session >= REPAIR_FROM:
                    for matrix in matrices.values():
                        matrix[row, column] = np.nan
        place(repair)
    return ResearchPanel(
        symbols=panel.symbols,
        instrument_ids=panel.instrument_ids,
        sessions=sessions,
        is_etf=panel.is_etf,
        **matrices,
    )


def measure(session: dt.date, *, repair: bool) -> dict[str, object]:
    """One session, both readings, every missed name attributed to the line that refused it."""
    panel = read_panel()
    extended = extend(panel, read_psv(TOPUP), read_psv(REPAIR) if repair else [])
    if session not in extended.sessions:
        raise SystemExit(
            f"{session} is not in the panel (it ends {extended.sessions[-1]}). "
            f"Run tools/twt/twt-chartink-pull.sh to fetch the box's later sessions into {TOPUP}."
        )
    column = extended.sessions.index(session)
    row_of = {symbol: row for row, symbol in enumerate(extended.symbols)}
    window = scan.Window(first=session, last=session)
    answer_key = scan.chartink_stock_days(CHARTINK, window)
    report: dict[str, object] = {
        "session": session.isoformat(),
        "weekday": _WEEKDAYS[session.weekday()]
        if session.weekday() < _TRADING_WEEKDAYS
        else "weekend",
        "panel_last_session": panel.sessions[-1].isoformat(),
        "extended_last_session": extended.sessions[-1].isoformat(),
        "repaired_names": sorted({bar.symbol for bar in read_psv(REPAIR)}) if repair else [],
        "chartink_names": len(answer_key),
    }
    for reading in scan.Reading:
        parts = scan.tight_state_parts(extended, reading)
        produced = scan.stock_days(parts.state, extended, window)
        card = score(produced, answer_key)
        weekly = scan.weekly_closes(extended, reading)
        month_low = scan.month_low_back(extended)
        volume_average = scan.rolling_mean(extended.volume, scan.VOL_SMA_BARS)
        per_name: dict[str, object] = {}
        for _, symbol in answer_key:
            row = row_of.get(symbol)
            if row is None:
                per_name[symbol] = {"verdict": "unknown_symbol"}
                continue
            closes = weekly[:, row, column]
            tight = (
                round(float(abs(np.nanmax(closes) / np.nanmin(closes) - 1) * 100), 3)
                if np.isfinite(closes).all()
                else None
            )
            if parts.state[row, column]:
                verdict = "FOUND"
            elif not parts.has_bar[row, column]:
                verdict = "no_bar"
            elif not parts.has_volume_average[row, column]:
                verdict = "no_volume_average"
            elif not parts.tight[row, column]:
                verdict = "not_tight"
            elif not parts.above_month_low[row, column]:
                verdict = "not_above_month_low"
            elif not parts.floors[row, column]:
                verdict = "below_a_floor"
            else:
                verdict = "unexplained"
            window_50 = extended.volume[row, column - scan.VOL_SMA_BARS + 1 : column + 1]
            per_name[symbol] = {
                "verdict": verdict,
                "tight_pct": tight,
                "bars_in_50": int(np.isfinite(window_50).sum()),
                "volume_sma_50": (
                    None
                    if not np.isfinite(volume_average[row, column])
                    else round(float(volume_average[row, column]))
                ),
                "close": round(float(extended.close[row, column]), 2),
                "close_raw": round(float(extended.close_raw[row, column]), 2),
                "month_3_low": (
                    None
                    if not np.isfinite(month_low[row, column])
                    else round(float(month_low[row, column]), 2)
                ),
            }
        report[reading.value] = {
            "produced": card.produced,
            "expected": card.expected,
            "matched": card.matched,
            "recall_pct": round(card.recall_pct, 1),
            "precision_pct": round(card.precision_pct, 1),
            "produced_names": sorted({symbol for _, symbol in produced}),
            "missed": [symbol for _, symbol in card.missed],
            "spurious": [symbol for _, symbol in card.spurious],
            "per_name": per_name,
        }
    return report


def weekdays() -> dict[str, object]:
    """T4's 64.9 %, split by the weekday it was measured on. No box, no network, no top-up.

    The split is the point: Chartink's backtester reads a **completed** weekly candle, so on a
    Friday its candle and a live 15:30 candle are the same candle and the two readings must agree.
    If they do, T4's all-weekday average is the wrong number to hold a Friday to.
    """
    panel = read_panel()
    window = scan.chartink_window(CHARTINK, panel.sessions[-1])
    answer_key = scan.chartink_stock_days(CHARTINK, window)
    out: dict[str, object] = {
        "window": [window.first.isoformat(), window.last.isoformat()],
        "chartink_stock_days": len(answer_key),
    }
    for reading in scan.Reading:
        parts = scan.tight_state_parts(panel, reading)
        produced = scan.stock_days(parts.state, panel, window)
        card = score(produced, answer_key)
        mine: dict[int, set[tuple[dt.date, str]]] = defaultdict(set)
        theirs: dict[int, set[tuple[dt.date, str]]] = defaultdict(set)
        for pair in produced:
            mine[pair[0].weekday()].add(pair)
        for pair in answer_key:
            theirs[pair[0].weekday()].add(pair)
        out[reading.value] = {
            "overall": {
                "produced": card.produced,
                "expected": card.expected,
                "matched": card.matched,
                "recall_pct": round(card.recall_pct, 1),
                "precision_pct": round(card.precision_pct, 1),
            },
            "miss_reasons": scan.diagnose(panel, card.missed, parts),
            "by_weekday": {
                _WEEKDAYS[day]: {
                    "expected": (c := score(mine[day], theirs[day])).expected,
                    "produced": c.produced,
                    "matched": c.matched,
                    "recall_pct": round(c.recall_pct, 1),
                    "precision_pct": round(c.precision_pct, 1),
                }
                for day in range(_TRADING_WEEKDAYS)
            },
        }
    return out


def _render_measure(report: dict[str, object]) -> str:
    lines = [
        f"session {report['session']} ({report['weekday']})  "
        f"panel ends {report['panel_last_session']}, extended to {report['extended_last_session']}",
        f"Chartink names for this session: {report['chartink_names']}",
    ]
    for reading in scan.Reading:
        block = report[reading.value]
        assert isinstance(block, dict)
        lines.append(
            f"  {reading.value:14s} produced {block['produced']} expected {block['expected']} "
            f"both {block['matched']} recall {block['recall_pct']}% "
            f"precision {block['precision_pct']}%"
        )
        buckets: dict[str, list[str]] = defaultdict(list)
        for symbol, info in sorted(block["per_name"].items()):
            assert isinstance(info, dict)
            if info["verdict"] != "FOUND":
                buckets[str(info["verdict"])].append(symbol)
        for reason, names in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
            lines.append(f"      {reason:22s} {len(names):2d}  {', '.join(names)}")
        lines.append(f"      spurious {len(block['spurious'])}: {', '.join(block['spurious'])}")
    return "\n".join(lines)


def _render_weekdays(out: dict[str, object]) -> str:
    span = out["window"]
    assert isinstance(span, list)
    lines = [f"window {span[0]} -> {span[1]}: {out['chartink_stock_days']} rows"]
    for reading in scan.Reading:
        block = out[reading.value]
        assert isinstance(block, dict)
        overall = block["overall"]
        lines.append(
            f"  {reading.value:14s} recall {overall['recall_pct']}% "
            f"precision {overall['precision_pct']}%   "
            + "  ".join(
                f"{day} {stat['recall_pct']}/{stat['precision_pct']}"
                for day, stat in block["by_weekday"].items()
            )
        )
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("measure", help="one session against Chartink's own names for it")
    one.add_argument("--session", default="2026-09-11", type=dt.date.fromisoformat)
    one.add_argument("--no-repair", action="store_true", help="panel bars only, no box repair")
    one.add_argument("--json", action="store_true")
    many = sub.add_parser("weekdays", help="T4's 64.9 %%, split by weekday")
    many.add_argument("--json", action="store_true")
    parsed = parser.parse_args()
    if parsed.command == "measure":
        report = measure(parsed.session, repair=not parsed.no_repair)
        print(json.dumps(report, indent=1) if parsed.json else _render_measure(report))
    else:
        out = weekdays()
        print(json.dumps(out, indent=1) if parsed.json else _render_weekdays(out))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
