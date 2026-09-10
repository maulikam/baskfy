"""Rebuild the stop for a manually-entered swing position, and print the calls.

These 18 entries were taken by hand, so no `sw_position` row exists and the entry-day
low -- the stop, and the denominator of every R that follows -- was never recorded.
This reconstructs it and prints the exact GTT and sell calls.

    python -m scripts.swing_stop_plan --tradebook ~/Downloads/tradebook-EQ.csv
    python -m scripts.swing_stop_plan --entered PKTEA=2026-09-09 --entered ELLEN=2026-09-05
    python -m scripts.swing_stop_plan NOVARTIND ELLEN --tradebook tb.csv

WHERE THE ENTRY DATE COMES FROM, and why it is not automatic. Kite's /orders and /trades
are same-day only and Zerodha flushes them nightly (`kite_client.trades` says so in its own
docstring). Anything bought before today therefore exists only in a Console tradebook
export: Console -> Reports -> Tradebook -> the equity segment -> download CSV. Pass it with
--tradebook. The desk's own `fills` table is tried first for anything it happens to hold.

Rules applied, all from docs/swing/04-business-rules.md:
  6.1  stop = low of the entry day; widest = min(adr_pct x 1.0, 10%)
  6.2  trail = MA10 when adr_pct >= 6 else MA20
  6.3  partial = qty // 3
  6.4  manage() precedence: hard stop > close below trail > partial > breakeven
  6.5  a stop never falls
  9.4  the GTT's resting limit = trigger x SWING_GTT_LIMIT_FRACTION (0.97)

It PRINTS. There is no order call anywhere in this file. Placing them is a hand on a key.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal as D, ROUND_DOWN
from pathlib import Path

from app import config as C
from app.kite_client import Kite

DEFAULT_TICK = D("0.05")
ADR_BARS = 20                    # 04 §1, LiquidityConfig.adr_bars
FAST_TRAIL_MIN_ADR = D("6")      # 04 §6.2, StopConfig.fast_trail_min_adr_pct
MAX_STOP_PCT = D("10")           # 04 §6.1, StopConfig.max_stop_distance_pct
PARTIAL_FROM, PARTIAL_TO = 3, 5  # 04 §6.3
BLOCKED = ("SGB", "GS", "GOI")   # non-negotiable 7: untouchable, and the client would raise


def snap(x, tick: D = DEFAULT_TICK) -> D:
    """Down to the instrument's own tick. Down, always: a stop rounded up is a stop moved in.

    The tick is per symbol, not a constant -- `kite_client.place_gtt_stop` reads it for the
    same reason, because a trigger off the tick is rejected outright and the stop silently
    never exists.
    """
    return (D(str(x)) / tick).quantize(D("1"), rounding=ROUND_DOWN) * tick


def sma(closes: list[D], n: int) -> D | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def adr_pct(bars: list[dict], n: int = ADR_BARS) -> D:
    """04 §1: mean of (high/low - 1) x 100 over the last n bars, today included."""
    window = [b for b in bars[-n:] if b["low"]]
    if not window:
        return D(0)
    return sum((D(str(b["high"])) / D(str(b["low"])) - 1) * 100 for b in window) / len(window)


# ---------- where the entry date comes from ----------

def from_tradebook(path: Path) -> dict[str, date]:
    """Earliest BUY per symbol in a Zerodha Console tradebook CSV.

    Console has renamed these columns more than once, so the header is matched by
    substring rather than pinned. A row that yields neither a symbol nor a date is
    skipped silently: a partly-parsed export must not become a wrong stop.
    """
    out: dict[str, date] = {}
    with path.open(newline="") as fh:
        rows = list(csv.reader(fh))
    head_i = next((i for i, r in enumerate(rows)
                   if any("symbol" in c.lower() for c in r)), None)
    if head_i is None:
        raise SystemExit(f"{path}: no header row with a 'symbol' column -- is this a tradebook?")
    head = [c.strip().lower() for c in rows[head_i]]

    def col(*names):
        for n in names:
            for i, c in enumerate(head):
                if n in c:
                    return i
        return None

    i_sym, i_dt = col("symbol"), col("trade_date", "trade date", "date")
    i_side = col("trade_type", "trade type", "type")
    if i_sym is None or i_dt is None:
        raise SystemExit(f"{path}: need a symbol and a trade-date column")
    for r in rows[head_i + 1:]:
        if len(r) <= max(i_sym, i_dt):
            continue
        if i_side is not None and len(r) > i_side and "buy" not in r[i_side].strip().lower():
            continue
        sym, raw = r[i_sym].strip().upper(), r[i_dt].strip()[:10]
        if not sym or not raw:
            continue
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                d = datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
            out[sym] = min(out.get(sym, d), d)
            break
    return out


def from_desk_fills(db: Path) -> dict[str, date]:
    """The desk's own fill log, when it happens to carry the name."""
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select symbol, min(when_ts) from fills where side='BUY' group by symbol").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {s: datetime.fromtimestamp(t).date() for s, t in rows if t}


