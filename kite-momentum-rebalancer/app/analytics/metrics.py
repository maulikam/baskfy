"""Metrics engine — pure, vectorized pandas. No I/O, no network, no broker calls.

Every function takes plain Series/DataFrames and returns plain values, so each one is
unit-testable in isolation and safe to call from a request path (phase 3 reads
precomputed values from SQLite; nothing here blocks).

CONVENTIONS (consistent with app/analytics/db.py — do not diverge):
- Cashflow sign: invest NEGATIVE, withdraw/terminal POSITIVE (the XIRR convention).
  Internally these are flipped to "flow INTO the portfolio" where that reads better.
- Daily TWR neutralises flows at END of day:  r_t = (nav_t - flow_t) / nav_{t-1} - 1
  This is the same formula db.rechain_index() uses, so the index series stored in
  SQLite and the one computed here agree exactly.
- Drawdown depth is a NEGATIVE fraction (-0.25 == a 25% fall). depth_pct is the same
  number in percent. VaR is returned as a POSITIVE loss magnitude.
- Sharpe/Sortino annualise the ARITHMETIC mean of daily excess returns (textbook form).
  CAGR-based ratios are available via calmar(), which uses geometric growth.
- Day count: ACT/365 for XIRR and CAGR; 252 trading days for volatility scaling.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DAYS_PER_YEAR = 365.0
# A "constant" return series still has std ~1e-19 from float noise. Comparing against
# exactly 0.0 lets that noise become the denominator and produce ratios like 7e16, so
# every risk-adjusted ratio guards on this tolerance instead.
EPS = 1e-12
SECTORS_FILE = "data/sectors.csv"          # same file rebalance.py reads (symbol,cluster)

# Indian capital-gains regime used by tax_drag()
STCG_RATE = 0.20                            # holding < 12 months
LTCG_RATE = 0.125                           # holding >= 12 months
LTCG_EXEMPTION = 125_000.0                  # per financial year, on LTCG only
LONG_TERM_DAYS = 365


# =====================================================================================
# helpers
# =====================================================================================
def _series(x: Any, *, sort: bool = True) -> pd.Series:
    """Coerce to a float Series with a DatetimeIndex."""
    s = x.copy() if isinstance(x, pd.Series) else pd.Series(x)
    s.index = pd.to_datetime(s.index)
    s = s.astype(float)
    return s.sort_index() if sort else s


def _flows_into(cashflows: Any, index: pd.Index) -> pd.Series:
    """Net flow INTO the portfolio per date, aligned to `index`.

    Input uses the stored convention (invest negative), so the sign is flipped here.
    """
    if cashflows is None:
        return pd.Series(0.0, index=index)
    cf = _series(cashflows)
    cf = cf.groupby(cf.index).sum()
    return (-cf).reindex(index).fillna(0.0)


def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Inner-join two series on their common dates."""
    a, b = _series(a), _series(b)
    common = a.index.intersection(b.index)
    return a.loc[common], b.loc[common]


def load_sectors(path: str = SECTORS_FILE) -> dict[str, str]:
    """symbol -> cluster, from the same CSV rebalance.py uses. Empty if absent."""
    if not os.path.exists(path):
        return {}
    s = pd.read_csv(path)
    return dict(zip(s["symbol"].astype(str), s["cluster"].astype(str)))


# =====================================================================================
# returns
# =====================================================================================
def daily_returns(navs: Any, cashflows: Any = None) -> pd.Series:
    """Daily time-weighted returns with external flows neutralised.

    Days where there was no capital at risk yesterday yield 0.0 rather than dividing
    by zero. The first observation is dropped (no prior day to measure against).
    """
    nav = _series(navs)
    flows = _flows_into(cashflows, nav.index)
    prev = nav.shift(1)
    r = (nav - flows) / prev - 1.0
    r = r.where(prev > 0, 0.0)
    return r.iloc[1:]


def twr_chained(navs: Any, cashflows: Any = None, base: float = 100.0) -> pd.Series:
    """Chained time-weighted-return index, exactly `base` on the first date.

    With no external cashflows this reduces to the simple NAV growth curve, which is
    the phase-2 acceptance criterion.
    """
    nav = _series(navs)
    if nav.empty:
        return pd.Series(dtype=float)
    r = daily_returns(nav, cashflows)
    chained = (1.0 + r).cumprod() * base
    head = pd.Series([float(base)], index=nav.index[:1])
    return pd.concat([head, chained])


