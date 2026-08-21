"""The index board page — what /indices renders and what its drill-down returns.

This is the read-only half of the desk pointed outward: instead of "what do I hold", it
answers "where is the market strong, and which names inside that strength does the screen
already like". Three sources meet here and each answers a different question:

  nse_indices.board()   the live tape for ~140 indices, and the advance/decline NSE
                        publishes per index — that is real constituent breadth, counted by
                        the exchange, not something inferred here.
  nse_indices.constituents()  who is actually in an index.
  the latest scan CSV, run through scoring.score()  the desk's OWN view of a name: its
                        Momentum Quality Score, its trend and momentum components, why it
                        was rejected if it was.

The join is deliberately lopsided. A scan holds a few hundred screened names, not the
whole market, so most constituents of most indices will have no score, and the page says
so rather than implying a blank means zero. The scan is what makes this the Momentum Desk's
index board rather than a generic market screen: the interesting cell is not the day's
move, it is which names in a hot sector the screen has already surfaced.

Live per-stock prices need a Kite session, and the token expires daily with no refresh, so
every quote field degrades to None and the panel keeps working on NSE data alone.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Any

import pandas as pd

from . import nse_indices

log = logging.getLogger("index_view")

SCAN_GLOB = "data/uploads/scan_*.csv"

_scan_lock = threading.Lock()
_scan_cache: dict[str, Any] = {"path": None, "mtime": None, "by_symbol": {}, "as_of": None}


# --- the screen -------------------------------------------------------------------------

def latest_scan_path() -> str | None:
    paths = sorted(glob.glob(SCAN_GLOB), key=os.path.getmtime, reverse=True)
    return paths[0] if paths else None


def scan_scores() -> dict:
    """The newest scan CSV, scored, keyed by symbol. Recomputed only when the file changes.

    Scoring the frame costs real work and the file changes once a week, so the result is
    memoised on (path, mtime). A failure here must not take the board down — the index
    board is useful with no scan at all, so this degrades to an empty map.
    """
    path = latest_scan_path()
    if not path:
        return {"by_symbol": {}, "as_of": None, "path": None, "count": 0}

    mtime = os.path.getmtime(path)
    with _scan_lock:
        if _scan_cache["path"] == path and _scan_cache["mtime"] == mtime:
            return {"by_symbol": _scan_cache["by_symbol"], "as_of": _scan_cache["as_of"],
                    "path": path, "count": len(_scan_cache["by_symbol"])}

        by_symbol: dict[str, dict] = {}
        as_of = None
        try:
            from ..scoring import load_scan, score
            scored = score(load_scan(path))
            if "date" in scored.columns and len(scored):
                as_of = str(scored["date"].max())[:10]
            for r in scored.to_dict("records"):
                sym = str(r.get("symbol") or "").strip()
                if not sym:
                    continue
                by_symbol[sym] = {
                    "score": _f(r.get("SCORE")),
                    "rank": _i(r.get("rank")),
                    "reject": (str(r.get("reject")) if r.get("reject") not in (None, "", float("nan")) else None),
                    "close": _f(r.get("close")),
                    "ma_50": _f(r.get("ma_50")),
                    "ma_200": _f(r.get("ma_200")),
                    "rsi": _f(r.get("rsi_one_year")),
                    "ret_1y": _f(r.get("absolute_return_one_year")),
                    "ret_3m": _f(r.get("absolute_return_three_months")),
                    "ret_1m": _f(r.get("absolute_return_one_month")),
                    "away_ath": _f(r.get("away_from_high_all_time")),
                    "vol_1y": _f(r.get("volatility_one_year")),
                    "beta": _f(r.get("beta")),
                    "trend": _f(r.get("A_trend")),
                    "momentum": _f(r.get("B_momentum")),
                }
        except Exception as e:                                   # noqa: BLE001
            log.warning("scan scoring failed for %s: %s", path, e)
            by_symbol = {}

        _scan_cache.update({"path": path, "mtime": mtime, "by_symbol": by_symbol, "as_of": as_of})
        return {"by_symbol": by_symbol, "as_of": as_of, "path": path, "count": len(by_symbol)}


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


# --- board ------------------------------------------------------------------------------

# Which board rows are worth offering a drill-down on. A leveraged, inverse or G-Sec index
# has no constituent list in the archive, and India VIX is not an index of anything — so
# the row renders, but without a control that can only ever fail.
NO_CONSTITUENTS = ("VIX", "G-SEC", "GS ", "BHARATBOND", "BHARAT BOND",
                   "LEVERAGE", "INVERSE", "DIVIDEND POINTS", "USD")


def _drillable(name: str) -> bool:
    up = (name or "").upper()
    return not any(t in up for t in NO_CONSTITUENTS)


def family(name: str) -> str:
    """Broad / Sector / Factor / Theme / Derived, for filtering the board.

    Order matters: "NIFTY INDIA DEFENCE EQUAL WEIGHT" is a theme that happens to be equal
    weighted, not a factor index, so the strong theme words are tested before the factor
    words.
    """
    up = (name or "").upper()
    if any(t in up for t in ("VIX", "G-SEC", "GS ", "BHARATBOND", "BHARAT BOND",
                             "LEVERAGE", "INVERSE", "DIVIDEND POINTS", "USD")):
        return "Derived"
    if up in BROAD:
        return "Broad"
    if any(t in up for t in ("DEFENCE", "RAILWAY", "TOURISM", "INTERNET", "DIGITAL",
                             "AUTOMOTIVE", "CORPORATE GROUP", "MAATR", "CONGLOMERATE",
                             "ESG", "SHARIAH", "AHIMSA")):
        return "Theme"
    if any(t in up for t in ("MOMENTUM", "QUALITY", "VALUE", "ALPHA", "VOLATILITY",
                             "HIGH BETA", "MULTIFACTOR", "MQVLV", "EQUAL WEIGHT",
                             "EQUAL-CAP", "DIVIDEND OPPORTUNIT")):
        return "Factor"
    if any(t in up for t in ("BANK", "NBFC", "INSURANCE", "FINANCIAL SERVICES",
                             "CAPITAL MARKETS", "HOUSING FINANCE", "IT", "PHARMA",
                             "HEALTHCARE", "HOSPITALS", "AUTO", "FMCG", "METAL", "REALTY",
                             "REITS", "MEDIA", "ENERGY", "OIL & GAS", "POWER",
                             "TELECOM", "CEMENT", "CHEMICALS", "CAPITAL GOODS",
                             "CONSUMER DURABLES", "RETAIL", "CONSTRUCTION",
                             "COMMODITIES", "SUGAR")):
        return "Sector"
    return "Theme"


BROAD = {
    "NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500",
    "NIFTY MIDCAP 50", "NIFTY MIDCAP 100", "NIFTY MIDCAP 150", "NIFTY MIDCAP SELECT",
    "NIFTY MIDCAP LIQUID 15", "NIFTY100 LIQUID 15", "NIFTY SMALLCAP 50",
    "NIFTY SMALLCAP 100", "NIFTY SMALLCAP 250", "NIFTY SMALLCAP 500",
    "NIFTY MICROCAP 250", "NIFTY TOTAL MARKET", "NIFTY LARGEMIDCAP 250",
    "NIFTY MIDSMALLCAP 400", "NIFTY MIDSMALLCAP400 50:50", "NIFTY SME EMERGE",
    "NIFTY IPO", "NIFTY500 MULTICAP 50:25:25", "NIFTY INDIA FPI 150",
    "NIFTY500 LARGEMIDSMALL EQUAL-CAP WEIGHTED",
}


def build(selected: str = "") -> dict:
    """Everything /indices needs for its first paint."""
    b = nse_indices.board()
    scan = scan_scores()

    rows = []
    for r in b["rows"]:
        adv, dec = r.get("advances"), r.get("declines")
        total = sum(x for x in (adv, dec, r.get("unchanged")) if x is not None) or None
        rows.append({
            **r,
            "family": family(r["name"]),
            "drillable": _drillable(r["name"]),
            "members": total,
            # The exchange's own count of how many constituents rose. This is the one
            # breadth number on the page that is measured rather than derived.
            "adv_pct": (round(adv / total * 100, 1) if adv is not None and total else None),
            "range_pos": _range_pos(r),
            "year_pos": _year_pos(r),
        })

    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0)))
    chosen = _pick(rows, selected)
    return {
        "rows": rows,
        "selected": chosen,
        "fetched_at": b.get("fetched_at"),
        "stale": b.get("stale"),
        "error": b.get("error"),
        "scan": {"as_of": scan["as_of"], "count": scan["count"],
                 "path": os.path.basename(scan["path"]) if scan["path"] else None},
        "families": ["All", "Broad", "Sector", "Factor", "Theme", "Derived"],
        "advancing": sum(1 for r in rows if (r["pct"] or 0) > 0),
        "declining": sum(1 for r in rows if (r["pct"] or 0) < 0),
        "unchanged_count": sum(1 for r in rows if r["pct"] == 0),
    }


def _pick(rows: list[dict], selected: str) -> dict | None:
    if not rows:
        return None
    if selected:
        want = nse_indices.slug(selected)
        for r in rows:
            if nse_indices.slug(r["name"]) == want:
                return r
    for r in rows:
        if r["name"].upper() == "NIFTY 50":
            return r
    return rows[0]


def _range_pos(r: dict) -> float | None:
    """Where the close sits inside the day's range, 0-100. None when NSE published no OHLC."""
    lo, hi, last = r.get("low"), r.get("high"), r.get("last")
    if lo is None or hi is None or last is None or hi <= lo:
        return None
    return round(max(0.0, min(1.0, (last - lo) / (hi - lo))) * 100, 1)