# ---------- the rules ----------

def plan_one(h: dict, bars: list[dict], entered: date | None, tick: D) -> dict:
    entry, ltp = D(str(h["average_price"])), D(str(h["last_price"]))
    closes = [D(str(b["close"])) for b in bars]
    adr = adr_pct(bars)
    widest = min(adr, MAX_STOP_PCT)
    trail_n = 10 if adr >= FAST_TRAIL_MIN_ADR else 20

    row = {"symbol": h["symbol"], "qty": int(h["quantity"]), "entry": entry, "ltp": ltp,
           "adr_pct": adr, "widest_pct": widest, "trail_n": trail_n,
           "trail": sma(closes, trail_n), "bars_since_entry": None,
           "stop": None, "r_now": None, "tick": tick, "note": ""}

    if entered is None:
        row["note"] = "NO ENTRY DATE -- pass --tradebook or --entered {}=YYYY-MM-DD".format(
            h["symbol"])
        return row

    day = next((b for b in bars if b["date"].date() == entered), None)
    if day is None:
        row["note"] = f"no daily bar on {entered} -- a holiday, or the wrong date"
        return row

    lod = D(str(day["low"]))
    row["bars_since_entry"] = sum(1 for b in bars if b["date"].date() > entered)
    row["stop_pct"] = (entry - lod) / entry * 100 if entry else D(0)
    r = entry - lod
    row["r_inr"] = r * row["qty"]
    row["r_now"] = (ltp - entry) / r if r > 0 else None

    stop = lod
    if row["r_now"] is not None and row["r_now"] >= 1:      # 6.4 rule 5, and 6.5
        stop = max(stop, entry)
    row["stop"] = snap(stop, tick)

    if r <= 0:
        row["note"] = "entry at or below the day's low -- check the date, a stop >= entry is not a position"
    elif row["stop_pct"] > widest:
        row["note"] = (f"STOP_TOO_WIDE {row['stop_pct']:.2f}% vs one ADR {widest:.2f}% -- "
                       f"04 §6.1 would not have taken this size; cut qty until "
                       f"(entry-stop) x qty is inside 0.5% of the sleeve")
    return row


def verdict(row: dict) -> str:
    """04 §6.4, in its own precedence order. The trail leg needs a CLOSE, not a tick."""
    if row["stop"] is None:
        return "UNKNOWN -- no entry date"
    if row["ltp"] <= row["stop"]:
        return "STOPPED_OUT / HARD_STOP_HIT -- out now; the GTT should already have fired"
    if (row["trail"] is not None and (row["bars_since_entry"] or 0) > 0
            and row["ltp"] < row["trail"]):
        return f"WATCH TRAIL -- under MA{row['trail_n']}; if it CLOSES there, sell all at tomorrow's open"
    if (PARTIAL_FROM <= (row["bars_since_entry"] or 0) <= PARTIAL_TO
            and row["ltp"] > row["entry"]):
        return f"SELL_PARTIAL -- {row['qty'] // 3} shares, then stop to breakeven"
    if row["r_now"] is not None and row["r_now"] >= 1:
        return "RAISE_STOP / BREAKEVEN_AT_R"
    return "HOLD / NOTHING_TO_DO"