def twr_total(navs: Any, cashflows: Any = None) -> float:
    """Total time-weighted return over the whole series, as a fraction."""
    idx = twr_chained(navs, cashflows)
    if len(idx) < 2:
        return 0.0
    return float(idx.iloc[-1] / idx.iloc[0] - 1.0)


def absolute(series: Any) -> float:
    """Point-to-point return of any level series (index, NAV, price)."""
    s = _series(series)
    if len(s) < 2 or s.iloc[0] == 0:
        return 0.0
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def cagr(series: Any, *, day_count: float = DAYS_PER_YEAR) -> float:
    """Compound annual growth rate of a level series (ACT/365)."""
    s = _series(series)
    if len(s) < 2 or s.iloc[0] <= 0:
        return 0.0
    years = (s.index[-1] - s.index[0]).days / day_count
    if years <= 0:
        return 0.0
    return float((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0)


def xirr(cashflows: Sequence[float], dates: Sequence[Any], *, guess: float = 0.1,
         tol: float = 1e-10, max_iter: int = 100) -> float:
    """Money-weighted return (ACT/365).

    Newton-Raphson with the analytic derivative, falling back to bisection over a
    bracketed sign change when Newton leaves the domain or fails to converge — Newton
    alone is unreliable on irregular flow patterns and can step below r = -1.

    Sign convention: invest NEGATIVE, withdraw/terminal POSITIVE. Returns nan when the
    flows are all one sign (no root exists).
    """
    amounts = np.asarray(list(cashflows), dtype=float)
    when = pd.to_datetime(pd.Index(list(dates)))
    if amounts.size != len(when) or amounts.size < 2:
        return float("nan")
    order = np.argsort(when.values)
    amounts, when = amounts[order], when[order]
    if not (np.any(amounts > 0) and np.any(amounts < 0)):
        return float("nan")

    years = np.array([(d - when[0]).days for d in when], dtype=float) / DAYS_PER_YEAR

    def npv(rate: float) -> float:
        return float(np.sum(amounts / (1.0 + rate) ** years))

    def d_npv(rate: float) -> float:
        return float(np.sum(-amounts * years / (1.0 + rate) ** (years + 1.0)))

    # --- Newton-Raphson ---
    rate = float(guess)
    for _ in range(max_iter):
        if rate <= -1.0:
            break
        f = npv(rate)
        if abs(f) < tol:
            return rate
        d = d_npv(rate)
        if d == 0 or not np.isfinite(d):
            break
        step = f / d
        nxt = rate - step
        if not np.isfinite(nxt) or nxt <= -1.0:
            break
        if abs(nxt - rate) < tol:
            return nxt
        rate = nxt
    else:
        if abs(npv(rate)) < 1e-6:
            return rate

    # --- bisection fallback: scan for a sign change, then halve ---
    lo, hi = -0.9999, 1e-8
    f_lo = npv(lo)
    grid = np.concatenate([np.linspace(-0.99, 1.0, 200), np.linspace(1.0, 100.0, 200)])
    bracket = None
    for hi in grid:
        if hi <= lo:
            continue
        f_hi = npv(hi)
        if np.isfinite(f_hi) and f_lo * f_hi < 0:
            bracket = (lo, hi)
            break
        lo, f_lo = hi, f_hi
    if bracket is None:
        return float("nan")

    lo, hi = bracket
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return float(mid)
        if npv(lo) * f_mid < 0:
            hi = mid
        else:
            lo = mid
    return float((lo + hi) / 2.0)


def rolling_returns(level: Any,
                    windows: Mapping[str, int] | None = None) -> pd.DataFrame:
    """Trailing returns over trading-day windows (1M/3M/6M/1Y by default)."""
    s = _series(level)
    w = dict(windows or {"1M": 21, "3M": 63, "6M": 126, "1Y": TRADING_DAYS})
    return pd.DataFrame({k: s.pct_change(n) for k, n in w.items()})


def monthly_returns(level: Any) -> pd.Series:
    """Calendar-month returns of a level series, indexed by month end."""
    s = _series(level)
    if s.empty:
        return pd.Series(dtype=float)
    monthly = s.resample("ME").last()
    # seed with the opening level so the first partial month is measured correctly
    first = pd.Series([s.iloc[0]], index=[s.index[0] - pd.Timedelta(days=1)])
    return pd.concat([first, monthly]).pct_change().dropna()


def monthly_return_matrix(level: Any, *, pct: bool = True) -> pd.DataFrame:
    """Year x month matrix of returns, for the heatmap in phase 3."""
    m = monthly_returns(level)
    if m.empty:
        return pd.DataFrame()
    df = pd.DataFrame({"year": m.index.year, "month": m.index.month,
                       "ret": m.to_numpy() * (100.0 if pct else 1.0)})
    out = df.pivot_table(index="year", columns="month", values="ret", aggfunc="last")
    return out.reindex(columns=range(1, 13))


# =====================================================================================
# risk
# =====================================================================================
def _ratio(num: float, den: float) -> float:
    """Risk-adjusted ratio with an honest degenerate case.

    A ~zero denominator with a real numerator is UNDEFINED (riskless outperformance),
    not zero — reporting 0.0 there would claim "no skill" on a dashboard. nan renders
    as an em dash; 0.0 would render as a confident, wrong number.
    """
    if den < EPS:
        return 0.0 if abs(num) < EPS else float("nan")
    return float(num / den)


def ann_volatility(returns: Any, periods: int = TRADING_DAYS) -> float:
    r = _series(returns).dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1) * np.sqrt(periods))


