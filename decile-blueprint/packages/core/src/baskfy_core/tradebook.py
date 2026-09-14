"""A broker's trade history, read and reasoned about without touching anything (law 1).

Kite's ``/trades`` endpoint takes no date and is flushed nightly, so a person's history exists in
exactly two places: Zerodha Console's tradebook export, and whatever was captured from the API on
the day it happened (NEEDS-MAULIK §32). This module reads the first and reasons about both.

**Strict, where the desk's parser was lenient.** ``kite-momentum-rebalancer``'s
``analytics/tradebook.py`` is the column contract this follows, but it read an unparseable number
as ``0.0`` and fell back through a bare ``except``. Under a money figure that is a quantity of
zero written silently, so here an unreadable quantity, price, date or side refuses the whole file
with the line number. House rule 3: nothing is swallowed.

**Equity delivery only.** A Console export can carry F&O and currency rows. They are counted and
skipped, never guessed into an equity position: an option's "quantity" is lots of a contract, not
shares of the stock its symbol starts with.

**What trades cannot tell you.** Bonuses, splits and demergers change a holding without a trade,
and shares transferred in from another demat have no buy. :func:`history_for` therefore never
claims a purchase date for a holding the trades do not add up to — it reports the mismatch.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from baskfy_core.curated_accounting import CashFlow, xirr


class TradebookError(ValueError):
    """The file cannot be read without guessing. The message names the line and the field."""


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


_BUY_WORDS: Final = frozenset({"buy", "b", "bought", "purchase"})
_SELL_WORDS: Final = frozenset({"sell", "s", "sold", "sale"})

#: Console's equity segment names, and exchanges that carry cash equity.
_EQUITY_SEGMENTS: Final = frozenset({"eq", "equity", "cash", ""})
_EQUITY_EXCHANGES: Final = frozenset({"NSE", "BSE"})

_DATE_FORMATS: Final = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d")
_DATETIME_FORMATS: Final = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
)

#: Where ``csv.DictReader`` puts fields beyond the header — a sign a number held a bare comma.
_EXTRA: Final = "__extra_fields__"

#: The largest file a person should need: 20 years of an active account is well under this.
MAX_TRADEBOOK_ROWS: Final = 200_000


@dataclass(frozen=True, slots=True)
class TradeFill:
    """One execution, in the broker's own words. Quantities are shares; prices are rupees."""

    symbol: str
    exchange: str
    side: TradeSide
    quantity: Decimal
    price: Decimal
    trade_date: dt.date
    trade_id: str
    order_id: str | None = None
    isin: str | None = None
    segment: str | None = None
    executed_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ParsedTradebook:
    fills: tuple[TradeFill, ...]
    #: Rows for derivatives, currency or commodity segments — counted, not read.
    skipped_non_equity: int


