"""Zerodha Console tradebook importer — rebuilds FIFO lots into the trades table.

WHY THIS EXISTS
kiteconnect exposes only today's orders and trades; there is no historical tradebook API.
Without acquisition dates every FIFO tax review returns TAX_DATA_UNKNOWN, which is the
correct fail-safe but leaves tax flagging inert. The Console export
(console.zerodha.com -> Reports -> Tradebook) is the authoritative history, so it is
parsed here rather than guessed at anywhere else.

WHAT IT PRODUCES
Buys open lots; sells consume them oldest-first. Each consumed slice becomes a closed
trade row (entry_ts, exit_ts, realised pnl); whatever is left over becomes an open lot
(exit_ts NULL), which is exactly the shape app/analytics/tax_lots.py reads.

IDEMPOTENCY
The tradebook is the source of truth, so an import REBUILDS every row for the symbols the
file covers. Re-importing the same file therefore changes nothing. Strategy annotations
that the broker cannot know — entry_score, exit_reason — are carried across the rebuild.

WHAT IT CANNOT KNOW
- Charges. The tradebook has no brokerage/STT columns (the P&L report does), so costs are
  recorded as 0 unless the file carries them. tax_drag() adds costs back before taxing, so
  a zero here understates cost, never the tax.
- Corporate actions. Bonuses and splits change quantity without a trade, so reconstructed
  quantities can drift from the real holding. reconcile_with_holdings() detects that
  instead of letting it pass silently.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from . import db

log = logging.getLogger("tradebook")

BUY_WORDS = {"buy", "b", "bought", "purchase"}
SELL_WORDS = {"sell", "s", "sold", "sale"}

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y",
                 "%Y/%m/%d", "%m/%d/%Y")
_DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                     "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


class TradebookError(ValueError):
    pass


@dataclass(frozen=True)
class Fill:
    symbol: str
    when: dt.datetime
    side: str                 # BUY | SELL
    quantity: int
    price: float
    exchange: str = "NSE"
    trade_id: str = ""
    charges: float = 0.0

    @property
    def ts(self) -> float:
        return self.when.timestamp()


@dataclass
class Lot:
    symbol: str
    when: dt.datetime
    quantity: int
    price: float
    charges: float = 0.0


@dataclass
class Rebuild:
    open_lots: list[Lot] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    unmatched_sells: list[dict] = field(default_factory=list)


# =====================================================================================
# parsing
# =====================================================================================
def _parse_when(date_raw: str, time_raw: str = "") -> dt.datetime | None:
    """Prefer the execution timestamp; fall back to the trade date at market open."""
    for raw in (time_raw or "").strip(), "":
        if not raw:
            continue
        for fmt in _DATETIME_FORMATS:
            try:
                return dt.datetime.strptime(raw, fmt)
            except ValueError:
                continue
        try:
            import pandas as pd
            # dayfirst: Indian exports are DD-MM-YYYY. Month-first would read
            # 01-07-2025 as 7 January and mis-date every lot by months.
            return pd.to_datetime(raw, dayfirst=True).to_pydatetime()
        except Exception:
            pass
    d = (date_raw or "").strip()
    if not d:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.combine(dt.datetime.strptime(d, fmt).date(),
                                       dt.time(9, 15))
        except ValueError:
            continue
    return None


def _num(raw) -> float:
    s = str(raw or "").replace(",", "").replace("₹", "").strip()
    if not s or s in {"-", "—"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_tradebook(path: str) -> list[Fill]:
    """Read a Console tradebook export. Column names are matched leniently.

    Expected shape (columns may be reordered or renamed slightly):
        symbol, isin, trade_date, exchange, segment, series, trade_type,
        auction, quantity, price, trade_id, order_id, order_execution_time
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise TradebookError(f"{path}: empty or unreadable CSV")
        cols = {(c or "").strip().lower(): c for c in reader.fieldnames}

        def find(*needles, exclude=()):
            for key, original in cols.items():
                if all(n in key for n in needles) and not any(x in key for x in exclude):
                    return original
            return None

        c_sym = find("symbol") or find("tradingsymbol") or find("instrument")
        c_side = find("trade", "type") or find("transaction") or find("buy")
        c_qty = find("quantity") or find("qty")
        c_price = find("price", exclude=("average",)) or find("price")
        c_date = find("trade", "date") or find("date", exclude=("time",))
        c_time = find("execution", "time") or find("order", "time") or find("timestamp")
        c_exch = find("exchange")
        c_tid = find("trade", "id")
        c_chg = find("charge") or find("brokerage")

        missing = [n for n, c in (("symbol", c_sym), ("trade_type", c_side),
                                  ("quantity", c_qty), ("price", c_price)) if not c]
        if missing:
            raise TradebookError(
                f"{path}: missing required columns {missing}. Export "
                f"console.zerodha.com -> Reports -> Tradebook. Found: {reader.fieldnames}")

        fills: list[Fill] = []
        for line_no, row in enumerate(reader, start=2):
            extra = row.get(None)
            if extra and any(str(v).strip() for v in extra):
                raise TradebookError(
                    f"{path}: line {line_no} has more fields than the header — an "
                    f"unquoted comma inside a number would silently mis-read quantities.")
            sym = (row.get(c_sym) or "").strip().upper()
            if not sym:
                continue
            side_raw = (row.get(c_side) or "").strip().lower()
            side = "BUY" if side_raw in BUY_WORDS else "SELL" if side_raw in SELL_WORDS else ""
            if not side:
                raise TradebookError(
                    f"{path}: line {line_no} has an unrecognised trade type "
                    f"{side_raw!r}; refusing to guess buy vs sell")
            when = _parse_when(row.get(c_date, ""), row.get(c_time, "") if c_time else "")
            if when is None:
                raise TradebookError(
                    f"{path}: line {line_no} ({sym}) has no parseable date. An "
                    f"acquisition date cannot be inferred, and a lot without one is "
                    f"never assumed tax-safe.")
            qty = int(abs(_num(row.get(c_qty))))
            if qty <= 0:
                continue
            fills.append(Fill(
                symbol=sym, when=when, side=side, quantity=qty,
                price=_num(row.get(c_price)),
                exchange=(row.get(c_exch) or "NSE").strip().upper() if c_exch else "NSE",
                trade_id=(row.get(c_tid) or "").strip() if c_tid else "",
                charges=_num(row.get(c_chg)) if c_chg else 0.0))

    if not fills:
        raise TradebookError(f"{path}: no usable trades found")
    return sorted(fills, key=lambda f: (f.symbol, f.when))