def downside_deviation(returns: Any, mar: float = 0.0,
                       periods: int = TRADING_DAYS) -> float:
    """Annualised downside deviation below a Minimum Acceptable Return.

    `mar` is an ANNUAL rate; it is de-annualised to a daily threshold. Squared
    shortfalls are averaged over ALL observations (Sortino's usual definition), not
    only the losing ones.
    """
    r = _series(returns).dropna()
    if len(r) < 2:
        return 0.0
    daily_mar = mar / periods
    short = np.minimum(r - daily_mar, 0.0)
    return float(np.sqrt((short ** 2).mean()) * np.sqrt(periods))


def max_drawdown(level: Any) -> dict:
    """Deepest peak-to-trough fall of a level series.

    Returns depth as a NEGATIVE fraction (-0.25 == fell 25%). duration_days spans peak
    to recovery, or peak to the last observation when still under water.
    recovery_date is None if the series never regained the peak.
    """
    s = _series(level).dropna()
    if s.empty:
        return {"depth": 0.0, "depth_pct": 0.0, "peak_date": None, "trough_date": None,
                "recovery_date": None, "duration_days": 0, "current_dd": 0.0,
                "recovered": False}
    peak = s.cummax()
    dd = s / peak - 1.0
    trough_date = dd.idxmin()
    depth = float(dd.loc[trough_date])

    before = s.loc[:trough_date]
    peak_value = float(peak.loc[trough_date])
    peak_date = before[before >= peak_value].index[0]

    after = s.loc[trough_date:]
    recovered = after[after >= peak_value]
    recovery_date = recovered.index[0] if len(recovered) else None
    end = recovery_date if recovery_date is not None else s.index[-1]

    return {
        "depth": depth,
        "depth_pct": depth * 100.0,
        "peak_date": peak_date,
        "trough_date": trough_date,
        "recovery_date": recovery_date,
        "duration_days": int((end - peak_date).days),
        "current_dd": float(dd.iloc[-1]),
        "recovered": recovery_date is not None,
    }


def drawdown_series(level: Any) -> pd.Series:
    """Underwater curve (fraction below running peak) for the phase-3 chart."""
    s = _series(level).dropna()
    if s.empty:
        return pd.Series(dtype=float)
    return s / s.cummax() - 1.0


def sharpe(returns: Any, rf: float = 0.0, periods: int = TRADING_DAYS) -> float:
    """Annualised Sharpe. `rf` is an annual risk-free rate."""
    r = _series(returns).dropna()
    vol = ann_volatility(r, periods)
    return _ratio(r.mean() * periods - rf, vol)


def sortino(returns: Any, mar: float = 0.0, periods: int = TRADING_DAYS) -> float:
    r = _series(returns).dropna()
    dd = downside_deviation(r, mar, periods)
    return _ratio(r.mean() * periods - mar, dd)


def calmar(level: Any) -> float:
    """CAGR / |max drawdown| — geometric, unlike Sharpe/Sortino above."""
    mdd = max_drawdown(level)["depth"]
    return _ratio(cagr(level), abs(mdd))


def beta(port_returns: Any, bench_returns: Any) -> float:
    p, b = _align(port_returns, bench_returns)
    p, b = p.dropna(), b.dropna()
    common = p.index.intersection(b.index)
    p, b = p.loc[common], b.loc[common]
    if len(p) < 2 or b.var(ddof=1) < EPS:
        return 0.0
    return float(p.cov(b) / b.var(ddof=1))


