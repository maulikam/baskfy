"""The FO ledger and ``04`` §7's pauses, pure (``docs/fno/06`` FO10).

**Never pooled.** Every figure here is keyed by ``simulated``: a paper line and a live line are
two lines, a paper book and a live book are two books, and a pause is evaluated over one mode's
rows only (DECISIONS-FO FO10.1, correcting FO4.9). A live loss never pauses paper and a paper
loss never pauses live. There is deliberately no function that returns one number over both.

The ledger (``build_ledger``) has one line per ``(sleeve, underlying, simulated)`` and one book
per ``simulated``: closed trades, realised ₹ and R, month-to-date realised, the open positions'
latest ``fo_mark`` P&L, and the drawdown of the realised series in ₹ and R (peak from zero, the
options pack's ``max_drawdown``).

The pauses (``evaluate_pauses``), ``04`` §7, exactly:

* **F1, per underlying** (``F1N``, ``F1B``): the last **3** closed trades on that underlying were
  each **≤ -0.6R**. It stands until Maulik lifts it: only trades closed after the last lift count
  (``lifted_after``; FO10.2), so a lift clears the run and the next three closes start a new one.
  An entry abandoned before it was a structure (``ABANDONED_PARTIAL``) neither counts nor breaks
  the run (FO10.4);
* **F2**: the calendar month's closed F2 trades total **≤ -6R** → new entries pause to the end
  of that month;
* **Book**: the month's realised FO result (F1 and F2, one mode) **≤ -monthly_pause_inr** → the
  book pauses to the end of that month. ``monthly_pause_inr = 0`` (the seed) means **the ₹75,000
  ceiling applies**, and a value above the ceiling is held to it (FO10.5).

The month is the one ``as_of`` falls in: the scan passes the session its rows describe, the desk
the session it is closing in (FO4.9). A pause stops new entries only; open structures run to their
own exits (``04`` §7) — nothing here closes anything.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    FoSleeveGroup,
    group_of,
)
from baskfy_core.fno.exits import book_paused, f1_paused, f2_month_paused
from baskfy_core.fno.journal import CloseKind, PooledRows
from baskfy_core.options.journal import max_drawdown

F1_SLEEVES: tuple[FoSleeve, ...] = (FoSleeve.F1N, FoSleeve.F1B)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One ``fo_journal`` row, as the ledger and the pauses read it."""

    position_id: int
    sleeve: FoSleeve
    symbol: str
    simulated: bool
    closed_on: dt.date
    closed_at: dt.datetime | None
    net_pnl_inr: Decimal
    r_multiple: Decimal
    closed_reason: str
    rolls: int = 0


@dataclass(frozen=True, slots=True)
class OpenMark:
    """An open position's latest ``fo_mark``: its nightly P&L and its planned max loss."""

    position_id: int
    sleeve: FoSleeve
    symbol: str
    simulated: bool
    trade_date: dt.date | None
    pnl_inr: Decimal
    r_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class LineKey:
    sleeve: FoSleeve
    symbol: str
    simulated: bool


@dataclass(frozen=True, slots=True)
class LedgerFigures:
    """Realised and open figures for one pool."""

    closed: int
    wins: int
    realised_inr: Decimal
    realised_r: Decimal
    mtd_closed: int
    mtd_realised_inr: Decimal
    mtd_realised_r: Decimal
    open_positions: int
    open_marked_inr: Decimal
    max_drawdown_inr: Decimal
    max_drawdown_r: Decimal

    @property
    def total_inr(self) -> Decimal:
        """Realised plus the open positions' marked P&L."""
        return self.realised_inr + self.open_marked_inr

    @property
    def win_rate(self) -> Decimal | None:
        return None if not self.closed else Decimal(self.wins) / self.closed


@dataclass(frozen=True, slots=True)
class Ledger:
    as_of: dt.date
    #: One per ``(sleeve, underlying, simulated)`` present, in a stable order.
    lines: tuple[tuple[LineKey, LedgerFigures], ...]
    #: One per ``simulated`` present: the paper book and the live book, never one number.
    books: tuple[tuple[bool, LedgerFigures], ...]

    def line(self, sleeve: FoSleeve, symbol: str, *, simulated: bool) -> LedgerFigures | None:
        return dict(self.lines).get(LineKey(sleeve, symbol, simulated))

    def book(self, *, simulated: bool) -> LedgerFigures | None:
        return dict(self.books).get(simulated)


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _in_month(day: dt.date, as_of: dt.date) -> bool:
    return month_start(as_of) <= day <= as_of