def _year_pos(r: dict) -> float | None:
    lo, hi, last = r.get("year_low"), r.get("year_high"), r.get("last")
    if lo is None or hi is None or last is None or hi <= lo:
        return None
    return round(max(0.0, min(1.0, (last - lo) / (hi - lo))) * 100, 1)


# --- drill-down -------------------------------------------------------------------------

def drilldown(name: str, kite=None) -> dict:
    """One index's constituents, each with live price and the desk's own score.

    `kite` is optional and may be an unauthenticated client — quotes are best-effort, and
    the panel is expected to render without them.
    """
    c = nse_indices.constituents(name)
    scan = scan_scores()["by_symbol"]
    rows = c["rows"]

    quotes: dict[str, dict] = {}
    quote_error = None
    if rows and kite is not None:
        try:
            quotes = kite.quotes([r["symbol"] for r in rows])
        except Exception as e:                                   # noqa: BLE001
            quote_error = str(e)
            log.info("drilldown quotes failed for %s: %s", name, e)

    out = []
    for r in rows:
        q = quotes.get(r["symbol"]) or {}
        s = scan.get(r["symbol"])
        last, prev = q.get("last_price"), q.get("prev_close")
        pct = round((last - prev) / prev * 100, 2) if last and prev else None
        out.append({
            **r,
            "last": last,
            "change": q.get("net_change"),
            "pct": pct,
            "day_low": q.get("low"),
            "day_high": q.get("high"),
            "volume": q.get("volume"),
            "in_scan": s is not None,
            "score": (s or {}).get("score"),
            "rank": (s or {}).get("rank"),
            "reject": (s or {}).get("reject"),
            "rsi": (s or {}).get("rsi"),
            "ret_1y": (s or {}).get("ret_1y"),
            "ret_3m": (s or {}).get("ret_3m"),
            "away_ath": (s or {}).get("away_ath"),
            "above_50dma": _above(s, "ma_50"),
            "above_200dma": _above(s, "ma_200"),
        })

    # Sort by the desk's score first — the point of opening a sector is to see which of its
    # names the screen already likes, so those must not be buried alphabetically.
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["symbol"]))

    scored = [r for r in out if r["in_scan"]]
    return {
        "index": name,
        "rows": out,
        "count": len(out),
        "scored_count": len(scored),
        "fetched_at": c.get("fetched_at"),
        "stale": c.get("stale"),
        "error": c.get("error"),
        "quote_error": quote_error,
        "industries": sorted({r["industry"] for r in out if r["industry"]}),
        # Breadth over the scanned subset only, and labelled as such on the page. Stating
        # it over all constituents would be a lie: an unscored name is unknown, not below.
        "breadth": _breadth(scored),
    }


def _above(s: dict | None, key: str) -> bool | None:
    if not s or s.get("close") is None or s.get(key) is None:
        return None
    return bool(s["close"] > s[key])


def _breadth(scored: list[dict]) -> dict:
    n = len(scored)
    if not n:
        return {"n": 0}

    def pct(pred) -> float:
        return round(sum(1 for r in scored if pred(r)) * 100 / n, 1)

    return {
        "n": n,
        "above_200dma": pct(lambda r: r["above_200dma"] is True),
        "above_50dma": pct(lambda r: r["above_50dma"] is True),
        "near_ath": pct(lambda r: r["away_ath"] is not None and r["away_ath"] > -10),
        "positive_1y": pct(lambda r: (r["ret_1y"] or 0) > 0),
    }