def alpha_jensen(port_returns: Any, bench_returns: Any, rf: float = 0.0,
                 periods: int = TRADING_DAYS) -> float:
    """Annualised Jensen's alpha: Rp - [Rf + beta*(Rb - Rf)]."""
    p, b = _align(port_returns, bench_returns)
    if p.empty:
        return 0.0
    bt = beta(p, b)
    ann_p, ann_b = float(p.mean() * periods), float(b.mean() * periods)
    return float(ann_p - (rf + bt * (ann_b - rf)))


def tracking_error(port_returns: Any, bench_returns: Any,
                   periods: int = TRADING_DAYS) -> float:
    p, b = _align(port_returns, bench_returns)
    diff = (p - b).dropna()
    if len(diff) < 2:
        return 0.0
    return float(diff.std(ddof=1) * np.sqrt(periods))


def information_ratio(port_returns: Any, bench_returns: Any,
                      periods: int = TRADING_DAYS) -> float:
    p, b = _align(port_returns, bench_returns)
    te = tracking_error(p, b, periods)
    return _ratio((p.mean() - b.mean()) * periods, te)


def var_parametric(returns: Any, confidence: float = 0.95, z: float | None = None) -> float:
    """Gaussian VaR as a POSITIVE daily loss fraction. z defaults to 1.645 at 95%."""
    r = _series(returns).dropna()
    if len(r) < 2:
        return 0.0
    zz = 1.645 if z is None else float(z)
    return float(-(r.mean() - zz * r.std(ddof=1)))


def var_historical(returns: Any, confidence: float = 0.95) -> float:
    """Empirical VaR as a POSITIVE daily loss fraction."""
    r = _series(returns).dropna()
    if r.empty:
        return 0.0
    return float(-r.quantile(1.0 - confidence))


# =====================================================================================
# trades
# =====================================================================================
def _closed(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or len(trades) == 0:
        return pd.DataFrame(columns=["symbol", "pnl", "costs", "entry_ts", "exit_ts"])
    df = trades.copy()
    if "exit_ts" in df.columns:
        df = df[df["exit_ts"].notna()]
    return df


def win_rate(trades: pd.DataFrame) -> float:
    df = _closed(trades)
    return 0.0 if df.empty else float((df["pnl"] > 0).mean())


def avg_win(trades: pd.DataFrame) -> float:
    df = _closed(trades)
    wins = df.loc[df["pnl"] > 0, "pnl"]
    return float(wins.mean()) if len(wins) else 0.0


def avg_loss(trades: pd.DataFrame) -> float:
    """Mean losing trade — NEGATIVE."""
    df = _closed(trades)
    losses = df.loc[df["pnl"] < 0, "pnl"]
    return float(losses.mean()) if len(losses) else 0.0


def payoff_ratio(trades: pd.DataFrame) -> float:
    al = avg_loss(trades)
    return 0.0 if al == 0 else float(avg_win(trades) / abs(al))


def profit_factor(trades: pd.DataFrame) -> float:
    df = _closed(trades)
    gains = df.loc[df["pnl"] > 0, "pnl"].sum()
    losses = df.loc[df["pnl"] < 0, "pnl"].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / abs(losses))


def expectancy(trades: pd.DataFrame) -> float:
    """Expected rupees per trade: p*avg_win + (1-p)*avg_loss."""
    wr = win_rate(trades)
    return float(wr * avg_win(trades) + (1.0 - wr) * avg_loss(trades))


def avg_holding_period(trades: pd.DataFrame) -> float:
    """Mean holding period in days. entry_ts/exit_ts are epoch seconds."""
    df = _closed(trades)
    if df.empty or "entry_ts" not in df.columns:
        return 0.0
    held = (df["exit_ts"].astype(float) - df["entry_ts"].astype(float)) / 86400.0
    return float(held.mean())


def turnover_pct(traded_value: float, avg_capital: float) -> float:
    """One-way turnover as a percentage of capital."""
    return 0.0 if avg_capital <= 0 else float(traded_value / avg_capital * 100.0)


def hit_rate_by_exit_reason(trades: pd.DataFrame) -> pd.DataFrame:
    df = _closed(trades)
    if df.empty or "exit_reason" not in df.columns:
        return pd.DataFrame(columns=["trades", "hit_rate", "avg_pnl", "total_pnl"])
    g = df.groupby("exit_reason")["pnl"]
    return pd.DataFrame({
        "trades": g.size(),
        "hit_rate": g.apply(lambda s: float((s > 0).mean())),
        "avg_pnl": g.mean(),
        "total_pnl": g.sum(),
    }).sort_values("total_pnl", ascending=False)


