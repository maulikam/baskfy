"""Portfolio performance view model. Reads stored snapshots, benchmarks and trades only.

HONESTY ABOUT SHORT HISTORY
A Sharpe ratio computed from five days is arithmetic, not information. Every metric here
carries the observation count it was computed from and a `meaningful` flag against a
documented minimum, and the page refuses to present an unsupported number as a result.
That matters more than the numbers themselves: the whole point of a performance page is
to be trusted, and a confident figure drawn from a fortnight destroys that.

The index is the TWR chain stored by db.rechain_index(), so contributions and withdrawals
never register as performance.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd

from .. import config as C
from . import benchmark as B
from . import db as DB
from . import metrics as M

# Below these counts a metric is arithmetic rather than evidence.
MIN_DAYS = {"return": 5, "volatility": 21, "ratio": 63, "drawdown": 21,
            "monthly": 2, "relative": 21}
BENCHMARKS = ("NIFTY 500", "NIFTY200MOMENTM50", "NIFTY200MOMENTM30")


def _flag(n: int, kind: str) -> dict:
    need = MIN_DAYS[kind]
    return {"n": n, "need": need, "meaningful": n >= need}


def _series(conn) -> tuple[pd.Series, pd.Series, pd.Series]:
    rows = DB.snapshot_series(conn)
    if not rows:
        empty = pd.Series(dtype=float)
        return empty, empty, empty
    idx = pd.to_datetime([r["date"] for r in rows])
    return (pd.Series([r["index_value"] for r in rows], index=idx, dtype=float),
            pd.Series([r["nav"] for r in rows], index=idx, dtype=float),
            pd.Series([r["cash"] for r in rows], index=idx, dtype=float))


def _cashflows(conn) -> pd.Series:
    rows = conn.execute("SELECT date, SUM(amount) amt FROM cashflows GROUP BY date"
                        ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series([float(r["amt"]) for r in rows],
                     index=pd.to_datetime([r["date"] for r in rows]))


def _polyline(series: pd.Series, width: float, height: float, *,
              lo: float | None = None, hi: float | None = None) -> str:
    """Points for an inline SVG polyline. Server-rendered: no script, no CDN."""
    if len(series) < 2:
        return ""
    lo = series.min() if lo is None else lo
    hi = series.max() if hi is None else hi
    span = (hi - lo) or 1.0
    n = len(series) - 1
    return " ".join(
        f"{i / n * width:.2f},{height - (v - lo) / span * height:.2f}"
        for i, v in enumerate(series.to_numpy()))


def build(conn) -> dict:
    index, nav, cash = _series(conn)
    if index.empty:
        return {"available": False,
                "message": "No EOD snapshots recorded yet.",
                "how_to": "python -m app.analytics.snapshot"}

    flows = _cashflows(conn)
    returns = M.daily_returns(nav, flows if len(flows) else None)
    n = len(index)

    # --- benchmarks, each labelled with the series actually used --------------------
    curves, used, notes = {}, {}, []
    for name in BENCHMARKS:
        s = B.benchmark_series(conn, name)
        if s.empty:
            continue
        s = s[s.index >= index.index[0]]
        if len(s) < 2:
            notes.append(f"{name}: no overlap with the portfolio's life")
            continue
        curves[name] = s
        used[name] = s.attrs.get("series_type", "UNKNOWN")

    combined = M.equity_curve_vs_benchmark(index, curves, series_types=used)
    curve = combined["curve"]

    # --- drawdown --------------------------------------------------------------------
    dd = M.max_drawdown(index)
    underwater = M.drawdown_series(index) * 100.0

    # --- money-weighted return, if flows exist ---------------------------------------
    xirr = None
    if len(flows):
        amounts = list(flows.to_numpy()) + [float(nav.iloc[-1])]
        dates = list(flows.index) + [nav.index[-1]]
        value = M.xirr(amounts, dates)
        xirr = None if value != value else value        # nan check

    # --- trades ------------------------------------------------------------------------
    trades = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT symbol, entry_ts, exit_ts, qty, entry_price, exit_price, entry_score,"
        " exit_reason, pnl, costs FROM trades")])
    closed = trades[trades["exit_ts"].notna()] if len(trades) else pd.DataFrame()
    trade_stats = None
    if len(closed):
        trade_stats = {
            "closed": int(len(closed)),
            "win_rate": M.win_rate(closed) * 100.0,
            "avg_win": M.avg_win(closed), "avg_loss": M.avg_loss(closed),
            "payoff": M.payoff_ratio(closed), "profit_factor": M.profit_factor(closed),
            "expectancy": M.expectancy(closed),
            "avg_hold_days": M.avg_holding_period(closed),
            "by_symbol": M.per_symbol_contribution(closed).head(12).reset_index()
                          .to_dict("records"),
            "by_exit": M.hit_rate_by_exit_reason(closed).reset_index().to_dict("records"),
        }

    monthly = M.monthly_return_matrix(index)
    rel = {}
    if curves:
        primary = next(iter(curves))
        br = curves[primary].pct_change().dropna()
        rel = {"name": primary, "series_type": used.get(primary, "UNKNOWN"),
               "beta": M.beta(returns, br), "alpha": M.alpha_jensen(returns, br),
               "tracking_error": M.tracking_error(returns, br),
               "information_ratio": M.information_ratio(returns, br),
               "up_capture": M.up_capture(M.monthly_returns(index),
                                          M.monthly_returns(curves[primary])),
               "down_capture": M.down_capture(M.monthly_returns(index),
                                              M.monthly_returns(curves[primary]))}

    # --- current book ------------------------------------------------------------------
    latest = DB.get_snapshot(conn, index.index[-1].date().isoformat())
    positions = json.loads(latest["holdings_json"] or "{}").get("positions", [])
    tradeable = [p for p in positions if not p.get("excluded")]

    return {
        "available": True,
        "inception": index.index[0].date().isoformat(),
        "as_of": index.index[-1].date().isoformat(),
        "observations": n,
        "index_value": float(index.iloc[-1]),
        "nav": float(nav.iloc[-1]),
        "cash": float(cash.iloc[-1]),
        "cash_pct": float(cash.iloc[-1] / nav.iloc[-1] * 100) if nav.iloc[-1] else None,
        "positions": len(tradeable),

        # polarity drives BOTH the colour and the sign prefix:
        #   up   higher is better, signed, green when positive
        #   down lower is better, never green
        #   loss a magnitude that is already bad, never green, never a plus sign
        "metrics": [
            {"label": "Total return", "value": M.absolute(index) * 100, "unit": "%",
             "polarity": "up", "flag": _flag(n, "return"),
             "note": "Time-weighted, so deposits and withdrawals do not count as gains."},
            {"label": "CAGR", "value": M.cagr(index) * 100, "unit": "%",
             "polarity": "up", "flag": _flag(n, "ratio"),
             "note": "Annualising a few weeks extrapolates noise."},
            {"label": "XIRR", "value": xirr * 100 if xirr is not None else None, "unit": "%",
             "polarity": "up", "flag": _flag(n, "ratio"),
             "note": ("Money-weighted, from recorded cashflows."
                      if xirr is not None else "No cashflows recorded.")},
            {"label": "Volatility", "value": M.ann_volatility(returns) * 100, "unit": "%",
             "polarity": "down", "flag": _flag(len(returns), "volatility"),
             "note": "Annualised. Higher is not better."},
            {"label": "Sharpe", "value": M.sharpe(returns), "unit": "",
             "polarity": "up", "flag": _flag(len(returns), "ratio"),
             "note": "Excess return per unit of risk."},
            {"label": "Sortino", "value": M.sortino(returns), "unit": "",
             "polarity": "up", "flag": _flag(len(returns), "ratio"),
             "note": "Downside risk only."},
            {"label": "Calmar", "value": M.calmar(index), "unit": "",
             "polarity": "up", "flag": _flag(n, "drawdown"),
             "note": "CAGR over the deepest fall."},
            {"label": "Max drawdown", "value": dd["depth"] * 100, "unit": "%",
             "polarity": "loss", "flag": _flag(n, "drawdown"),
             "note": f"{dd['duration_days']}d"
                     + ("" if dd["recovered"] else ", still under water")},
            {"label": "VaR 95%", "value": M.var_historical(returns) * 100, "unit": "%",
             "polarity": "loss", "flag": _flag(len(returns), "volatility"),
             "note": "Worst daily loss at 95% confidence. A loss, not a gain."},
        ],

        "drawdown": {"depth_pct": dd["depth"] * 100,
                     "peak": dd["peak_date"].date().isoformat() if dd["peak_date"] else None,
                     "trough": dd["trough_date"].date().isoformat() if dd["trough_date"] else None,
                     "recovered": dd["recovered"],
                     "recovery": dd["recovery_date"].date().isoformat()
                                 if dd["recovery_date"] else None,
                     "duration_days": dd["duration_days"],
                     "current_pct": dd["current_dd"] * 100},

        "chart": {
            "series": [{"name": col,
                        "points": _polyline(curve[col].dropna(), 1000, 260,
                                            lo=float(curve.min().min()),
                                            hi=float(curve.max().max())),
                        "last": float(curve[col].dropna().iloc[-1]),
                        "type": ("NAV" if col == "portfolio" else used.get(col, "UNKNOWN"))}
                       for col in curve.columns if curve[col].notna().any()],
            "lo": float(curve.min().min()) if len(curve) else 100.0,
            "hi": float(curve.max().max()) if len(curve) else 100.0,
            "start": curve.index[0].date().isoformat() if len(curve) else None,
            "end": curve.index[-1].date().isoformat() if len(curve) else None,
        },
        "underwater": {"points": _polyline(underwater, 1000, 90,
                                           lo=float(min(underwater.min(), -0.01)), hi=0.0),
                       "worst": float(underwater.min())},
        "series_used": combined["series_used"],
        "dropped_benchmarks": combined.get("dropped", []),
        "benchmark_notes": notes,
        "relative": rel,
        "relative_flag": _flag(len(returns), "relative"),
        "monthly": ({"years": list(monthly.index),
                     "rows": [[None if pd.isna(v) else round(float(v), 2)
                               for v in monthly.loc[y]] for y in monthly.index]}
                    if len(monthly) else None),
        "monthly_flag": _flag(len(M.monthly_returns(index)), "monthly"),
        "trades": trade_stats,
        "has_cashflows": bool(len(flows)),
    }
