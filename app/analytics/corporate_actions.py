"""Corporate actions that move quantity without a trade.

WHY THIS EXISTS
A tradebook records transactions. A bonus issue or a split changes what you hold without
any transaction at all, so FIFO lots rebuilt from fills alone come out short — on this
book, ANANDRATHI showed 114 held against 70 in lots, and the 44-share gap was exactly the
position on the day the price halved.

Reconciliation could already SEE that gap. What it could not do is price it, and the
pricing is where the money is.

BONUS AND SPLIT ARE NOT THE SAME THING
Modelling both as "extra shares appeared" would silently misprice one of them:

  Bonus   Cost of acquisition is NIL (s.55). The holding period starts at ALLOTMENT, not
          at the original purchase. Selling a bonus share is therefore short-term tax on
          the entire proceeds for a year after it lands.
  Split   Nothing is acquired. One lot subdivides: quantity multiplies, cost per share
          divides by the same factor, and the holding period is INHERITED. A share bought
          two years ago is still long-term after a split.

Ratios are stored new:old, so a 1:1 bonus is (1, 1) and a 1:2 split is (2, 1).

Applied during the FIFO rebuild in date order alongside fills, so a sale after the ex-date
consumes the adjusted book and a sale before it does not.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import db

BONUS, SPLIT = "bonus", "split"
KINDS = (BONUS, SPLIT)


class CorporateActionError(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    symbol: str
    kind: str
    ex_date: dt.date
    ratio_new: float
    ratio_old: float
    note: str = ""

    @property
    def multiple(self) -> float:
        return self.ratio_new / self.ratio_old

    @property
    def when(self) -> dt.datetime:
        # Start of the ex-date: the adjustment precedes any trade on that session.
        return dt.datetime.combine(self.ex_date, dt.time(0, 1))

    def describe(self) -> str:
        r = f"{self.ratio_new:g}:{self.ratio_old:g}"
        if self.kind == BONUS:
            return (f"{r} bonus — {self.multiple:g} new share(s) per share held, "
                    f"nil cost, holding period starts {self.ex_date}")
        return (f"{r} split — each share becomes {self.multiple:g}, cost per share "
                f"divided, holding period inherited")


def parse(symbol: str, kind: str, ex_date: str, ratio_new, ratio_old,
          note: str = "") -> Action:
    """Validate before anything is stored. A wrong ratio silently misprices a position."""
    kind = str(kind).strip().lower()
    if kind not in KINDS:
        raise CorporateActionError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise CorporateActionError("symbol is required")
    try:
        d = dt.date.fromisoformat(str(ex_date).strip())
    except ValueError:
        raise CorporateActionError(f"ex_date {ex_date!r} is not a YYYY-MM-DD date")
    try:
        new, old = float(ratio_new), float(ratio_old)
    except (TypeError, ValueError):
        raise CorporateActionError("ratio must be numeric")
    if new <= 0 or old <= 0:
        raise CorporateActionError("ratio parts must both be positive")
    if kind == SPLIT and new / old <= 1:
        raise CorporateActionError(
            "a split must increase the share count; use ratio 2:1 for a 1-into-2 split")
    if d > dt.date.today():
        raise CorporateActionError(f"ex_date {d} is in the future")
    return Action(symbol, kind, d, new, old, note.strip())


# =====================================================================================
# persistence
# =====================================================================================
def record(conn, action: Action) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT OR REPLACE INTO corporate_actions(symbol, kind, ex_date, ratio_new,"
            " ratio_old, note, created_at) VALUES(?,?,?,?,?,?,?)",
            (action.symbol, action.kind, action.ex_date.isoformat(), action.ratio_new,
             action.ratio_old, action.note,
             dt.datetime.now().isoformat(timespec="seconds")))
    return int(cur.lastrowid)


def load(conn, symbols: Sequence[str] | None = None) -> list[Action]:
    sql = "SELECT * FROM corporate_actions"
    args: tuple = ()
    if symbols is not None:
        if not symbols:
            return []
        sql += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
        args = tuple(symbols)
    return [Action(r["symbol"], r["kind"], dt.date.fromisoformat(r["ex_date"]),
                   float(r["ratio_new"]), float(r["ratio_old"]), r["note"] or "")
            for r in conn.execute(sql + " ORDER BY ex_date, id", args)]


def listing(conn) -> list[dict]:
    return [{**dict(r), "describe": Action(
        r["symbol"], r["kind"], dt.date.fromisoformat(r["ex_date"]),
        float(r["ratio_new"]), float(r["ratio_old"])).describe()}
        for r in conn.execute("SELECT * FROM corporate_actions ORDER BY ex_date DESC, id DESC")]


def remove(conn, action_id: int) -> bool:
    with db.transaction(conn):
        cur = conn.execute("DELETE FROM corporate_actions WHERE id=?", (action_id,))
    return cur.rowcount > 0


# =====================================================================================
# application
# =====================================================================================
def apply_to_book(book: list, action: Action) -> None:
    """Adjust one symbol's open lots in place. `book` is a list of tradebook.Lot.

    A bonus APPENDS a nil-cost lot dated at the ex-date, because those shares have their
    own holding period. A split REWRITES the existing lots, because nothing was acquired
    and the original dates still govern.
    """
    from .tradebook import Lot

    live = [l for l in book if l.quantity > 0]
    if not live:
        return
    m = action.multiple

    if action.kind == BONUS:
        extra = sum(l.quantity for l in live) * m
        whole = int(round(extra))
        if whole > 0:
            book.append(Lot(action.symbol, action.when, whole, 0.0, 0.0))
        return

    for l in live:                      # SPLIT
        l.quantity = int(round(l.quantity * m))
        l.price = round(l.price / m, 4)