# =====================================================================================
# FIFO reconstruction
# =====================================================================================
def build_lots(fills: Sequence[Fill]) -> Rebuild:
    """Buys open lots; sells consume them oldest-first."""
    out = Rebuild()
    books: dict[str, list[Lot]] = {}

    for f in sorted(fills, key=lambda x: (x.when, x.symbol)):
        book = books.setdefault(f.symbol, [])
        if f.side == "BUY":
            book.append(Lot(f.symbol, f.when, f.quantity, f.price, f.charges))
            continue

        remaining = f.quantity
        while remaining > 0 and book:
            lot = book[0]
            take = min(remaining, lot.quantity)
            share = (f.charges * take / f.quantity) if f.quantity else 0.0
            cost_share = (lot.charges * take / lot.quantity) if lot.quantity else 0.0
            out.closed.append({
                "symbol": f.symbol, "entry_ts": lot.when.timestamp(),
                "exit_ts": f.when.timestamp(), "qty": take,
                "entry_price": lot.price, "exit_price": f.price,
                "pnl": round((f.price - lot.price) * take, 2),
                "costs": round(share + cost_share, 2)})
            lot.quantity -= take
            lot.charges -= cost_share
            remaining -= take
            if lot.quantity <= 0:
                book.pop(0)
        if remaining > 0:
            # A sell with no inventory: a short, an intraday leg, or history that starts
            # mid-position. Recorded, never silently dropped — it means the lots are
            # incomplete and the tax review must say so.
            out.unmatched_sells.append({
                "symbol": f.symbol, "date": f.when.date().isoformat(),
                "quantity": remaining, "price": f.price})

    for book in books.values():
        out.open_lots.extend(l for l in book if l.quantity > 0)
    out.open_lots.sort(key=lambda l: (l.symbol, l.when))
    return out


