"""The holdings table, and what can honestly be said about it.

EVERY FIGURE HERE COMES FROM SOMEWHERE REAL — Kite's holdings payload, the fills table,
the uploaded scan, or the live GTT book. Where a field cannot be sourced it is absent
rather than approximated, and `gaps` names it, because on a page about money an invented
number is worse than a missing one.

What the account can answer:
  cost, price, invested, value, unrealised P&L, day change and day P&L  from Kite
  weight, concentration, top-five                                       computed here
  market-cap band, beta, distance from the 52-week high                 from the scan
  first bought, holding period, long/short-term for tax                 from fills
  protected / unprotected                                               from the GTT book

What it cannot, and why:
  sector          the scan carries no sector column, and guessing one from a symbol is
                  how a concentration warning becomes fiction
  dividends       no dividend feed is wired; kc.holdings() does not carry one
  XIRR            needs recorded cashflows and the table is empty
  factor exposure, correlations, international split — none of the inputs exist
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping, Sequence

from .. import config as C

# Kite reports market cap in Rs crore. The bands are the ones the strategy already uses
# for its category weights, so the page and the planner describe the same universe.
LARGE_CAP_CR = 100_000.0
MID_CAP_CR = 25_000.0
LONG_TERM_DAYS = 365          # equity LTCG threshold


def _band(marketcap_cr: float | None) -> str | None:
    if marketcap_cr is None:
        return None
    if marketcap_cr >= LARGE_CAP_CR:
        return "largecap"
    return "midcap" if marketcap_cr >= MID_CAP_CR else "smallcap"


def _lots(fills: Sequence[Mapping], symbol: str) -> dict:
    """First purchase and holding period, from the captured fill history.

    FIFO is not reconstructed here — tradebook.py already owns tax lots. What this needs
    is only the age of the oldest open buy, which decides short against long term.
    """
    buys = sorted((f for f in fills
                   if f["symbol"] == symbol and str(f["side"]).upper() == "BUY"),
                  key=lambda f: float(f["when_ts"] or 0))
    if not buys:
        return {}
    first = dt.datetime.fromtimestamp(float(buys[0]["when_ts"]))
    held = (dt.datetime.now() - first).days
    return {"first_bought": first.date().isoformat(), "held_days": held,
            "term": "long" if held >= LONG_TERM_DAYS else "short",
            "buy_fills": len(buys)}


def rows(holdings: Iterable[Mapping], *, raw: Sequence[Mapping] = (),
         scan: Mapping[str, Mapping] | None = None,
         fills: Sequence[Mapping] = (),
         protected: Mapping[str, str] | None = None) -> list[dict]:
    """One row per holding, with everything that can be sourced for it."""
    scan = scan or {}
    protected = protected or {}
    by_symbol = {r.get("tradingsymbol"): r for r in raw}

    out = []
    for h in holdings:
        sym = h["symbol"]
        qty = int(h["quantity"] or 0)
        avg = float(h.get("average_price") or 0.0)
        ltp = float(h.get("last_price") or 0.0)
        k = by_symbol.get(sym, {})
        s = scan.get(sym, {})

        invested = qty * avg
        value = qty * ltp
        pnl = value - invested
        # Kite's own day_change is per share and against the previous close, which is the
        # figure the app shows; deriving it from a stored close would drift on a holiday.
        day_pct = k.get("day_change_percentage")
        day_share = k.get("day_change")
        mcap = s.get("marketcap")

        row = {
            "symbol": sym,
            "isin": k.get("isin"),
            "exchange": h.get("exchange") or k.get("exchange"),
            "quantity": qty,
            "pledged_qty": int(h.get("pledged_qty") or 0),
            "t1_quantity": int(k.get("t1_quantity") or 0),
            "avg_price": round(avg, 2),
            "last_price": round(ltp, 2),
            "invested": round(invested),
            "value": round(value),
            "pnl": round(pnl),
            "pnl_pct": round(pnl / invested * 100, 2) if invested else None,
            "day_change_pct": None if day_pct is None else round(float(day_pct), 2),
            "day_pnl": None if day_share is None else round(float(day_share) * qty),
            "marketcap_cr": None if mcap is None else round(float(mcap)),
            "cap_band": _band(None if mcap is None else float(mcap)),
            "beta": s.get("beta"),
            "from_high_pct": s.get("away_from_high_one_year"),
            "stop": protected.get(sym, "unknown"),
            **_lots(fills, sym),
        }
        out.append(row)

    total = sum(r["value"] for r in out) or 1.0
    for r in out:
        r["weight_pct"] = round(r["value"] / total * 100, 2)
    return sorted(out, key=lambda r: -r["value"])


def summary(rows_: Sequence[Mapping], *, cash: float = 0.0) -> dict:
    """Portfolio totals. Percentages are of the equity book unless named otherwise."""
    invested = sum(r["invested"] for r in rows_)
    value = sum(r["value"] for r in rows_)
    day = [r["day_pnl"] for r in rows_ if r["day_pnl"] is not None]
    nav = value + cash
    top5 = sum(r["weight_pct"] for r in sorted(rows_, key=lambda r: -r["weight_pct"])[:5])
    betas = [(r["beta"], r["weight_pct"]) for r in rows_ if r.get("beta") is not None]
    wb = sum(b * w for b, w in betas) / sum(w for _b, w in betas) if betas else None
    return {
        "positions": len(rows_),
        "invested": round(invested),
        "value": round(value),
        "pnl": round(value - invested),
        "pnl_pct": round((value - invested) / invested * 100, 2) if invested else None,
        "day_pnl": round(sum(day)) if day else None,
        "day_pnl_pct": (round(sum(day) / (value - sum(day)) * 100, 2)
                        if day and value - sum(day) else None),
        "cash": round(cash),
        "nav": round(nav),
        "cash_pct": round(cash / nav * 100, 2) if nav else None,
        "top5_weight": round(top5, 1),
        "largest": (max(rows_, key=lambda r: r["weight_pct"])["symbol"] if rows_ else None),
        "largest_weight": (round(max(r["weight_pct"] for r in rows_), 2) if rows_ else None),
        "weighted_beta": None if wb is None else round(wb, 2),
        "pledged_positions": sum(1 for r in rows_ if r["pledged_qty"] > 0),
    }


def allocation(rows_: Sequence[Mapping], *, cash: float = 0.0) -> dict:
    """By market-cap band, plus the cash sleeve. Unbanded names are reported as such
    rather than folded into a band they were never classified into."""
    buckets: dict[str, float] = {}
    for r in rows_:
        buckets[r["cap_band"] or "unclassified"] = (
            buckets.get(r["cap_band"] or "unclassified", 0.0) + r["value"])
    total = sum(buckets.values()) + cash or 1.0
    out = [{"band": b, "value": round(v), "pct": round(v / total * 100, 1)}
           for b, v in sorted(buckets.items(), key=lambda kv: -kv[1])]
    if cash:
        out.append({"band": "cash", "value": round(cash), "pct": round(cash / total * 100, 1)})
    return {"rows": out, "total": round(total)}


def warnings(rows_: Sequence[Mapping], summary_: Mapping) -> list[dict]:
    """Only conditions the data can actually establish."""
    from ..core.guards import UntouchableInstrumentError, assert_tradeable

    def _tradeable(sym: str) -> bool:
        try:
            assert_tradeable(sym)
            return True
        except UntouchableInstrumentError:
            return False

    out = []
    cap = float(getattr(C, "MAX_SINGLE_WEIGHT", 15.0))
    for r in rows_:
        # An untouchable instrument is held ON PURPOSE and outside the strategy's sizing,
        # so measuring it against a position cap the planner will never apply to it is
        # noise — and noise in a warning list is what makes the real ones get ignored.
        if not _tradeable(r["symbol"]):
            continue
        if r["weight_pct"] > cap:
            out.append({"level": "warn", "symbol": r["symbol"],
                        "text": f"{r['weight_pct']:.1f}% of the book, over the "
                                f"{cap:.0f}% single-position cap"})
    unprotected = [r["symbol"] for r in rows_ if r["stop"] in ("missing", "partial")]
    if unprotected:
        out.append({"level": "bad", "symbol": None,
                    "text": f"{len(unprotected)} position(s) without full stop cover: "
                            + ", ".join(unprotected[:6])})
    deep = [r for r in rows_ if (r["pnl_pct"] or 0) <= -20 and _tradeable(r["symbol"])]
    for r in deep:
        out.append({"level": "warn", "symbol": r["symbol"],
                    "text": f"down {r['pnl_pct']:.1f}% on cost"})
    if summary_.get("top5_weight", 0) > 60:
        out.append({"level": "warn", "symbol": None,
                    "text": f"top five holdings are {summary_['top5_weight']:.0f}% of the "
                            "book"})
    return out


GAPS = [
    ("Sector allocation", "the scan carries no sector column; inferring one from a symbol "
                          "would make the concentration warning fiction"),
    ("Dividends and yield", "no dividend feed is wired, and kc.holdings() does not carry "
                            "one"),
    ("XIRR", "needs recorded cashflows; the cashflows table is empty"),
    ("Factor exposure and correlations", "none of the inputs exist in this account"),
]