def _decimal(raw: str | None, *, field: str, line: int) -> Decimal:
    text = (raw or "").replace(",", "").replace("₹", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise TradebookError(f"line {line}: {field} {raw!r} is not a number") from exc
    if not value.is_finite():
        raise TradebookError(f"line {line}: {field} {raw!r} is not a number")
    return value


def _date(raw: str | None, *, line: int) -> dt.date:
    text = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise TradebookError(f"line {line}: trade date {raw!r} is not a date this reader knows")


def _datetime(raw: str | None) -> dt.datetime | None:
    """The execution time, when the file has a readable one. Naive: Console prints IST."""
    text = (raw or "").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _column(columns: dict[str, str], *needles: str, exclude: tuple[str, ...] = ()) -> str | None:
    for key, original in columns.items():
        if all(n in key for n in needles) and not any(x in key for x in exclude):
            return original
    return None


def parse_tradebook_csv(text: str) -> ParsedTradebook:  # noqa: PLR0912, PLR0915 - one pass, one refusal each
    """Read a Console tradebook export. Raises :class:`TradebookError` rather than half-parse.

    Expected columns (Console's names; order and case do not matter): ``symbol, isin, trade_date,
    exchange, segment, series, trade_type, auction, quantity, price, trade_id, order_id,
    order_execution_time``. ``symbol, trade_date, trade_type, quantity, price, trade_id`` are
    required: without a trade id a second import of the same file could not be told from new
    trades.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), restkey=_EXTRA)
    if not reader.fieldnames:
        raise TradebookError("the file is empty or is not a CSV")
    columns = {(name or "").strip().lower(): name for name in reader.fieldnames}

    c_symbol = _column(columns, "symbol") or _column(columns, "tradingsymbol")
    c_side = _column(columns, "trade", "type") or _column(columns, "transaction")
    c_qty = _column(columns, "quantity") or _column(columns, "qty")
    c_price = _column(columns, "price", exclude=("average",))
    c_date = _column(columns, "trade", "date") or _column(columns, "date", exclude=("time",))
    c_trade_id = _column(columns, "trade", "id")
    c_order_id = _column(columns, "order", "id")
    c_time = _column(columns, "execution", "time") or _column(columns, "order", "time")
    c_exchange = _column(columns, "exchange")
    c_segment = _column(columns, "segment")
    c_isin = _column(columns, "isin")

    required = {
        "symbol": c_symbol,
        "trade_date": c_date,
        "trade_type": c_side,
        "quantity": c_qty,
        "price": c_price,
        "trade_id": c_trade_id,
    }
    missing = [name for name, column in required.items() if column is None]
    if missing or c_symbol is None or c_date is None or c_side is None:
        raise TradebookError(
            f"missing columns {missing}. Export console.zerodha.com → Reports → Tradebook as CSV. "
            f"Found: {list(reader.fieldnames)}"
        )
    if c_qty is None or c_price is None or c_trade_id is None:  # pragma: no cover - covered above
        raise TradebookError(f"missing columns {missing}")

    fills: list[TradeFill] = []
    skipped = 0
    for line, row in enumerate(reader, start=2):
        if line > MAX_TRADEBOOK_ROWS + 1:
            raise TradebookError(f"more than {MAX_TRADEBOOK_ROWS} rows; split the export by year")
        # Any field past the header refuses, even an empty one: "4,000" unquoted shifts every
        # later column one place right, and the overflow is often the empty last column.
        if row.get(_EXTRA) is not None:
            raise TradebookError(
                f"line {line}: more fields than the header — an unquoted comma inside a number "
                "would mis-read a quantity"
            )
        symbol = (row.get(c_symbol) or "").strip().upper()
        if not symbol:
            continue
        exchange = (row.get(c_exchange) or "NSE").strip().upper() if c_exchange else "NSE"
        segment = (row.get(c_segment) or "").strip() if c_segment else ""
        if exchange not in _EQUITY_EXCHANGES or segment.lower() not in _EQUITY_SEGMENTS:
            skipped += 1
            continue
        side_raw = (row.get(c_side) or "").strip().lower()
        if side_raw in _BUY_WORDS:
            side = TradeSide.BUY
        elif side_raw in _SELL_WORDS:
            side = TradeSide.SELL
        else:
            raise TradebookError(f"line {line} ({symbol}): trade type {side_raw!r} is not buy/sell")
        quantity = abs(_decimal(row.get(c_qty), field="quantity", line=line))
        if quantity == 0:
            raise TradebookError(f"line {line} ({symbol}): quantity is zero")
        price = _decimal(row.get(c_price), field="price", line=line)
        if price < 0:
            raise TradebookError(f"line {line} ({symbol}): price is negative")
        trade_id = (row.get(c_trade_id) or "").strip()
        if not trade_id:
            raise TradebookError(f"line {line} ({symbol}): no trade id")
        order_id = (row.get(c_order_id) or "").strip() if c_order_id else ""
        isin = (row.get(c_isin) or "").strip() if c_isin else ""
        fills.append(
            TradeFill(
                symbol=symbol,
                exchange=exchange,
                side=side,
                quantity=quantity,
                price=price,
                trade_date=_date(row.get(c_date), line=line),
                trade_id=trade_id,
                order_id=order_id or None,
                isin=isin or None,
                segment=segment or None,
                executed_at=_datetime(row.get(c_time)) if c_time else None,
            )
        )
    if not fills and skipped == 0:
        raise TradebookError("no trades in the file")
    return ParsedTradebook(fills=tuple(fills), skipped_non_equity=skipped)


# ---------------------------------------------------------------------------
# What the trades say about a holding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeHistory:
    """One instrument's trades, measured against what is held now.

    ``reconciles`` is the gate on everything else: only when the trades net to the held quantity
    is ``first_bought_on`` a fact rather than a guess. A bonus, a split or a transfer-in breaks it,
    and then the holding keeps "since grouped" and says why.
    """

    net_quantity: Decimal
    held_quantity: Decimal
    reconciles: bool
    #: The oldest date among the lots still open under FIFO. ``None`` unless ``reconciles``.
    first_bought_on: dt.date | None
    #: Oldest trade of any kind, for the "history starts" label.
    first_trade_on: dt.date | None
    #: FIFO cost of the lots still open; ``None`` unless ``reconciles``.
    open_cost: Decimal | None
    #: Sells matched to a buy the trades do not contain — history that starts mid-position.
    unmatched_sell_quantity: Decimal


def _ordered(fills: Iterable[TradeFill]) -> list[TradeFill]:
    # Buys before sells within a day: a same-day round trip must not look like a short sale.
    return sorted(
        fills,
        key=lambda f: (
            f.trade_date,
            f.executed_at or dt.datetime.combine(f.trade_date, dt.time.min),
            0 if f.side is TradeSide.BUY else 1,
            f.trade_id,
        ),
    )


@dataclass(slots=True)
class _Lot:
    quantity: Decimal
    price: Decimal
    bought_on: dt.date


def history_for(fills: Sequence[TradeFill], held_quantity: Decimal) -> TradeHistory:
    """FIFO over one instrument's fills. Pure; the caller groups fills by instrument."""
    lots: list[_Lot] = []
    unmatched = Decimal("0")
    net = Decimal("0")
    for fill in _ordered(fills):
        if fill.side is TradeSide.BUY:
            lots.append(_Lot(fill.quantity, fill.price, fill.trade_date))
            net += fill.quantity
            continue
        net -= fill.quantity
        remaining = fill.quantity
        while remaining > 0 and lots:
            take = min(lots[0].quantity, remaining)
            lots[0].quantity -= take
            remaining -= take
            if lots[0].quantity == 0:
                lots.pop(0)
        unmatched += remaining
    reconciles = unmatched == 0 and net == held_quantity
    open_dates = [lot.bought_on for lot in lots]
    open_cost = sum((lot.quantity * lot.price for lot in lots), Decimal("0"))
    return TradeHistory(
        net_quantity=net,
        held_quantity=held_quantity,
        reconciles=reconciles,
        first_bought_on=min(open_dates) if reconciles and open_dates else None,
        first_trade_on=min((f.trade_date for f in fills), default=None),
        open_cost=open_cost if reconciles else None,
        unmatched_sell_quantity=unmatched,
    )


def trade_flows(fills: Iterable[TradeFill]) -> list[CashFlow]:
    """Buys as money in (negative), sells as money out (positive), dated by trade date."""
    return [
        CashFlow(
            on=fill.trade_date,
            amount=-(fill.quantity * fill.price)
            if fill.side is TradeSide.BUY
            else fill.quantity * fill.price,
        )
        for fill in fills
    ]


def xirr_since_first_purchase(
    fills: Sequence[TradeFill], *, closing_value: Decimal, as_of: dt.date
) -> Decimal | None:
    """Money-weighted return over the trades plus what their open positions are worth now.

    The caller passes only instruments whose history reconciles (or has fully closed), so the
    closing value and the flows describe the same shares. ``None`` when nothing solves.
    """
    flows = trade_flows(fills)
    if closing_value > 0:
        flows.append(CashFlow(on=as_of, amount=closing_value))
    return xirr(flows)