def slippage(orders: pd.DataFrame) -> dict:
    """Fill quality vs the plan's reference price, from rebalance_orders.

    Signed so that POSITIVE always means "worse than planned": buys filled above the
    reference and sells filled below it both count as cost.
    """
    if orders is None or len(orders) == 0:
        return {"orders": 0, "mean_bps": 0.0, "median_bps": 0.0, "total_cost": 0.0}
    df = orders.copy()
    df = df[df["filled_qty"].fillna(0) > 0]
    df = df[df["planned_ref_price"].fillna(0) > 0]
    if df.empty:
        return {"orders": 0, "mean_bps": 0.0, "median_bps": 0.0, "total_cost": 0.0}
    sign = np.where(df["side"].str.upper() == "BUY", 1.0, -1.0)
    diff = (df["avg_fill_price"].astype(float) - df["planned_ref_price"].astype(float)) * sign
    bps = diff / df["planned_ref_price"].astype(float) * 10_000.0
    return {
        "orders": int(len(df)),
        "mean_bps": float(bps.mean()),
        "median_bps": float(bps.median()),
        "total_cost": float((diff * df["filled_qty"].astype(float)).sum()),
    }


def cost_drag(total_costs: float, avg_capital: float) -> float:
    """Explicit costs as a percentage of average capital."""
    return 0.0 if avg_capital <= 0 else float(total_costs / avg_capital * 100.0)


def _fy(ts: pd.Timestamp) -> str:
    """Indian financial year label (Apr-Mar) for a timestamp."""
    y = ts.year if ts.month >= 4 else ts.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def tax_drag(trades: pd.DataFrame, *, avg_capital: float | None = None,
             costs_deductible: bool = False) -> dict:
    """Indian capital-gains tax on realised trades.

    STCG 20% under 12 months; LTCG 12.5% at/over 12 months with a Rs 1.25L exemption
    applied PER FINANCIAL YEAR (Apr-Mar) to LTCG only.

    STT is NOT deductible against capital gains, so by default the `costs` column is
    added back before taxing — i.e. tax is charged on the gross gain. If your `costs`
    column is mostly brokerage/stamp (which ARE deductible), pass costs_deductible=True
    to tax the net figure instead. Losses offset gains within the same bucket and year;
    carry-forward across years is NOT modelled.
    """
    df = _closed(trades)
    empty = {"stcg_tax": 0.0, "ltcg_tax": 0.0, "total_tax": 0.0, "by_fy": pd.DataFrame(),
             "drag_pct": 0.0}
    if df.empty:
        return empty

    entry = pd.to_datetime(df["entry_ts"].astype(float), unit="s")
    exit_ = pd.to_datetime(df["exit_ts"].astype(float), unit="s")
    held_days = (exit_ - entry).dt.days
    gross = df["pnl"].astype(float)
    if not costs_deductible:
        gross = gross + df.get("costs", 0.0).astype(float)

    work = pd.DataFrame({
        "fy": [_fy(t) for t in exit_],
        "long_term": held_days >= LONG_TERM_DAYS,
        "gain": gross.to_numpy(),
    })

    rows = []
    for fy, grp in work.groupby("fy"):
        st_gain = float(grp.loc[~grp["long_term"], "gain"].sum())
        lt_gain = float(grp.loc[grp["long_term"], "gain"].sum())
        st_tax = max(st_gain, 0.0) * STCG_RATE
        lt_tax = max(lt_gain - LTCG_EXEMPTION, 0.0) * LTCG_RATE
        rows.append({"fy": fy, "stcg_gain": st_gain, "ltcg_gain": lt_gain,
                     "stcg_tax": st_tax, "ltcg_tax": lt_tax, "total_tax": st_tax + lt_tax})
    by_fy = pd.DataFrame(rows).set_index("fy").sort_index()

    total = float(by_fy["total_tax"].sum())
    return {
        "stcg_tax": float(by_fy["stcg_tax"].sum()),
        "ltcg_tax": float(by_fy["ltcg_tax"].sum()),
        "total_tax": total,
        "by_fy": by_fy,
        "drag_pct": 0.0 if not avg_capital else float(total / avg_capital * 100.0),
    }


