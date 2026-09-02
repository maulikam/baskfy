"""SW11 — the S2 Kite timing probe (STANDING-ANSWERS A4, DECISIONS-SW MD5).

SW6 left four questions no fixture can answer, because they are facts about what Kite prints and
when (`docs/swing/STATUS.md` SW6 "did NOT do"):

1. Does ``/quote``'s ``volume`` at 09:09 carry the pre-open matched quantity, or read 0 until
   09:15? (SW6.1's pace clock assumes the former.)
2. Does the pre-open ``last_price`` move during 09:00-09:08 — once, at 09:07-09:08, when the
   equilibrium price is set — or not at all?
3. Is ``ohlc.open`` populated before 09:15, so the gap could be measured from it at 09:09
   rather than at 09:16?
4. Does ``historical_data(interval="minute")`` return the forming/closed 09:20 candle at
   09:20:05, at 09:20:35, or only from 09:21? (The tick-built range no longer waits on it —
   SW11 — but the reconcile delay is tuned to this.)

This module is one morning of sampling written down. It is **pure over its source and its
clock**: :func:`run_timing_probe` takes a :class:`ProbeSource`, ``now`` and ``sleep``, walks
:data:`SCHEDULE`, and returns a :class:`ProbeReport`; :func:`render_report` turns that into the
Markdown that lands in ``docs/swing/status/S2-kite-timing.md``; :func:`probe_once` is the task
body, which also writes the ``.done`` marker that makes the Beat entry a one-shot. The Celery
binding (``baskfy.swing.timing_probe``) is gated by ``BASKFY_SWING_TIMING_PROBE`` and does
nothing at all with it false — the whole point is one morning, by Maulik's hand, after a Kite
login before 09:00.

Reads only: ``/quote`` a dozen times and three minute-candle requests, all through the
provider's limiter. Nothing here can act on a price (law 2).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from baskfy_core.models.base import JsonObject
from baskfy_providers.records import QuoteRecord

log = logging.getLogger(__name__)

__all__ = [
    "DONE_MARKER",
    "REPORT_NAME",
    "SCHEDULE",
    "CandleSample",
    "ProbeReport",
    "ProbeSource",
    "QuoteSample",
    "probe_once",
    "render_report",
    "run_timing_probe",
]

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

#: What the task writes, and what tells it not to run again.
REPORT_NAME: Final = "S2-kite-timing.md"
DONE_MARKER: Final = "S2-kite-timing.done"

#: When each sample is taken (IST wall clock) and what kind it is. Quotes through the pre-open
#: (09:00-09:08), at the 09:09 the old scan ran, at 09:12, and around the open; the minute
#: candle at 09:20:05 / 09:20:35 / 09:21:05 for question 4. Sorted; a sample whose time has
#: passed when the probe starts is skipped and said so in the report.
SCHEDULE: Final[tuple[tuple[dt.time, str], ...]] = (
    (dt.time(9, 4, 30), "quote"),
    (dt.time(9, 6, 0), "quote"),
    (dt.time(9, 7, 30), "quote"),
    (dt.time(9, 8, 0), "quote"),
    (dt.time(9, 8, 30), "quote"),
    (dt.time(9, 9, 0), "quote"),
    (dt.time(9, 12, 0), "quote"),
    (dt.time(9, 15, 30), "quote"),
    (dt.time(9, 16, 0), "quote"),
    (dt.time(9, 20, 5), "candle"),
    (dt.time(9, 20, 35), "candle"),
    (dt.time(9, 21, 5), "candle"),
    (dt.time(9, 21, 10), "quote"),
)

#: The minute the candle question is about: the 09:20 bar, first one after a 5-minute range.
CANDLE_OF_INTEREST: Final = dt.time(9, 20)
#: How many candle asks the schedule carries, and how many pre-open samples answer question 2.
_CANDLE_ASKS: Final = sum(1 for _, kind in SCHEDULE if kind == "candle")
_TWO_SAMPLES: Final = 2


class ProbeSource(Protocol):
    """What the probe reads. Production: the Kite provider; tests: a scripted fake."""

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]: ...

    def minute_candles(
        self, token: int, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class QuoteSample:
    at: dt.datetime
    symbol: str
    last_price: Decimal
    open: Decimal | None
    volume: int
    as_of: dt.datetime | None

    def as_dict(self) -> JsonObject:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "symbol": self.symbol,
            "last_price": str(self.last_price),
            "open": None if self.open is None else str(self.open),
            "volume": self.volume,
            "as_of": None if self.as_of is None else self.as_of.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class CandleSample:
    at: dt.datetime
    token: int
    count: int
    last_start: dt.datetime | None
    error: str | None = None

    def as_dict(self) -> JsonObject:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "token": self.token,
            "count": self.count,
            "last_start": None if self.last_start is None else self.last_start.isoformat(),
            "error": self.error,
        }


@dataclass(slots=True)
class ProbeReport:
    day: dt.date
    symbols: tuple[str, ...]
    token: int
    quotes: list[QuoteSample] = field(default_factory=list)
    candles: list[CandleSample] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # --- the four answers, derived from the samples ---------------------------------------

    def preopen_volume_seen(self) -> bool | None:
        """Q1: any sample at or before 09:09 with a positive volume."""
        early = [q for q in self.quotes if q.at.time() <= dt.time(9, 9)]
        if not early:
            return None
        return any(q.volume > 0 for q in early)

    def preopen_ltp_moved(self) -> bool | None:
        """Q2: did any symbol's last price change across the pre-open samples?"""
        early = [q for q in self.quotes if q.at.time() <= dt.time(9, 9)]
        if len({q.at for q in early}) < _TWO_SAMPLES:
            return None
        by_symbol: dict[str, set[Decimal]] = {}
        for q in early:
            by_symbol.setdefault(q.symbol, set()).add(q.last_price)
        return any(len(prices) > 1 for prices in by_symbol.values())

    def open_before_session(self) -> bool | None:
        """Q3: a positive ``ohlc.open`` on a sample at or before 09:09."""
        early = [q for q in self.quotes if q.at.time() <= dt.time(9, 9)]
        if not early:
            return None
        return any(q.open is not None and q.open > 0 for q in early)

    def candle_first_seen_at(self) -> dt.datetime | None:
        """Q4: the earliest candle sample in which the 09:20 bar was present."""
        for sample in self.candles:
            if sample.last_start is not None and sample.last_start.time() >= CANDLE_OF_INTEREST:
                return sample.at
        return None

    def good(self) -> bool:
        """One good run: a pre-open reading, a post-open reading, three candle asks that did
        not error, and the 09:20 candle seen in at least one of them."""
        pre = any(q.at.time() <= dt.time(9, 9) for q in self.quotes)
        post = any(q.at.time() >= dt.time(9, 15, 30) for q in self.quotes)
        candles_ok = len(self.candles) == _CANDLE_ASKS and all(
            c.error is None for c in self.candles
        )
        return pre and post and candles_ok and self.candle_first_seen_at() is not None

    def as_detail(self) -> JsonObject:
        return {
            "day": self.day.isoformat(),
            "symbols": list(self.symbols),
            "token": self.token,
            "quotes": [q.as_dict() for q in self.quotes],
            "candles": [c.as_dict() for c in self.candles],
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "answers": {
                "preopen_volume_seen": self.preopen_volume_seen(),
                "preopen_ltp_moved": self.preopen_ltp_moved(),
                "open_before_session": self.open_before_session(),
                "candle_first_seen_at": (
                    None
                    if (seen := self.candle_first_seen_at()) is None
                    else seen.isoformat(timespec="seconds")
                ),
            },
            "good": self.good(),
        }