def _ordered(trades: Iterable[ClosedTrade]) -> list[ClosedTrade]:
    return sorted(
        trades,
        key=lambda t: (t.closed_on, t.closed_at.timestamp() if t.closed_at else 0.0, t.position_id),
    )


def figures(
    trades: Sequence[ClosedTrade], marks: Sequence[OpenMark], as_of: dt.date
) -> LedgerFigures:
    """One pool's figures. Rows of both modes are refused (``PooledRows``)."""
    modes = {t.simulated for t in trades} | {m.simulated for m in marks}
    if len(modes) > 1:
        raise PooledRows("a ledger figure is one mode's: paper and live are never pooled")
    ordered = [t for t in _ordered(trades) if t.closed_on <= as_of]
    month = [t for t in ordered if _in_month(t.closed_on, as_of)]
    return LedgerFigures(
        closed=len(ordered),
        wins=sum(1 for t in ordered if t.net_pnl_inr > 0),
        realised_inr=sum((t.net_pnl_inr for t in ordered), Decimal(0)),
        realised_r=sum((t.r_multiple for t in ordered), Decimal(0)),
        mtd_closed=len(month),
        mtd_realised_inr=sum((t.net_pnl_inr for t in month), Decimal(0)),
        mtd_realised_r=sum((t.r_multiple for t in month), Decimal(0)),
        open_positions=len(marks),
        open_marked_inr=sum((m.pnl_inr for m in marks), Decimal(0)),
        max_drawdown_inr=max_drawdown(t.net_pnl_inr for t in ordered),
        max_drawdown_r=max_drawdown(t.r_multiple for t in ordered),
    )


def build_ledger(
    trades: Iterable[ClosedTrade], marks: Iterable[OpenMark], as_of: dt.date
) -> Ledger:
    """Per ``(sleeve, underlying, simulated)`` and per book per ``simulated``."""
    by_line: dict[LineKey, tuple[list[ClosedTrade], list[OpenMark]]] = defaultdict(lambda: ([], []))
    by_book: dict[bool, tuple[list[ClosedTrade], list[OpenMark]]] = defaultdict(lambda: ([], []))
    for t in trades:
        by_line[LineKey(t.sleeve, t.symbol, t.simulated)][0].append(t)
        by_book[t.simulated][0].append(t)
    for m in marks:
        by_line[LineKey(m.sleeve, m.symbol, m.simulated)][1].append(m)
        by_book[m.simulated][1].append(m)
    lines = tuple(
        (key, figures(*by_line[key], as_of))
        for key in sorted(by_line, key=lambda k: (k.simulated, k.sleeve.value, k.symbol))
    )
    books = tuple((mode, figures(*by_book[mode], as_of)) for mode in sorted(by_book))
    return Ledger(as_of, lines, books)


# --- the pauses ----------------------------------------------------------------------------------


class PauseCode(StrEnum):
    """``fo_sleeve_config`` / ``fo_book_config.paused_reason`` codes (≤ 32 characters)."""

    F1_LOSS_RUN = "F1_LOSS_RUN"
    F2_MONTH_R = "F2_MONTH_R"
    BOOK_MONTH_INR = "BOOK_MONTH_INR"


@dataclass(frozen=True, slots=True)
class FoPause:
    """One pause. ``scope`` is ``F1N``/``F1B`` (per underlying), ``F2`` or ``BOOK``.
    ``paused_until`` is inclusive; ``None`` stands until lifted (F1, FO10.2)."""

    scope: str
    code: PauseCode
    simulated: bool
    paused_until: dt.date | None
    message: str

    @property
    def reason(self) -> str:
        """The ``paused_reason`` column's value: the mode, then the code (FO10.1)."""
        return f"{mode_tag(simulated=self.simulated)}:{self.code.value}"

    def covers(self, sleeve: FoSleeve) -> bool:
        if self.scope == "BOOK":
            return True
        if self.scope == FoSleeveGroup.F2.value:
            return sleeve is FoSleeve.F2
        return self.scope == sleeve.value


def mode_tag(*, simulated: bool) -> str:
    return "PAPER" if simulated else "LIVE"


