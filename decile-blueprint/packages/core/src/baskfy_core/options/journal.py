"""The journal in R, summarised per sleeve and never pooled (``04`` §12).

A summary is keyed by ``(sleeve, simulated, sizing_mode)`` — the three things ``04`` §12 and
``03`` §11 forbid pooling. ``summarize`` returns one ``Summary`` per key present;
``summarize_one`` refuses rows of more than one key. There is deliberately no function that
returns one number over two sleeves, or over real and simulated rows, or over paper-one-lot and
budget-sized rows.

``O3A`` and ``O3B`` are separate keys (OP1.6): they share config and a paper period, but each is
its own setup, and a pooled O3 number would hide which one carries the result.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options.config import ExpiryKind, SizingMode, Sleeve


class PooledRows(ValueError):
    """Rows of more than one ``(sleeve, simulated, sizing_mode)`` handed to one summary."""


def r_multiple(net_pnl_inr: Decimal, r_inr: Decimal) -> Decimal:
    """``net_pnl_inr / R``. R must be positive — a zero R is a sizing bug, not a zero trade."""
    if r_inr <= 0:
        raise ValueError("R must be positive")
    return net_pnl_inr / r_inr


@dataclass(frozen=True, slots=True)
class JournalRow:
    """One session's record. ``traded=False`` rows are the skips, with their reason."""

    sleeve: Sleeve
    trade_date: dt.date
    simulated: bool
    sizing_mode: SizingMode
    traded: bool
    skip_reason: str | None = None
    expiry_kind: ExpiryKind | None = None
    net_pnl_inr: Decimal = Decimal(0)
    r_inr: Decimal = Decimal(1)
    closed_reason: str | None = None
    minutes_held: int = 0
    mae_r: Decimal = Decimal(0)
    mfe_r: Decimal = Decimal(0)

    @property
    def r(self) -> Decimal:
        return r_multiple(self.net_pnl_inr, self.r_inr)


@dataclass(frozen=True, slots=True)
class PoolKey:
    sleeve: Sleeve
    simulated: bool
    sizing_mode: SizingMode


def key_of(row: JournalRow) -> PoolKey:
    return PoolKey(row.sleeve, row.simulated, row.sizing_mode)


@dataclass(frozen=True, slots=True)
class Bucket:
    """A split of traded rows: how many, and their mean R."""

    count: int
    mean_r: Decimal


@dataclass(frozen=True, slots=True)
class Summary:
    key: PoolKey
    count: int
    traded: int
    skipped_by_reason: tuple[tuple[str, int], ...]
    win_rate: Decimal | None
    mean_r: Decimal | None
    expectancy_inr: Decimal | None
    expectancy_r: Decimal | None
    worst_r: Decimal | None
    max_drawdown_r: Decimal
    max_drawdown_inr: Decimal
    mae_r: tuple[Decimal, ...]
    mfe_r: tuple[Decimal, ...]
    mean_minutes_held: Decimal | None
    by_closed_reason: tuple[tuple[str, Bucket], ...]
    by_expiry_kind: tuple[tuple[str, Bucket], ...]
    by_weekday: tuple[tuple[int, Bucket], ...]


def max_drawdown(values: Iterable[Decimal]) -> Decimal:
    """The largest peak-to-trough fall of the running sum, as a positive number (0 if none).
    The peak starts at 0: a book that loses from its first trade has drawn down from nothing."""
    peak = running = Decimal(0)
    worst = Decimal(0)
    for value in values:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _buckets[K](rows: Sequence[JournalRow], label: dict[int, K]) -> tuple[tuple[K, Bucket], ...]:
    grouped: dict[K, list[Decimal]] = defaultdict(list)
    for index, row in enumerate(rows):
        if index in label:
            grouped[label[index]].append(row.r)
    return tuple(
        (name, Bucket(len(rs), sum(rs, Decimal(0)) / len(rs)))
        for name, rs in sorted(grouped.items(), key=lambda item: str(item[0]))
    )


def summarize_one(rows: Sequence[JournalRow]) -> Summary:
    """The ``04`` §12 summary for rows of **one** key; raises ``PooledRows`` otherwise.

    Traded rows are ordered by date for the drawdown. ``by_expiry_kind`` is filled for O1 and O3
    rows that carry one; ``by_weekday`` (ISO, Monday = 1) for O2.
    """
    keys = {key_of(row) for row in rows}
    if len(keys) != 1:
        raise PooledRows(f"one summary per (sleeve, simulated, sizing_mode); got {len(keys)} keys")
    key = keys.pop()
    traded = sorted((row for row in rows if row.traded), key=lambda row: row.trade_date)
    skipped = Counter(row.skip_reason or "UNSPECIFIED" for row in rows if not row.traded)
    rs = [row.r for row in traded]
    wins = sum(1 for r in rs if r > 0)
    kinds = {
        i: row.expiry_kind.value
        for i, row in enumerate(traded)
        if row.expiry_kind is not None and key.sleeve is not Sleeve.O2
    }
    weekdays = {
        i: row.trade_date.isoweekday() for i, row in enumerate(traded) if key.sleeve is Sleeve.O2
    }
    reasons = {i: row.closed_reason or "UNSPECIFIED" for i, row in enumerate(traded)}
    return Summary(
        key=key,
        count=len(rows),
        traded=len(traded),
        skipped_by_reason=tuple(sorted(skipped.items())),
        win_rate=Decimal(wins) / len(rs) if rs else None,
        mean_r=_mean(rs),
        expectancy_inr=_mean([row.net_pnl_inr for row in traded]),
        expectancy_r=_mean(rs),
        worst_r=min(rs) if rs else None,
        max_drawdown_r=max_drawdown(rs),
        max_drawdown_inr=max_drawdown(row.net_pnl_inr for row in traded),
        mae_r=tuple(sorted(row.mae_r for row in traded)),
        mfe_r=tuple(sorted(row.mfe_r for row in traded)),
        mean_minutes_held=_mean([Decimal(row.minutes_held) for row in traded]),
        by_closed_reason=_buckets(traded, reasons),
        by_expiry_kind=_buckets(traded, kinds),
        by_weekday=_buckets(traded, weekdays),
    )


def summarize(rows: Iterable[JournalRow]) -> tuple[Summary, ...]:
    """One summary per ``(sleeve, simulated, sizing_mode)`` present, in a stable order."""
    grouped: dict[PoolKey, list[JournalRow]] = defaultdict(list)
    for row in rows:
        grouped[key_of(row)].append(row)
    ordered = sorted(grouped, key=lambda k: (k.sleeve.value, k.simulated, k.sizing_mode.value))
    return tuple(summarize_one(grouped[key]) for key in ordered)