def _at(day: dt.date, when: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, when, tzinfo=IST)


def run_timing_probe(  # noqa: PLR0913 - the probe's seams, named
    source: ProbeSource,
    *,
    symbols: Sequence[str],
    token: int,
    day: dt.date,
    now: Callable[[], dt.datetime],
    sleep: Callable[[float], None] = time.sleep,
    schedule: Sequence[tuple[dt.time, str]] = SCHEDULE,
) -> ProbeReport:
    """Walk the schedule against the clock. A sample already in the past when the probe
    reaches it is skipped (and listed); a source error is recorded and the walk goes on."""
    report = ProbeReport(day=day, symbols=tuple(symbols), token=token)
    for when, kind in schedule:
        target = _at(day, when)
        moment = now()
        if moment > target + dt.timedelta(seconds=20):
            report.skipped.append(
                f"{kind} at {when.isoformat()} (probe reached it at {moment:%H:%M:%S})"
            )
            continue
        wait = (target - moment).total_seconds()
        if wait > 0:
            sleep(wait)
        stamp = now()
        if kind == "quote":
            try:
                rows = source.quotes(list(symbols))
            except Exception as exc:
                report.errors.append(f"quote at {when.isoformat()}: {exc}")
                continue
            for row in rows:
                report.quotes.append(
                    QuoteSample(
                        at=_at(day, when),
                        symbol=row.symbol,
                        last_price=row.last_price,
                        open=row.open,
                        volume=int(row.volume),
                        as_of=row.as_of,
                    )
                )
        else:
            start = _at(day, dt.time(9, 15)).replace(tzinfo=None)
            try:
                candles = source.minute_candles(token, start, stamp.replace(tzinfo=None))
            except Exception as exc:
                report.candles.append(
                    CandleSample(
                        at=_at(day, when), token=token, count=0, last_start=None, error=str(exc)
                    )
                )
                continue
            last = _last_candle_start(candles)
            report.candles.append(
                CandleSample(at=_at(day, when), token=token, count=len(candles), last_start=last)
            )
    return report