def per_symbol_contribution(trades: pd.DataFrame) -> pd.DataFrame:
    df = _closed(trades)
    if df.empty:
        return pd.DataFrame(columns=["pnl", "trades", "share_pct"])
    g = df.groupby("symbol")["pnl"]
    out = pd.DataFrame({"pnl": g.sum(), "trades": g.size()})
    total = out["pnl"].abs().sum()
    out["share_pct"] = 0.0 if total == 0 else out["pnl"] / total * 100.0
    return out.sort_values("pnl", ascending=False)


def per_sector_contribution(trades: pd.DataFrame,
                            sectors: Mapping[str, str] | None = None) -> pd.DataFrame:
    df = _closed(trades)
    if df.empty:
        return pd.DataFrame(columns=["pnl", "trades", "share_pct"])
    mapping = dict(sectors) if sectors is not None else load_sectors()
    df = df.assign(cluster=df["symbol"].map(lambda s: mapping.get(s, "other")))
    g = df.groupby("cluster")["pnl"]
    out = pd.DataFrame({"pnl": g.sum(), "trades": g.size()})
    total = out["pnl"].abs().sum()
    out["share_pct"] = 0.0 if total == 0 else out["pnl"] / total * 100.0
    return out.sort_values("pnl", ascending=False)


# =====================================================================================
# momentum diagnostics
# =====================================================================================
DEFAULT_SCORE_BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 101)]


def perf_by_score_bucket(trades: pd.DataFrame,
                         buckets: Sequence[tuple[float, float]] | None = None,
                         score_col: str = "entry_score") -> pd.DataFrame:
    """Did higher Momentum Quality Scores actually pay? The core strategy question."""
    df = _closed(trades)
    if df.empty or score_col not in df.columns:
        return pd.DataFrame(columns=["trades", "win_rate", "avg_pnl", "total_pnl"])
    bs = list(buckets or DEFAULT_SCORE_BUCKETS)
    edges = [b[0] for b in bs] + [bs[-1][1]]
    labels = [f"{lo:g}-{hi:g}" for lo, hi in bs]
    df = df.assign(bucket=pd.cut(df[score_col].astype(float), bins=edges,
                                 labels=labels, right=False, include_lowest=True))
    g = df.groupby("bucket", observed=False)["pnl"]
    return pd.DataFrame({
        "trades": g.size(),
        "win_rate": g.apply(lambda s: float((s > 0).mean()) if len(s) else 0.0),
        "avg_pnl": g.mean(),
        "total_pnl": g.sum(),
    })


def perf_by_rank_bucket(trades: pd.DataFrame, rank_col: str = "entry_rank",
                        buckets: Sequence[tuple[int, int]] = ((1, 6), (6, 15), (15, 26))
                        ) -> pd.DataFrame:
    """Rank 1-5 vs 15-25 — the table phase 3 renders."""
    df = _closed(trades)
    if df.empty or rank_col not in df.columns:
        return pd.DataFrame(columns=["trades", "win_rate", "avg_pnl", "total_pnl"])
    edges = [b[0] for b in buckets] + [buckets[-1][1]]
    labels = [f"{lo}-{hi - 1}" for lo, hi in buckets]
    df = df.assign(bucket=pd.cut(df[rank_col].astype(float), bins=edges,
                                 labels=labels, right=False))
    g = df.groupby("bucket", observed=False)["pnl"]
    return pd.DataFrame({
        "trades": g.size(),
        "win_rate": g.apply(lambda s: float((s > 0).mean()) if len(s) else 0.0),
        "avg_pnl": g.mean(),
        "total_pnl": g.sum(),
    })


def score_decay_vs_return(trades: pd.DataFrame, score_col: str = "entry_score",
                          return_col: str | None = None) -> dict:
    """Correlation between entry score and realised return.

    Pearson measures linear strength; Spearman measures whether the RANKING held,
    which is what a momentum screen actually claims to deliver.
    """
    df = _closed(trades)
    if df.empty or score_col not in df.columns:
        return {"n": 0, "pearson": float("nan"), "spearman": float("nan")}
    if return_col and return_col in df.columns:
        ret = df[return_col].astype(float)
    else:
        basis = (df["entry_price"].astype(float) * df["qty"].astype(float)).replace(0, np.nan)
        ret = df["pnl"].astype(float) / basis
    ok = pd.DataFrame({"s": df[score_col].astype(float), "r": ret}).dropna()
    if len(ok) < 3:
        return {"n": int(len(ok)), "pearson": float("nan"), "spearman": float("nan")}
    # Pearson of the ranks IS Spearman — computing it this way avoids pulling in scipy,
    # which pandas 3.x would otherwise require for method="spearman".
    return {"n": int(len(ok)),
            "pearson": float(ok["s"].corr(ok["r"])),
            "spearman": float(ok["s"].rank().corr(ok["r"].rank()))}