def gtt_call(row: dict) -> str:
    trig = row["stop"]
    lim = snap(trig * D(str(C.SWING_GTT_LIMIT_FRACTION)), row["tick"])
    s = row["symbol"]
    return (f'place_gtt_order(trigger_type="single", tradingsymbol="{s}", exchange="NSE",\n'
            f'    trigger_values=[{trig}], last_price={row["ltp"]},\n'
            f'    orders=[dict(exchange="NSE", tradingsymbol="{s}", transaction_type="SELL",\n'
            f'                 quantity={row["qty"]}, order_type="LIMIT", '
            f'product="CNC", price={lim})])')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="limit to these; default is every holding")
    ap.add_argument("--tradebook", type=Path, help="Zerodha Console tradebook CSV")
    ap.add_argument("--entered", action="append", default=[], metavar="SYM=YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=200, help="daily history to pull [200]")
    a = ap.parse_args(argv)

    entered: dict[str, date] = from_desk_fills(Path("data/portfolio.db"))
    if a.tradebook:
        entered.update(from_tradebook(a.tradebook))
    for pair in a.entered:
        sym, _, d = pair.partition("=")
        entered[sym.strip().upper()] = datetime.strptime(d.strip(), "%Y-%m-%d").date()

    k = Kite()
    if not k.is_authed():
        print("no Kite session -- run the desk's login first", file=sys.stderr)
        return 2

    want = {s.upper() for s in a.symbols}
    holds = [h for h in k.holdings()
             if (not want or h["symbol"] in want)
             and not h["symbol"].upper().startswith(BLOCKED)]

    resting = {g.get("condition", {}).get("tradingsymbol"): g
               for g in k.get_gtts() if g.get("status") == "active"}

    tokens = {i["tradingsymbol"]: i["instrument_token"]
              for i in k.instruments("NSE") if i.get("segment") == "NSE"}

    start, end = date.today() - timedelta(days=a.days), date.today()
    print(f"{'symbol':<13}{'qty':>7}{'entry':>10}{'ltp':>10}{'stop':>10}"
          f"{'ADR%':>7}{f'MA':>9}{'bars':>6}  {'GTT':<6} verdict")
    print("-" * 124)

    calls: list[str] = []
    for h in sorted(holds, key=lambda h: h["symbol"]):
        tok = tokens.get(h["symbol"])
        if tok is None:
            print(f"{h['symbol']:<13}  ** not in the NSE instrument dump, skipped")
            continue
        bars = k.historical(tok, start, end, "day")
        if not bars:
            print(f"{h['symbol']:<13}  ** no daily bars returned, skipped")
            continue
        tick = D(str(k.tick_size(h["symbol"], "NSE") or DEFAULT_TICK))
        row = plan_one(h, bars, entered.get(h["symbol"]), tick)
        armed = "yes" if row["symbol"] in resting else "NAKED"
        t = f"{row['trail']:.2f}" if row["trail"] is not None else "-"
        s = str(row["stop"]) if row["stop"] is not None else "?"
        print(f"{row['symbol']:<13}{row['qty']:>7}{row['entry']:>10.2f}{row['ltp']:>10.2f}"
              f"{s:>10}{row['adr_pct']:>7.2f}{t:>9}{str(row['bars_since_entry']):>6}"
              f"  {armed:<6} {verdict(row)}")
        if row["note"]:
            print(f"{'':<13}  ** {row['note']}")
        if row["stop"] is not None:
            calls.append(gtt_call(row))

    print("\n\n=== the calls -- nothing below has been sent ===\n")
    for c in calls:
        print(c, "\n")
    print("Raising a stop is delete_gtt_order(trigger_id=...) THEN place_gtt_order(...),")
    print("in that order, matching the desk's own sequence in swing_execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