def _last_candle_start(candles: Sequence[dict[str, object]]) -> dt.datetime | None:
    starts: list[dt.datetime] = []
    for candle in candles:
        raw = candle.get("date")
        if isinstance(raw, dt.datetime):
            starts.append(raw.replace(tzinfo=None))
        elif isinstance(raw, str):
            try:
                starts.append(dt.datetime.fromisoformat(raw).replace(tzinfo=None))
            except ValueError:
                continue
    return max(starts) if starts else None


def _yes_no(value: bool | None) -> str:
    return "unknown (no sample)" if value is None else ("**yes**" if value else "**no**")


def render_report(report: ProbeReport, *, generated_at: dt.datetime) -> str:
    """The Markdown for ``docs/swing/status/S2-kite-timing.md``: the four answers, then the
    evidence, then what each answer means for the schedule."""
    seen = report.candle_first_seen_at()
    lines = [
        "# S2 — Kite timing, as measured",
        "",
        f"Probed on {report.day.isoformat()} (IST), written {generated_at:%Y-%m-%d %H:%M:%S} IST "
        f"by `baskfy.swing.timing_probe` (`docs/swing/STANDING-ANSWERS.md` A4). Symbols: "
        f"{', '.join(report.symbols)}; minute candles for token {report.token}.",
        "",
        "Good run: "
        + ("yes." if report.good() else "NO — see skipped/errors; the Beat entry stays armed."),
        "",
        "## The four answers",
        "",
        f"1. `volume` at ≤ 09:09 carries the pre-open matched quantity: "
        f"{_yes_no(report.preopen_volume_seen())}",
        "2. The pre-open `last_price` moves during 09:00-09:09: "
        f"{_yes_no(report.preopen_ltp_moved())}",
        f"3. `ohlc.open` is populated before 09:15: {_yes_no(report.open_before_session())}",
        "4. The 09:20 minute candle first appeared in `historical_data` at: "
        + (seen.strftime("%H:%M:%S") if seen is not None else "never in the three asks"),
        "",
        "## What this means for the schedule",
        "",
        "- Gap scan: "
        + (
            "answers 1 and 3 are both yes, so the scan could return to 09:09 (one line in "
            "`celery_app.py`, `swing-premarket-gaps`); it stays at 09:16 until Maulik moves it."
            if report.preopen_volume_seen() and report.open_before_session()
            else "at least one of answers 1 and 3 is no or unknown — the scan stays at 09:16 "
            "(SW11, MD5)."
        ),
        "- Opening range: built from the ticks either way (A4); `range_reconcile_delay_minutes` "
        + (
            f"[1] is enough — the candle was there at {seen:%H:%M:%S}."
            if seen is not None and seen.time() <= dt.time(9, 21, 5)
            else "[1] may be short — the candle was not seen by 09:21:05; consider 2."
        ),
        "",
        "## Quote samples",
        "",
        "| at | symbol | last_price | ohlc.open | volume | exchange timestamp |",
        "|---|---|---|---|---|---|",
    ]
    for q in report.quotes:
        lines.append(
            f"| {q.at:%H:%M:%S} | {q.symbol} | {q.last_price} | "
            f"{'-' if q.open is None else q.open} | {q.volume} | "
            f"{'-' if q.as_of is None else q.as_of.strftime('%H:%M:%S')} |"
        )
    lines += [
        "",
        "## Minute-candle samples",
        "",
        "| asked at | candles | last candle start | error |",
        "|---|---|---|---|",
    ]
    for c in report.candles:
        lines.append(
            f"| {c.at:%H:%M:%S} | {c.count} | "
            f"{'-' if c.last_start is None else c.last_start.strftime('%H:%M')} | {c.error or ''} |"
        )
    if report.skipped:
        lines += ["", "## Skipped (the probe started late)", ""] + [
            f"- {s}" for s in report.skipped
        ]
    if report.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in report.errors]
    lines.append("")
    return "\n".join(lines)


def probe_once(  # noqa: PLR0913 - the task body's seams, named
    source: ProbeSource,
    *,
    out_dir: Path,
    symbols: Sequence[str],
    token: int,
    now: Callable[[], dt.datetime],
    sleep: Callable[[float], None] = time.sleep,
    schedule: Sequence[tuple[dt.time, str]] = SCHEDULE,
) -> JsonObject:
    """The task body: refuse if the marker exists, sample, write the report, and — after a
    good run only — write the marker so the next 09:04 does nothing."""
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / DONE_MARKER
    if marker.exists():
        return {"skipped": f"{marker} exists — the probe already had its good run", "good": True}
    day = now().astimezone(IST).date()
    report = run_timing_probe(
        source, symbols=symbols, token=token, day=day, now=now, sleep=sleep, schedule=schedule
    )
    text = render_report(report, generated_at=now().astimezone(IST))
    path = out_dir / REPORT_NAME
    path.write_text(text, encoding="utf-8")
    log.info("S2 timing probe written to %s (good=%s)", path, report.good())
    if report.good():
        marker.write_text(f"{day.isoformat()}\n", encoding="utf-8")
    return {"report": str(path), **report.as_detail()}