def exit_efficiency(trades: pd.DataFrame, fwd_col: str = "fwd_return_20d") -> dict:
    """Did exits add value? Needs a column of each name's return N days AFTER exit.

    Kept pure: the caller supplies forward returns (phase 3 fills them from stored
    prices), so nothing here touches the network. A GOOD exit is followed by a fall,
    hence efficiency = -mean(forward return): positive means selling helped.
    """
    df = _closed(trades)
    if df.empty or fwd_col not in df.columns:
        return {"n": 0, "efficiency": float("nan"), "mean_fwd_return": float("nan"),
                "pct_good_exits": float("nan")}
    fwd = df[fwd_col].astype(float).dropna()
    if fwd.empty:
        return {"n": 0, "efficiency": float("nan"), "mean_fwd_return": float("nan"),
                "pct_good_exits": float("nan")}
    return {"n": int(len(fwd)), "efficiency": float(-fwd.mean()),
            "mean_fwd_return": float(fwd.mean()),
            "pct_good_exits": float((fwd < 0).mean() * 100.0)}


def stop_loss_effectiveness(trades: pd.DataFrame, fwd_col: str = "fwd_return_20d",
                            reason_match: str = "stop") -> dict:
    """Same question, restricted to stop-outs: did the stops save money or shake you out?"""
    df = _closed(trades)
    if df.empty or "exit_reason" not in df.columns:
        return {"n": 0, "efficiency": float("nan"), "saved_by_stop_pct": float("nan"),
                "shaken_out_pct": float("nan")}
    stops = df[df["exit_reason"].astype(str).str.contains(reason_match, case=False, na=False)]
    eff = exit_efficiency(stops, fwd_col)
    if fwd_col in stops.columns:
        fwd = stops[fwd_col].astype(float).dropna()
        saved = float((fwd < 0).mean() * 100.0) if len(fwd) else float("nan")
        shaken = float((fwd > 0).mean() * 100.0) if len(fwd) else float("nan")
    else:
        saved = shaken = float("nan")
    return {"n": int(len(stops)), "efficiency": eff["efficiency"],
            "saved_by_stop_pct": saved, "shaken_out_pct": shaken}


def cash_drag(navs: Any, cash: Any, invested: Any = None,
              cashflows: Any = None) -> dict:
    """Cost of holding cash, as an ESTIMATE.

    Attributing drag exactly needs the invested leg's own flow-adjusted return, which
    the snapshot series does not separate. This uses the standard approximation:
    average cash weight x the annualised return the invested leg earned. Reported as a
    positive percentage of return foregone.
    """
    nav = _series(navs)
    c = _series(cash).reindex(nav.index).ffill()
    weight = (c / nav.replace(0, np.nan)).dropna()
    avg_w = float(weight.mean()) if len(weight) else 0.0

    if invested is not None:
        inv = _series(invested).reindex(nav.index).ffill()
        inv_ret = cagr(inv[inv > 0]) if (inv > 0).any() else 0.0
    else:
        inv_ret = cagr(twr_chained(nav, cashflows))

    return {"avg_cash_weight_pct": avg_w * 100.0,
            "max_cash_weight_pct": float(weight.max() * 100.0) if len(weight) else 0.0,
            "invested_leg_cagr": float(inv_ret),
            "estimated_drag_pct": float(avg_w * inv_ret * 100.0)}


def weekly_turnover_trend(orders: pd.DataFrame, capital: Any,
                          ts_col: str = "created_ts") -> pd.DataFrame:
    """One-way traded value per ISO week, as a percentage of capital that week."""
    if orders is None or len(orders) == 0 or ts_col not in getattr(orders, "columns", []):
        return pd.DataFrame(columns=["traded_value", "capital", "turnover_pct"])
    df = orders.copy()
    df["when"] = pd.to_datetime(df[ts_col].astype(float), unit="s")
    qty = df.get("filled_qty", df.get("planned_qty", 0)).astype(float).abs()
    px = df.get("avg_fill_price", df.get("planned_ref_price", 0.0)).astype(float)
    df["value"] = qty * px
    weekly = df.set_index("when")["value"].resample("W").sum()

    cap = _series(capital).reindex(weekly.index, method="ffill") if not np.isscalar(capital) \
        else pd.Series(float(capital), index=weekly.index)
    out = pd.DataFrame({"traded_value": weekly, "capital": cap})
    out["turnover_pct"] = np.where(out["capital"] > 0,
                                   out["traded_value"] / out["capital"] * 100.0, 0.0)
    return out