def book_pause_limit(monthly_pause_inr: Decimal, ceilings: FnoCeilings) -> Decimal:
    """``fo_book_config.monthly_pause_inr`` as the rule applies it (FO10.5): 0 — the seed, "not
    set" — is the ceiling; anything above the ceiling is held to it."""
    ceiling = ceilings.book_monthly_loss_inr_max
    if monthly_pause_inr <= 0:
        return ceiling
    return min(monthly_pause_inr, ceiling)


def evaluate_pauses(  # noqa: PLR0913 - every input 04 §7 names, by keyword
    trades: Iterable[ClosedTrade],
    *,
    simulated: bool,
    as_of: dt.date,
    monthly_pause_inr: Decimal,
    lifted_after: Mapping[FoSleeve, dt.datetime] | None = None,
    config: FnoConfig = DEFAULT_FNO_CONFIG,
    ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> tuple[FoPause, ...]:
    """``04`` §7 over one mode's closed trades up to ``as_of``. Rows of the other mode are not
    read at all (they are filtered out before any sum)."""
    mine = [t for t in _ordered(trades) if t.simulated is simulated and t.closed_on <= as_of]
    lifts = lifted_after or {}
    found: list[FoPause] = []
    f1 = config.f1
    for sleeve in F1_SLEEVES:
        lifted = lifts.get(sleeve)
        history = [
            t.r_multiple
            for t in mine
            if t.sleeve is sleeve
            and t.closed_reason != CloseKind.ABANDONED_PARTIAL.value
            and (lifted is None or (t.closed_at is not None and t.closed_at > lifted))
        ]
        if f1_paused(history, f1):
            tail = ", ".join(f"{r}R" for r in history[-f1.pause_consecutive :])
            found.append(
                FoPause(
                    sleeve.value,
                    PauseCode.F1_LOSS_RUN,
                    simulated,
                    None,
                    f"the last {f1.pause_consecutive} closed {sleeve.value} trades were each ≤ "
                    f"{f1.pause_loss_r}R ({tail}); new entries pause until lifted (04 §7)",
                )
            )
    month = [t for t in mine if _in_month(t.closed_on, as_of)]
    f2_month = [t.r_multiple for t in month if group_of(t.sleeve) is FoSleeveGroup.F2]
    if f2_month and f2_month_paused(f2_month, config.f2):
        found.append(
            FoPause(
                FoSleeveGroup.F2.value,
                PauseCode.F2_MONTH_R,
                simulated,
                month_end(as_of),
                f"the month's closed F2 trades total {sum(f2_month, Decimal(0))}R ≤ "
                f"{config.f2.month_pause_r}R; new entries pause for the rest of the month (04 §7)",
            )
        )
    limit = book_pause_limit(monthly_pause_inr, ceilings)
    realised = sum((t.net_pnl_inr for t in month), Decimal(0))
    if month and book_paused(realised, limit):
        found.append(
            FoPause(
                "BOOK",
                PauseCode.BOOK_MONTH_INR,
                simulated,
                month_end(as_of),
                f"the month's realised FO result ₹{realised} has reached the ₹{limit} book "
                "pause; new entries pause for the rest of the month (04 §7)",
            )
        )
    return tuple(found)


def pauses_for(pauses: Iterable[FoPause], sleeve: FoSleeve) -> list[FoPause]:
    """The pauses that stop a new ``sleeve`` entry."""
    return [p for p in pauses if p.covers(sleeve)]


def column_pause_applies(
    paused_until: dt.date | None, paused_reason: str | None, *, simulated: bool, day: dt.date
) -> bool:
    """Whether a stored ``paused_until``/``paused_reason`` stops an entry of this mode on
    ``day``. A ledger-written reason carries its mode (``PAPER:…`` / ``LIVE:…``) and binds that
    mode only; a reason without one is a hand-set pause and binds both (FO10.1)."""
    if paused_until is None or day > paused_until:
        return False
    tag, sep, _code = (paused_reason or "").partition(":")
    if sep and tag in {"PAPER", "LIVE"}:
        return tag == mode_tag(simulated=simulated)
    return True


__all__ = [
    "ClosedTrade",
    "FoPause",
    "Ledger",
    "LedgerFigures",
    "LineKey",
    "OpenMark",
    "PauseCode",
    "book_pause_limit",
    "build_ledger",
    "column_pause_applies",
    "evaluate_pauses",
    "figures",
    "mode_tag",
    "month_end",
    "month_start",
    "pauses_for",
]
