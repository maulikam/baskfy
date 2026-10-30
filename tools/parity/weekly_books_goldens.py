"""Goldens for the desk's weekly_books sub-lane (DECISIONS-RS R8.5): the Python modules' own
answers on deterministic inputs, for `baskfy-desk`'s `weekly_books::parity` test to reproduce
byte for byte (`json.dumps` text of every output).

The modules have no route of their own (their pages are other lanes'), so this is the parity
gate for them in place of a `desk_diff.py` scenario. Synthetic inputs only — never
`portfolio.db`, never a live account, no network.

Run (TZ matters: the desk's naive local time is IST, as in `desk_diff.py`):
    TZ=Asia/Kolkata PYTHONPATH=<wt>/kite-momentum-rebalancer:<wt>/decile-blueprint/packages/execution/src \
      <main>/kite-momentum-rebalancer/.venv/bin/python tools/parity/weekly_books_goldens.py \
      rust/crates/baskfy-desk/fixtures/weekly_books/goldens.json
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import tempfile

assert os.environ.get("TZ") == "Asia/Kolkata", "run with TZ=Asia/Kolkata"

from app import reconcile as R  # noqa: E402
from app.analytics import db  # noqa: E402
from app.analytics import holdings_view as H  # noqa: E402
from app.analytics import reconcile_view as RV  # noqa: E402
from app.analytics import snapshot as S  # noqa: E402
from app.analytics import tax_lots as TL  # noqa: E402
from app.analytics import tradebook as TB  # noqa: E402

NOW = dt.datetime(2026, 8, 15, 18, 0, 0)


class _FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return NOW if tz is None else NOW.replace(tzinfo=tz)


class _FixedDate(dt.date):
    @classmethod
    def today(cls):  # noqa: ANN206
        return NOW.date()


class _DtShim:
    """`dt` as the modules see it, with now()/today() frozen."""

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(dt, name)

    datetime = _FixedDT
    date = _FixedDate


for mod in (H, TB, S):
    mod.dt = _DtShim()


def fill_row(f: TB.Fill) -> list:
    return [f.symbol, f.when.isoformat(), f.side, f.quantity, f.price, f.exchange, f.trade_id, f.charges]


cases: list[dict] = []

# --- holdings_view ------------------------------------------------------------------------
book = [
    {"symbol": "AAA", "quantity": 100, "average_price": 90.0, "last_price": 100.0, "pledged_qty": 0, "exchange": "NSE"},
    {"symbol": "BBB", "quantity": 37, "average_price": 1234.567, "last_price": 987.65, "pledged_qty": 37, "exchange": "NSE"},
    {"symbol": "SGBDE31III", "quantity": 392, "average_price": 5000.0, "last_price": 6123.45, "pledged_qty": 0, "exchange": "NSE"},
    {"symbol": "CCC", "quantity": 5, "average_price": 0, "last_price": 41.05, "pledged_qty": 0, "exchange": ""},
    {"symbol": "DDD", "quantity": 250, "average_price": 210.0, "last_price": 150.25, "pledged_qty": 0, "exchange": "NSE"},
]
raw = [
    {"tradingsymbol": "AAA", "day_change": -2.5, "day_change_percentage": -2.4390243, "t1_quantity": 10, "isin": "INAAA"},
    {"tradingsymbol": "BBB", "day_change": 3.333, "day_change_percentage": 0.33870, "t1_quantity": 0, "isin": "INBBB"},
    {"tradingsymbol": "CCC", "exchange": "BSE"},
    {"tradingsymbol": "DDD", "day_change": 0.0, "day_change_percentage": 0.0},
]
scan = {"AAA": {"marketcap": 150000.4, "beta": 1.2, "away_from_high_one_year": -12.5},
        "BBB": {"marketcap": 25000, "beta": 0.85},
        "DDD": {"marketcap": 4999.5, "beta": None}}
fills = [{"symbol": "AAA", "side": "BUY", "when_ts": (NOW - dt.timedelta(days=400, hours=3)).timestamp()},
         {"symbol": "AAA", "side": "buy", "when_ts": (NOW - dt.timedelta(days=10)).timestamp()},
         {"symbol": "BBB", "side": "BUY", "when_ts": (NOW - dt.timedelta(days=364, seconds=1)).timestamp()},
         {"symbol": "BBB", "side": "SELL", "when_ts": (NOW - dt.timedelta(days=500)).timestamp()}]
protected = {"AAA": "ok", "BBB": "partial", "DDD": "missing"}
for cash in (0.0, 125000.75):
    rows = H.rows(book, raw=raw, scan=scan, fills=fills, protected=protected)
    summ = H.summary(rows, cash=cash)
    cases.append({"kind": "holdings", "input": {"holdings": book, "raw": raw, "scan": scan, "fills": fills,
                                                "protected": protected, "cash": cash, "now": NOW.isoformat()},
                  "output": {"rows": rows, "summary": summ, "allocation": H.allocation(rows, cash=cash),
                             "warnings": H.warnings(rows, summ)}})

# An empty book (review addition): the sums are Python ints, so top5_weight is `0`, not `0.0`.
for cash in (0.0, 5000.5):
    rows = H.rows([], raw=[], scan={}, fills=[], protected={})
    summ = H.summary(rows, cash=cash)
    cases.append({"kind": "holdings", "input": {"holdings": [], "raw": [], "scan": {}, "fills": [],
                                                "protected": {}, "cash": cash, "now": NOW.isoformat()},
                  "output": {"rows": rows, "summary": summ, "allocation": H.allocation(rows, cash=cash),
                             "warnings": H.warnings(rows, summ)}})

# --- tax_lots -----------------------------------------------------------------------------
tax_inputs = [
    {"qty": 150, "price": 100.0, "as_of": "2026-08-14", "review_days": 45,
     "lots": [{"quantity": 100, "acquired_on": "2025-09-19", "price": 50.0},
              {"quantity": 100, "acquired_on": "2024-01-05", "price": 120.0}]},
    {"qty": 30, "price": 10.0, "as_of": "2026-08-14", "review_days": 45,
     "lots": [{"quantity": 10, "acquired_on": None, "price": 1.0},
              {"quantity": 10, "acquired_on": "2026-01-01", "price": None}]},
    {"qty": 0, "price": 10.0, "as_of": "2026-08-14", "review_days": 10, "lots": []},
    {"qty": 5, "price": 333.33, "as_of": "2026-08-14", "review_days": 400,
     "lots": [{"quantity": 7, "acquired_on": "2026-02-01", "price": 111.11, "corporate_action_adjusted": True}]},
]
for t in tax_inputs:
    lots = TL.lots_from_rows("X", t["lots"])
    rev = TL.review_sale("X", t["qty"], t["price"], lots, as_of=dt.date.fromisoformat(t["as_of"]),
                         review_days=t["review_days"])
    cases.append({"kind": "tax", "input": t,
                  "output": {"review": rev.as_dict(), "items": TL.manual_review_items({"X": rev})}})

# --- tradebook: parsing ---------------------------------------------------------------------
HEADER = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,"
          "quantity,price,trade_id,order_id,order_execution_time")
csvs = [
    HEADER + "\nrrkabel,INE1,2025-07-01,nse,EQ,EQ,Buy,false,100,1900.5,T1,O1,2025-07-01T09:30:00\n"
             "RRKABEL,INE1,15-01-2026,NSE,EQ,EQ,sold,false,40,2500,T2,O2,\n"
             "ABC,INE2,01-Jul-2025,,EQ,EQ,b,false,\"1,500\",\"₹2,150.75\",,O3,01-07-2025T10:11:12\n"
             ",INE3,2025-07-01,NSE,EQ,EQ,buy,false,1,1,T4,O4,\n"
             "ZERO,INE4,2025-07-01,NSE,EQ,EQ,buy,false,0,1,T5,O5,\n",
    "Symbol,Trade Date,Trade Type,Qty,Price,Charges\nX,2025-01-01,buy,10,5.5,1.25\nX,2025-03-01,sell,4,6,0.5\n",
    HEADER + "\nX,INE1,not-a-date,NSE,EQ,EQ,buy,false,10,100,T1,O1,\n",
    HEADER + "\nX,INE1,2025-07-01,NSE,EQ,EQ,transfer,false,10,100,T1,O1,\n",
    "symbol,trade_date\nX,2025-07-01\n",
    HEADER + "\nX,INE1,2025-07-01,NSE,EQ,EQ,buy,false,1,500,2150.75,T1,O1,2025-07-01T09:30:00\n",
    HEADER + "\n",
    # Review additions: execution times only pandas reads, blank lines, a whitespace-only time.
    HEADER + "\nP,INE1,2025-07-01,NSE,EQ,EQ,buy,false,1,10,T1,O1,2025-07-02 09:30\n\n"
             "Q,INE1,2025-07-01,NSE,EQ,EQ,buy,false,2,10,T2,O2,03/07/2025 10:11:12\n"
             "R,INE1,2025-07-01,NSE,EQ,EQ,buy,false,3,10,T3,O3,   \n"
             "S,INE1,2025-07-01,NSE,EQ,EQ,buy,false,4,10,T4,O4,2025-07-04\n"
             "T,INE1,2025-07-01,NSE,EQ,EQ,sell,false,-5,10,T5,O5,2025/07/05 11:00:00\n",
]
with tempfile.TemporaryDirectory() as tmp:
    for i, text in enumerate(csvs):
        path = os.path.join(tmp, f"tb{i}.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            out = {"fills": [fill_row(x) for x in TB.parse_tradebook(path)]}
        except TB.TradebookError as exc:
            out = {"error": str(exc).replace(path, "<path>")}
        cases.append({"kind": "parse", "input": {"csv": text}, "output": out})

# --- tradebook: FIFO and keys ---------------------------------------------------------------
from app.analytics.corporate_actions import Action  # noqa: E402

fifo_inputs = [
    {"fills": [["X", "2025-01-01T09:15:00", "BUY", 100, 50.0, "NSE", "", 10.0],
               ["X", "2025-06-01T09:15:00", "BUY", 100, 80.0, "NSE", "", 7.5],
               ["Y", "2025-02-01T10:00:00", "BUY", 3, 1.1, "NSE", "", 0.0],
               ["X", "2026-01-01T11:00:00.250000", "SELL", 150, 100.0, "NSE", "", 3.3],
               ["Y", "2025-03-01T10:00:00", "SELL", 5, 1.3, "NSE", "", 0.0]],
     "actions": [["X", "bonus", "2025-03-01", 1.0, 2.0], ["X", "split", "2025-09-01", 5.0, 1.0]]},
]
for fi in fifo_inputs:
    fl = [TB.Fill(s, dt.datetime.fromisoformat(w), sd, q, p, e, t, c) for s, w, sd, q, p, e, t, c in fi["fills"]]
    acts = [Action(s, k, dt.date.fromisoformat(d), n, o) for s, k, d, n, o in fi["actions"]]
    rb = TB.build_lots(fl, acts)
    cases.append({"kind": "fifo", "input": fi, "output": {
        "closed": rb.closed,
        "open": [[l.symbol, l.when.isoformat(), l.quantity, l.price, l.charges, l.when.timestamp()] for l in rb.open_lots],
        "unmatched": rb.unmatched_sells,
        "keys": [TB.fill_key(f) for f in fl]}})

# --- tradebook: the database round trip ---------------------------------------------------
db_text = (HEADER + "\nX,INE1,2025-01-01,NSE,EQ,EQ,buy,false,100,50,T1,O1,\n"
           "X,INE1,2025-02-01,NSE,EQ,EQ,buy,false,50,60.5,T2,O2,\n"
           "Y,INE2,2025-03-01,NSE,EQ,EQ,buy,false,10,7,T3,O3,\n"
           "Y,INE2,2025-04-01,NSE,EQ,EQ,sell,false,15,9,T4,O4,\n")
kite_trades = [{"trade_id": "K1", "tradingsymbol": "X", "transaction_type": "SELL", "quantity": 40,
                "average_price": 90.0, "product": "CNC", "exchange": "NSE", "fill_timestamp": "2026-08-15 10:00:00"},
               {"trade_id": "K2", "tradingsymbol": "Z", "transaction_type": "BUY", "quantity": 4,
                "average_price": 12.5, "product": "MIS", "exchange": "NSE", "fill_timestamp": "2026-08-15 10:00:00"}]
positions = [{"symbol": "X", "quantity": 110, "average_price": 55.123, "value": 9000},
             {"symbol": "Y", "quantity": 0, "average_price": 7, "value": 0},
             {"symbol": "W", "quantity": 3, "average_price": 1, "value": 30}]


class _K:
    def trades(self):  # noqa: ANN201
        return list(kite_trades)


with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "tradebook.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(db_text)
    with db.connect(os.path.join(tmp, "p.db")) as conn:
        db.migrate(conn)
        imp = TB.import_tradebook(conn, path)
        imp["file"] = "<path>"
        cap = TB.capture_live_trades(conn, _K())
        st = TB.status(conn, positions)
        trades = [list(r) for r in conn.execute(
            "SELECT symbol, entry_ts, exit_ts, qty, entry_price, exit_price, pnl, costs FROM trades ORDER BY id")]
        fl = [list(r) for r in conn.execute(
            "SELECT trade_id, symbol, when_ts, side, quantity, price, exchange, charges, source, captured_at "
            "FROM fills ORDER BY trade_id")]
cases.append({"kind": "tradebook_db", "input": {"csv": db_text, "trades": kite_trades, "positions": positions,
                                                "now": NOW.isoformat()},
              "output": {"import": imp, "capture": cap, "status": st, "trades": trades, "fills": fl}})

# --- reconcile_view -------------------------------------------------------------------------
plan = {"orders": [{"symbol": "AAA", "qty_final": 10}, {"symbol": "BBB", "qty_final": 5},
                   {"symbol": "CCC", "qty_final": 0}, {"symbol": "DDD", "qty_final": 7}]}
hold = [{"symbol": "AAA", "quantity": 3}, {"symbol": "BBB", "quantity": 5}, {"symbol": "SGB1", "quantity": 392},
        {"symbol": "DDD", "quantity": 20}, {"symbol": "EEE", "quantity": 1}]
cases.append({"kind": "positions", "input": {"plan": plan, "holdings": hold},
              "output": RV.positions_view(plan, hold)})
cases.append({"kind": "positions", "input": {"plan": None, "holdings": []}, "output": RV.positions_view(None, [])})

# --- reconcile ----------------------------------------------------------------------------
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
AT = dt.datetime(2026, 9, 28, 9, 20, 5, tzinfo=IST)


class _Book:
    def __init__(self, c):  # noqa: ANN001
        self.c = c

    def orders(self):  # noqa: ANN201
        return list(self.c["orders"])

    def get_gtts(self):  # noqa: ANN201
        return list(self.c["gtts"])

    def holdings(self):  # noqa: ANN201
        return list(self.c["holdings"])


class _Risk:
    def __init__(self):  # noqa: ANN204
        self.released = []

    def release(self, s, v):  # noqa: ANN001, ANN201
        self.released.append([s, v])


rc = {"orders": [{"order_id": "B1", "status": "CANCELLED", "filled_quantity": 40},
                 {"order_id": "B2", "status": "COMPLETE", "filled_quantity": 7}],
      "gtts": [{"id": 778, "status": "ACTIVE", "condition": {"tradingsymbol": "BIGCO", "trigger_values": [80.05]},
                "orders": [{"transaction_type": "SELL", "quantity": 150}]},
               {"id": 779, "status": "active", "condition": {"tradingsymbol": "GONECO", "trigger_values": [1]},
                "orders": [{"transaction_type": "BUY", "quantity": 1}, {"quantity": 100}]},
               {"id": 780, "status": "triggered", "condition": {"tradingsymbol": "FIRECO"},
                "orders": [{"transaction_type": "SELL", "quantity": 100}]},
               {"id": 781, "status": "deleted", "condition": {}, "orders": []},
               {"id": 782, "status": "active", "condition": {"tradingsymbol": "SMALLCO"},
                "orders": [{"transaction_type": "SELL", "quantity": 10}]}],
      "holdings": [{"symbol": "GONECO", "quantity": 60}, {"symbol": "FIRECO", "quantity": 100}],
      "sleeve_orders": [{"broker_order_id": "B1", "symbol": "TWTCO", "quantity": 100, "filled_quantity": 0,
                         "reference_price": "100.05"},
                        {"broker_order_id": "B2", "symbol": "TWTCO", "quantity": 7, "reference_price": None},
                        {"broker_order_id": "B9", "symbol": "TWTCO", "quantity": 1},
                        {"broker_order_id": "", "symbol": "TWTCO", "quantity": 1}],
      "positions": [{"id": 1, "symbol": "NAKEDCO", "quantity_open": 100, "gtt_id": None, "simulated": True},
                    {"id": 2, "symbol": "MISSCO", "quantity_open": 100, "gtt_id": "777", "simulated": True},
                    {"id": 3, "symbol": "BIGCO", "quantity_open": 100, "gtt_id": "778", "simulated": True},
                    {"id": 4, "symbol": "GONECO", "quantity_open": 100, "gtt_id": 779, "simulated": False},
                    {"id": 5, "symbol": "FIRECO", "quantity_open": 100, "gtt_id": "780", "simulated": False},
                    {"id": 6, "symbol": "DELCO", "quantity_open": 5, "gtt_id": "781", "simulated": True},
                    {"id": 7, "symbol": "SMALLCO", "quantity_open": 25, "gtt_id": "782", "simulated": True},
                    {"id": 8, "symbol": "DRYCO", "quantity_open": 25, "gtt_id": "DRY-1", "simulated": True},
                    {"id": 9, "symbol": "SHUT", "quantity_open": 0, "gtt_id": None, "simulated": True},
                    {"id": 10, "instrument_id": 42, "quantity_open": 3, "gtt_id": None, "simulated": False}]}
risk = _Risk()


async def _update(payload):  # noqa: ANN001, ANN202
    return "applied" if payload["status"] != "COMPLETE" else None


hooks = R.SleeveHooks("twt", open_orders=lambda: list(rc["sleeve_orders"]), on_order_update=_update,
                      open_positions=lambda: list(rc["positions"]), risk=risk)
run = asyncio.run(R.reconcile_once(_Book(rc), [hooks], now=AT, issues=R.MemoryIssueStore()))
cases.append({"kind": "reconcile", "input": rc, "output": {
    "line": run.line(), "released": risk.released,
    "issues": [[i.sleeve, i.position_id, i.symbol, i.kind, i.detail] for i in run.issues],
    "gtts": {k: [v.gtt_id, v.status, v.symbol, v.quantity, None if v.trigger is None else str(v.trigger)]
             for k, v in sorted(R.gtt_index(rc["gtts"]).items())}}})

# --- snapshot -----------------------------------------------------------------------------


class _SnapKite:
    def __init__(self, hs):  # noqa: ANN001, ANN204
        self.hs = hs

    def is_authed(self):  # noqa: ANN201
        return True

    def holdings(self):  # noqa: ANN201
        return list(self.hs)

    def available_cash(self):  # noqa: ANN201
        return 123456.789


class _NoLimit:
    async def api_slot(self):  # noqa: ANN201
        return None


snap_holdings = [{"symbol": "AAA", "exchange": "NSE", "quantity": 110, "pledged_qty": 10,
                  "average_price": 90.123456, "last_price": 100.05},
                 {"symbol": "SGBDE31III", "exchange": "NSE", "quantity": 5, "pledged_qty": 0,
                  "average_price": 5000, "last_price": 6123.4},
                 {"symbol": "BÉTA", "exchange": "BSE", "quantity": 3, "pledged_qty": 3,
                  "average_price": 1.0, "last_price": 1e-7}]
for include in (False, True):
    S.C.INCLUDE_UNTOUCHABLE_IN_NAV = include
    job = S.SnapshotJob(kite=_SnapKite(snap_holdings), limits=_NoLimit())
    snap = asyncio.run(job.collect(None, "holdings"))
    cases.append({"kind": "snapshot", "input": {"holdings": snap_holdings, "include": include,
                                                "today": NOW.date().isoformat()}, "output": snap})

out = sys.argv[1]
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"note": "tools/parity/weekly_books_goldens.py — Python's answers; never edit by hand.",
               "cases": cases}, f, indent=1)
print(f"{len(cases)} cases -> {out}")