# =====================================================================================
# benchmarking
# =====================================================================================
def up_capture(port_monthly: Any, bench_monthly: Any) -> float:
    """Compounded portfolio return in the benchmark's UP months / the benchmark's own."""
    p, b = _align(port_monthly, bench_monthly)
    mask = b > 0
    if not mask.any():
        return float("nan")
    bp = float((1.0 + b[mask]).prod() - 1.0)
    if bp == 0:
        return float("nan")
    return float(((1.0 + p[mask]).prod() - 1.0) / bp)


def down_capture(port_monthly: Any, bench_monthly: Any) -> float:
    """Same in the benchmark's DOWN months. Below 1.0 is good (you fell less)."""
    p, b = _align(port_monthly, bench_monthly)
    mask = b < 0
    if not mask.any():
        return float("nan")
    bp = float((1.0 + b[mask]).prod() - 1.0)
    if bp == 0:
        return float("nan")
    return float(((1.0 + p[mask]).prod() - 1.0) / bp)


def equity_curve_vs_benchmark(port_index: Any, benchmarks: Mapping[str, Any],
                              base: float = 100.0,
                              series_types: Mapping[str, str] | None = None) -> dict:
    """Rebase portfolio + benchmarks to `base` at their common inception.

    Returns the curve plus a `series_used` map, because a Nifty 500 PRI comparison and
    a Nifty 500 TRI comparison are different claims and every chart must say which one
    it drew. TRI is authoritative for reported numbers; PRI understates the benchmark
    by roughly the dividend yield.
    """
    p = _series(port_index).dropna()
    if p.empty:
        return {"curve": pd.DataFrame(), "series_used": {}, "inception": None}

    cols: dict[str, pd.Series] = {}
    dropped: list[str] = []
    start = p.index[0]
    for name, series in benchmarks.items():
        b = _series(series).dropna()
        b = b[b.index >= start]
        if b.empty or b.iloc[0] == 0:
            # No overlap with the portfolio's life, so it cannot be rebased. Reported
            # rather than silently omitted — a missing line on a chart must be explainable.
            dropped.append(name)
            continue
        cols[name] = b / b.iloc[0] * base

    curve = pd.DataFrame({"portfolio": p / p.iloc[0] * base, **cols})
    curve = curve.sort_index().ffill().dropna(how="all")
    used = {"portfolio": "NAV"}
    used.update({k: (series_types or {}).get(k, "UNKNOWN") for k in cols})
    return {"curve": curve, "series_used": used, "dropped": dropped,
            "inception": start.date().isoformat()}


def summary(navs: Any, cashflows: Any = None, *, benchmark: Any = None,
            rf: float = 0.0, series_type: str = "UNKNOWN") -> dict:
    """Headline metric bundle for the phase-3 cards. Pure; no I/O."""
    nav = _series(navs)
    idx = twr_chained(nav, cashflows)
    r = daily_returns(nav, cashflows)
    mdd = max_drawdown(idx)

    out = {
        "inception": idx.index[0].date().isoformat() if len(idx) else None,
        "as_of": idx.index[-1].date().isoformat() if len(idx) else None,
        "days": int(len(idx)),
        "index_value": float(idx.iloc[-1]) if len(idx) else None,
        "absolute": absolute(idx),
        "cagr": cagr(idx),
        "twr": twr_total(nav, cashflows),
        "ann_volatility": ann_volatility(r),
        "sharpe": sharpe(r, rf),
        "sortino": sortino(r, rf),
        "calmar": calmar(idx),
        "max_drawdown": mdd["depth"],
        "max_drawdown_duration_days": mdd["duration_days"],
        "max_drawdown_recovered": mdd["recovered"],
        "current_drawdown": mdd["current_dd"],
        "var95_parametric": var_parametric(r),
        "var95_historical": var_historical(r),
        "benchmark_series": None,
    }
    if benchmark is not None:
        b = _series(benchmark)
        br = b.pct_change().dropna()
        out.update({
            "benchmark_series": series_type,
            "beta": beta(r, br),
            "alpha_jensen": alpha_jensen(r, br, rf),
            "tracking_error": tracking_error(r, br),
            "information_ratio": information_ratio(r, br),
            "up_capture": up_capture(monthly_returns(idx), monthly_returns(b)),
            "down_capture": down_capture(monthly_returns(idx), monthly_returns(b)),
        })
    return out