# =====================================================================================
# persistence
# =====================================================================================
def import_tradebook(conn, path: str, *, replace: bool = True) -> dict:
    """Parse, rebuild and store. Re-importing the same file is a no-op."""
    fills = parse_tradebook(path)
    rebuild = build_lots(fills)
    symbols = sorted({f.symbol for f in fills})

    # Strategy annotations the broker cannot know are preserved across the rebuild.
    kept: dict[tuple, tuple] = {}
    if replace:
        for r in conn.execute(
                "SELECT symbol, entry_ts, qty, entry_score, exit_reason FROM trades "
                f"WHERE symbol IN ({','.join('?' * len(symbols))})", symbols):
            if r["entry_score"] is not None or r["exit_reason"] is not None:
                kept[(r["symbol"], r["entry_ts"], r["qty"])] = (r["entry_score"],
                                                               r["exit_reason"])

    rows = []
    for c in rebuild.closed:
        ann = kept.get((c["symbol"], c["entry_ts"], c["qty"]), (None, None))
        rows.append((c["symbol"], c["entry_ts"], c["exit_ts"], c["qty"],
                     c["entry_price"], c["exit_price"], ann[0], ann[1],
                     c["pnl"], c["costs"]))
    for l in rebuild.open_lots:
        ann = kept.get((l.symbol, l.when.timestamp(), l.quantity), (None, None))
        rows.append((l.symbol, l.when.timestamp(), None, l.quantity, l.price, None,
                     ann[0], ann[1], None, round(l.charges, 2)))

    with db.transaction(conn):
        if replace and symbols:
            conn.execute(f"DELETE FROM trades WHERE symbol IN "
                         f"({','.join('?' * len(symbols))})", symbols)
        conn.executemany(
            "INSERT INTO trades(symbol, entry_ts, exit_ts, qty, entry_price, exit_price,"
            " entry_score, exit_reason, pnl, costs) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

    realised = sum(c["pnl"] for c in rebuild.closed)
    return {"file": path, "fills": len(fills), "symbols": len(symbols),
            "open_lots": len(rebuild.open_lots), "closed_trades": len(rebuild.closed),
            "unmatched_sells": rebuild.unmatched_sells,
            "realised_pnl": round(realised, 2),
            "first_trade": min(f.when for f in fills).date().isoformat(),
            "last_trade": max(f.when for f in fills).date().isoformat()}


def reconcile_with_holdings(conn, holdings: Iterable[Mapping]) -> list[dict]:
    """Compare reconstructed open quantity against the real holding, per symbol.

    A mismatch usually means a corporate action (bonus, split, merger) moved quantity
    without a trade, or that the export does not reach back far enough. Either way the
    lots are wrong for tax purposes, so it is surfaced rather than assumed away.
    """
    recon = {r["symbol"]: r["qty"] for r in conn.execute(
        "SELECT symbol, SUM(qty) qty FROM trades WHERE exit_ts IS NULL GROUP BY symbol")}
    out = []
    for h in holdings:
        sym = h["symbol"]
        actual = int(h.get("quantity") or 0)
        lots = int(recon.get(sym, 0))
        if actual != lots:
            out.append({
                "symbol": sym, "holding_qty": actual, "lot_qty": lots,
                "difference": actual - lots,
                "likely_cause": ("corporate action or export starts mid-position"
                                 if lots and actual > lots else
                                 "no tradebook history for this symbol" if not lots else
                                 "lots exceed the holding — check for a missing sell")})
    return out


def coverage(conn, holdings: Iterable[Mapping]) -> dict:
    """How much of the book the tax module can actually reason about."""
    recon = {r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM trades WHERE exit_ts IS NULL")}
    held = [h["symbol"] for h in holdings]
    covered = [s for s in held if s in recon]
    return {"holdings": len(held), "with_lots": len(covered),
            "coverage_pct": round(len(covered) / len(held) * 100, 1) if held else 0.0,
            "missing": sorted(set(held) - recon)}


# =====================================================================================
# CLI
# =====================================================================================
def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="import a Zerodha Console tradebook export")
    ap.add_argument("--file", help="tradebook CSV from console.zerodha.com")
    ap.add_argument("--db", default=None)
    ap.add_argument("--reconcile", action="store_true",
                    help="compare reconstructed lots against live Kite holdings")
    ap.add_argument("--summary", action="store_true", help="what is stored right now")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect(a.db) as conn:
        db.migrate(conn)

        if a.file:
            try:
                res = import_tradebook(conn, a.file)
            except TradebookError as exc:
                raise SystemExit(f"import failed: {exc}")
            print(f"imported {res['fills']} fills across {res['symbols']} symbols "
                  f"({res['first_trade']} -> {res['last_trade']})")
            print(f"  open lots      {res['open_lots']}")
            print(f"  closed trades  {res['closed_trades']}  "
                  f"realised Rs {res['realised_pnl']:,.0f}")
            if res["unmatched_sells"]:
                print(f"  UNMATCHED SELLS: {len(res['unmatched_sells'])} — the export "
                      f"does not cover the full history for these")
                for u in res["unmatched_sells"][:10]:
                    print(f"    {u['date']}  {u['symbol']:<14}{u['quantity']:>7} "
                          f"@ {u['price']}")

        if a.summary or a.file:
            row = conn.execute(
                "SELECT COUNT(*) n, SUM(exit_ts IS NULL) open FROM trades").fetchone()
            print(f"\ntrades table: {row['n']} rows, {row['open'] or 0} open lots")

        if a.reconcile:
            from ..kite_client import Kite
            k = Kite()
            if not k.is_authed():
                raise SystemExit("Kite session expired — log in at / first")
            holdings = [h for h in k.holdings()
                        if not h["symbol"].upper().startswith("SGB")]
            cov = coverage(conn, holdings)
            print(f"\nlot coverage: {cov['with_lots']}/{cov['holdings']} holdings "
                  f"({cov['coverage_pct']}%)")
            if cov["missing"]:
                print(f"  no lots for: {', '.join(cov['missing'])}")
            problems = reconcile_with_holdings(conn, holdings)
            if problems:
                print(f"\n{len(problems)} quantity mismatches:")
                print(f"  {'symbol':<16}{'holding':>9}{'lots':>9}{'diff':>9}  likely cause")
                for p in problems:
                    print(f"  {p['symbol']:<16}{p['holding_qty']:>9}{p['lot_qty']:>9}"
                          f"{p['difference']:>+9}  {p['likely_cause']}")
            else:
                print("  every holding reconciles against its lots")


if __name__ == "__main__":
    _main()
