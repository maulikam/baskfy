#!/usr/bin/env python
"""Two trade lists, and every way they differ — named and sized, never averaged (TW2).

``docs/twt/06`` TW2: *"Every difference from the research goes in DECISIONS-TW.md with its cause
and its size. A difference that cannot be explained is a blocker for TW9, not a footnote."* This
module is the half of that sentence a machine can do.

The discipline it enforces, in three rules:

1. **Nothing is summarised into a single number.** There is no mean error, no RMS, no "worst
   case" that stands in for the rest. :class:`TradeComparison` carries one
   :class:`Difference` per differing field per trade, and rendering it prints them.
2. **A tolerance marks a difference; it never removes one.** ``within_tolerance`` is a flag on a
   difference that is still in the list, still counted, still rendered. ``clean`` means *no
   difference at all*; ``passes`` means *every difference is inside a tolerance somebody wrote
   down*. A test may assert either, and the two are not the same claim.
3. **Identity before arithmetic.** Trades are matched on ``(symbol, entry_date)`` — the two things
   a reproduction cannot get right by accident — so a missing trade is reported as *missing* and
   not as a hundred wrong prices.

Pure stdlib and ``Decimal``: money never becomes a float on its way through this file.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

#: The columns ``research/tight-close/out/final_trades.csv`` carries, in its own order.
GOLDEN_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "entry_date",
    "exit_date",
    "entry",
    "exit",
    "qty",
    "pnl",
    "ret_pct",
    "r_mult",
    "hold",
    "reason",
)

#: The research's exit labels, and what ``baskfy_core.twt.exits.ExitReason`` calls the same event.
#: The study's five are lower case and split the stop by **how it filled**, which is the same split
#: the sleeve's enum makes.
#:
#: **There is no separate label for the trail, in either vocabulary.** The research simulator
#: raises ``pos.stop`` as the trail ratchets and reports every exit through it as ``stop``, so
#: STRATEGY §3's "137 of 164 exits are the trail" is the 137 ``stop`` rows and not a field of the
#: file. A comparison of trail-versus-disaster exits is therefore **not available from the
#: goldens** and TW2 must not invent one (DECISIONS-TW TW2.4).
RESEARCH_EXIT_REASONS: Final[Mapping[str, str]] = {
    "stop": "STOP_HIT",
    "stop_day0": "STOP_DAY0",
    "stop_gap": "STOP_GAP",
    "no_bar": "NO_BAR",
    "end": "END_OF_RUN",
}

#: Fields compared on a matched trade, and how a difference in each is sized.
COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "exit_date",
    "entry_price",
    "exit_price",
    "quantity",
    "pnl_inr",
    "return_pct",
    "hold_sessions",
    "reason",
)

#: How much a field may differ before the difference is called out of tolerance. A price is
#: compared **to the paisa** because the study's fills are exchange prints; a percentage carries
#: the rounding of the study's own printing.
DEFAULT_TRADE_TOLERANCE: Final[Mapping[str, Decimal]] = {
    "entry_price": Decimal("0.01"),
    "exit_price": Decimal("0.01"),
    "pnl_inr": Decimal("1"),
    "return_pct": Decimal("0.01"),
}

#: ``final_metrics.json``'s headline numbers and the tolerance each is read to. Three of them are
#: stored rounded to one decimal place, so half of that last place is the honest allowance.
DEFAULT_METRIC_TOLERANCE: Final[Mapping[str, float]] = {
    "cagr_pct": 0.01,
    "max_dd_pct": 0.05,
    "trades": 0.0,
    "win_rate_pct": 0.05,
    "profit_factor": 0.005,
    "avg_hold": 0.05,
    "exposure_pct": 0.05,
}

#: How many differences :meth:`TradeComparison.render` prints before it says "and N more".
_RENDER_LIMIT: Final = 20


@dataclass(frozen=True, slots=True)
class Trade:
    """One round trip, in the shape both sides are read into before they are compared."""

    symbol: str
    entry_date: dt.date
    exit_date: dt.date
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    pnl_inr: Decimal
    return_pct: Decimal
    hold_sessions: int
    reason: str

    @property
    def key(self) -> tuple[str, dt.date]:
        """The match key: what a reproduction cannot get right by accident."""
        return (self.symbol, self.entry_date)


class DifferenceKind(StrEnum):
    #: In the golden list and not in the produced one.
    MISSING = "missing"
    #: In the produced list and not in the golden one.
    EXTRA = "extra"
    #: Both lists hold the trade and one of its fields differs.
    FIELD = "field"


@dataclass(frozen=True, slots=True)
class Difference:
    kind: DifferenceKind
    symbol: str
    entry_date: dt.date
    #: The field that differs, for :attr:`DifferenceKind.FIELD`; empty otherwise.
    field: str = ""
    expected: str = ""
    produced: str = ""
    #: How big the difference is, where the field is a number. ``None`` for a date, a reason or a
    #: whole missing trade — **and a ``None`` size is never a small size**.
    size: Decimal | None = None
    within_tolerance: bool = False

    def render(self) -> str:
        if self.kind is not DifferenceKind.FIELD:
            return f"{self.kind.value:8s} {self.symbol} entered {self.entry_date}"
        size = "" if self.size is None else f" (by {self.size})"
        flag = " [inside tolerance]" if self.within_tolerance else ""
        return (
            f"{self.kind.value:8s} {self.symbol} entered {self.entry_date}: "
            f"{self.field} expected {self.expected} produced {self.produced}{size}{flag}"
        )


@dataclass(frozen=True, slots=True)
class TradeComparison:
    expected_count: int
    produced_count: int
    matched_count: int
    differences: tuple[Difference, ...]

    @property
    def clean(self) -> bool:
        """No difference of any kind, of any size. The claim TW2's AC asks for first."""
        return not self.differences

    @property
    def passes(self) -> bool:
        """Every difference is a field difference inside a written-down tolerance."""
        return all(
            difference.kind is DifferenceKind.FIELD and difference.within_tolerance
            for difference in self.differences
        )

    def by_kind(self) -> dict[str, int]:
        counts = {kind.value: 0 for kind in DifferenceKind}
        for difference in self.differences:
            counts[difference.kind.value] += 1
        return {name: count for name, count in counts.items() if count}

    def by_field(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for difference in self.differences:
            if difference.kind is DifferenceKind.FIELD:
                counts[difference.field] = counts.get(difference.field, 0) + 1
        return counts

    def outside_tolerance(self) -> tuple[Difference, ...]:
        return tuple(
            difference for difference in self.differences if not difference.within_tolerance
        )

    def largest(self, field: str) -> Difference | None:
        """The biggest difference in one field — for the report, never for the verdict."""
        sized = [
            difference
            for difference in self.differences
            if difference.field == field and difference.size is not None
        ]
        if not sized:
            return None
        return max(sized, key=lambda difference: abs(difference.size or Decimal(0)))

    def render(self, limit: int = _RENDER_LIMIT) -> str:
        head = (
            f"{self.matched_count} of {self.expected_count} golden trades matched "
            f"({self.produced_count} produced); {len(self.differences)} differences"
        )
        if self.clean:
            return head + " — identical"
        lines = [head, f"  by kind: {self.by_kind()}"]
        if self.by_field():
            lines.append(f"  by field: {self.by_field()}")
        for difference in self.differences[:limit]:
            lines.append("  " + difference.render())
        remaining = len(self.differences) - limit
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
        return "\n".join(lines)


def _numeric(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def _field_difference(
    expected: Trade, produced: Trade, field: str, tolerance: Mapping[str, Decimal]
) -> Difference | None:
    mine = getattr(produced, field)
    theirs = getattr(expected, field)
    if mine == theirs:
        return None
    left, right = _numeric(theirs), _numeric(mine)
    size = abs(right - left) if left is not None and right is not None else None
    allowed = tolerance.get(field)
    return Difference(
        kind=DifferenceKind.FIELD,
        symbol=expected.symbol,
        entry_date=expected.entry_date,
        field=field,
        expected=str(theirs),
        produced=str(mine),
        size=size,
        within_tolerance=size is not None and allowed is not None and size <= allowed,
    )


def compare_trades(
    produced: Sequence[Trade],
    expected: Sequence[Trade],
    tolerance: Mapping[str, Decimal] = DEFAULT_TRADE_TOLERANCE,
    fields: Sequence[str] = COMPARED_FIELDS,
) -> TradeComparison:
    """Every difference between two trade lists, by kind and by size.

    Matched on ``(symbol, entry_date)``. A key either side holds twice is a difference in itself
    and is reported as an extra or a missing trade rather than silently paired.
    """
    mine = {trade.key: trade for trade in produced}
    theirs = {trade.key: trade for trade in expected}
    if len(mine) != len(produced) or len(theirs) != len(expected):
        raise ValueError(
            "a trade list holds two trades with the same (symbol, entry_date); the comparison "
            "would have to guess which is which, and guessing is what this module exists to stop"
        )
    differences: list[Difference] = []
    for key in sorted(theirs.keys() - mine.keys()):
        differences.append(
            Difference(kind=DifferenceKind.MISSING, symbol=key[0], entry_date=key[1])
        )
    for key in sorted(mine.keys() - theirs.keys()):
        differences.append(Difference(kind=DifferenceKind.EXTRA, symbol=key[0], entry_date=key[1]))
    for key in sorted(mine.keys() & theirs.keys()):
        for name in fields:
            found = _field_difference(theirs[key], mine[key], name, tolerance)
            if found is not None:
                differences.append(found)
    return TradeComparison(
        expected_count=len(theirs),
        produced_count=len(mine),
        matched_count=len(mine.keys() & theirs.keys()),
        differences=tuple(differences),
    )


@dataclass(frozen=True, slots=True)
class MetricDifference:
    name: str
    expected: float
    produced: float
    tolerance: float

    @property
    def delta(self) -> float:
        return self.produced - self.expected

    @property
    def inside(self) -> bool:
        return abs(self.delta) <= self.tolerance

    def render(self) -> str:
        flag = "ok " if self.inside else "OUT"
        return (
            f"{flag} {self.name:16s} study {self.expected:>10.4g} "
            f"ours {self.produced:>10.4g} delta {self.delta:+.4g} (tolerance {self.tolerance})"
        )


@dataclass(frozen=True, slots=True)
class MetricComparison:
    metrics: tuple[MetricDifference, ...]

    @property
    def passes(self) -> bool:
        return all(metric.inside for metric in self.metrics)

    def outside(self) -> tuple[MetricDifference, ...]:
        return tuple(metric for metric in self.metrics if not metric.inside)

    def render(self) -> str:
        return "\n".join(metric.render() for metric in self.metrics)


def compare_metrics(
    produced: Mapping[str, float],
    expected: Mapping[str, float],
    tolerance: Mapping[str, float] = DEFAULT_METRIC_TOLERANCE,
) -> MetricComparison:
    """Each headline number against the study's, with the allowance it is read to.

    A name the produced side does not carry is **not skipped**: it is reported against a
    ``nan``, which is outside every tolerance. A reproduction that forgot to compute a number has
    not reproduced it.
    """
    rows = []
    for name, allowance in tolerance.items():
        rows.append(
            MetricDifference(
                name=name,
                expected=float(expected[name]),
                produced=float(produced.get(name, float("nan"))),
                tolerance=allowance,
            )
        )
    return MetricComparison(metrics=tuple(rows))


def read_golden_trades(path: Path) -> tuple[Trade, ...]:
    """``final_trades.csv`` — the study's own 164 trades, as written by ``final_tc.py``.

    Numbers go through ``Decimal(str(...))``, so the digits in the file are the digits compared:
    the study printed full float repr and a re-parse through ``float`` would introduce a
    difference this module exists to detect.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if rows and tuple(rows[0].keys()) != GOLDEN_COLUMNS:
        raise ValueError(f"{path} has columns {tuple(rows[0].keys())}, expected {GOLDEN_COLUMNS}")
    return tuple(
        Trade(
            symbol=row["symbol"],
            entry_date=dt.date.fromisoformat(row["entry_date"]),
            exit_date=dt.date.fromisoformat(row["exit_date"]),
            entry_price=Decimal(row["entry"]),
            exit_price=Decimal(row["exit"]),
            quantity=int(row["qty"]),
            pnl_inr=Decimal(row["pnl"]),
            return_pct=Decimal(row["ret_pct"]),
            hold_sessions=int(row["hold"]),
            reason=RESEARCH_EXIT_REASONS.get(row["reason"], row["reason"].upper()),
        )
        for row in rows
    )


def read_golden_metrics(path: Path) -> dict[str, float]:
    """``final_metrics.json`` — every number in it that is a number."""
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: float(value)
        for name, value in stored.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def trades_from(records: Iterable[object]) -> tuple[Trade, ...]:
    """Adapt whatever ``baskfy_core.twt.backtest`` returns into :class:`Trade`.

    The seam's tolerance for names: a produced trade must carry ``symbol``, ``entry_date``,
    ``exit_date``, ``entry_price``, ``exit_price``, ``quantity``, ``pnl_inr``, ``return_pct``,
    ``hold_sessions`` and ``reason`` — which is exactly ``baskfy_core.vbt.backtest.BacktestTrade``,
    the shape TW1's sibling is building against. A record missing one of them raises here, with
    the name, rather than comparing a default.
    """
    adapted = []
    for record in records:
        values = {}
        for name in (
            "symbol",
            "entry_date",
            "exit_date",
            "entry_price",
            "exit_price",
            "quantity",
            "pnl_inr",
            "return_pct",
            "hold_sessions",
            "reason",
        ):
            if not hasattr(record, name):
                raise AttributeError(
                    f"a produced trade has no {name!r}; TW2's comparison needs it "
                    f"(the record is {record!r})"
                )
            values[name] = getattr(record, name)
        adapted.append(
            Trade(
                symbol=str(values["symbol"]),
                entry_date=values["entry_date"],
                exit_date=values["exit_date"],
                entry_price=Decimal(values["entry_price"]),
                exit_price=Decimal(values["exit_price"]),
                quantity=int(values["quantity"]),
                pnl_inr=Decimal(values["pnl_inr"]),
                return_pct=Decimal(values["return_pct"]),
                hold_sessions=int(values["hold_sessions"]),
                reason=str(values["reason"]),
            )
        )
    return tuple(adapted)
