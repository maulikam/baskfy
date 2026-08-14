#!/usr/bin/env python
"""Regime overlay backtest — variants, ablations, costs, robustness, event timelines.

WHAT THIS CAN AND CANNOT CLAIM
Signals replay through the SAME pure functions the live engine uses
(app/core/regime.py), so a historical tier is exactly what the live engine would have
decided from those inputs. Everything else is bounded by the data that exists:

- PORTFOLIO PROXY. There is no stock-level historical book, so performance is measured on
  the NIFTY 500 MOMENTUM 50 return series as a proxy. It is labelled proxy everywhere and
  does NOT reproduce the live stock-selection portfolio.
- PRICE RETURN. Kite index candles are price-return only. Results are labelled PRI unless
  a TRI series is supplied.
- NO HISTORICAL BREADTH. breadth_readings only accumulates from the day you start saving
  it, so any pre-existing period is a PRICE-ONLY PROXY of the live regime. Breadth rules
  are treated as "not evaluable" for those weeks — which, by the engine's own asymmetry,
  means they can neither force risk-off nor permit R1.
- VARIANT D IS NOT AVAILABLE. It needs stock-level holdings, exit decisions and skipped
  replacements. Approximating it as a fixed allocation would be a different strategy
  wearing its name, so it is reported as NOT AVAILABLE.

NO LOOKAHEAD
The signal uses the completed weekly close; the exposure change executes at the NEXT
eligible session's OPEN from cached OHLC. Executing at the signal-generating close would
be trading on a price you could not have had.

Usage:
    python -m scripts.backtest_regime --list
    python -m scripts.backtest_regime --fetch --start 2005-01-01
    python -m scripts.backtest_regime --run --start 2020-01-01
    python -m scripts.backtest_regime --run --robustness
    python -m scripts.backtest_regime --run --timelines
    python -m scripts.backtest_regime --run --portfolio-series data/uploads/port.csv
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from app import config as C                                    # noqa: E402
from app.analytics import breadth as B                          # noqa: E402
from app.analytics import db, index_cache as IC, metrics as M    # noqa: E402
from app.core import regime as R                                # noqa: E402

TRADING_WEEKS = 52


# =====================================================================================
# cost model
# =====================================================================================
@dataclass(frozen=True)
class CostModel:
    """Charges are applied to TRADED NOTIONAL, never to the whole portfolio.

    An R1 -> R2 move on Rs 1 crore trades Rs 30 lakh (a 30-point exposure change), not
    Rs 1 crore. Charging the full corpus per transition would overstate costs by ~3x and
    make any overlay look unusable.
    """
    stt_pct: float = 0.001            # 0.1% per side, delivery
    brokerage_pct: float = 0.0        # Zerodha delivery is zero
    exchange_pct: float = 0.0000325
    sebi_pct: float = 0.000001
    gst_pct: float = 0.18             # on brokerage + exchange charges
    stamp_pct_buy: float = 0.00015
    dp_per_scrip: float = 0.0         # index proxy has no scrip-level DP charge
    slippage_pct: float = 0.0005

    def one_way(self, notional: float, *, is_buy: bool) -> float:
        n = abs(notional)
        stt = n * self.stt_pct
        brokerage = n * self.brokerage_pct
        exch = n * self.exchange_pct
        sebi = n * self.sebi_pct
        gst = (brokerage + exch) * self.gst_pct
        stamp = n * self.stamp_pct_buy if is_buy else 0.0
        slip = n * self.slippage_pct
        return stt + brokerage + exch + sebi + gst + stamp + slip

    def round_trip(self, notional: float) -> float:
        return self.one_way(notional, is_buy=True) + self.one_way(notional, is_buy=False)

    def breakdown(self, notional: float, *, is_buy: bool) -> dict:
        n = abs(notional)
        brokerage, exch = n * self.brokerage_pct, n * self.exchange_pct
        return {"stt": n * self.stt_pct, "brokerage": brokerage, "exchange": exch,
                "sebi": n * self.sebi_pct, "gst": (brokerage + exch) * self.gst_pct,
                "stamp": n * self.stamp_pct_buy if is_buy else 0.0,
                "dp": self.dp_per_scrip, "slippage": n * self.slippage_pct}


SLIPPAGE_SCENARIOS = {"low": 0.0002, "base": 0.0005, "stressed_smallcap": 0.0025}


# =====================================================================================
# variants
# =====================================================================================
@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    kind: str                     # none | binary | tiered
    ma: int | None = None         # for binary designs
    buffered: bool = True
    use_structural: bool = True
    use_sentinel: bool = True
    use_breadth: bool = True
    available: bool = True
    unavailable_reason: str = ""


VARIANTS: tuple[Variant, ...] = (
    Variant("A", "No regime overlay", "none"),
    Variant("B_raw", "Binary M50 50-DMA, raw crossing", "binary", ma=50, buffered=False),
    Variant("B_buf", "Binary M50 50-DMA, buffer+confirm", "binary", ma=50, buffered=True),
    Variant("C_raw", "Binary M50 200-DMA, raw crossing", "binary", ma=200, buffered=False),
    Variant("C_buf", "Binary M50 200-DMA, buffer+confirm", "binary", ma=200, buffered=True),
    Variant("D", "M50 50-DMA middle ground (skip entries, no liquidation)", "tiered",
            available=False,
            unavailable_reason="requires stock-level holdings, exit decisions and skipped "
                               "replacements; cannot be approximated from an index series"),
    Variant("E", "Three-index tiered, no sentinel", "tiered", use_sentinel=False),
    Variant("F", "Full tiered model", "tiered"),
    # ablations
    Variant("abl_breadth", "Ablation: breadth only", "tiered", use_structural=False,
            use_sentinel=False),
    Variant("abl_sentinel", "Ablation: Momentum 50 only", "tiered", use_structural=False,
            use_breadth=False),
    Variant("abl_structural", "Ablation: structural indices only", "tiered",
            use_sentinel=False, use_breadth=False),
    Variant("abl_struct_breadth", "Ablation: structural + breadth", "tiered",
            use_sentinel=False),
    Variant("abl_struct_sentinel", "Ablation: structural + Momentum 50", "tiered",
            use_breadth=False),
    Variant("abl_complete", "Ablation: complete model", "tiered"),
)


# =====================================================================================
# data
# =====================================================================================
def load_ohlc(conn, name: str) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT date, open, high, low, close FROM index_series "
        "WHERE index_name=? AND is_final=1 ORDER BY date", (name,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def breadth_covers_window(conn, start: dt.date, end: dt.date,
                          min_readings: int = 26, min_span_frac: float = 0.5) -> bool:
    """Is there enough stored breadth to replay this window?

    A single recent reading is not coverage. Without this check one row makes a 21-year
    backtest believe breadth was available, and the engine's degraded-input lock then
    freezes the tier for the entire history.
    """
    h = breadth_history(conn)
    if h.empty:
        return False
    inside = h[(h.index >= pd.Timestamp(start)) & (h.index <= pd.Timestamp(end))]
    if len(inside) < min_readings:
        return False
    span = (inside.index[-1] - inside.index[0]).days
    total = max((pd.Timestamp(end) - pd.Timestamp(start)).days, 1)
    return span / total >= min_span_frac


def breadth_history(conn) -> pd.Series:
    rows = B.history(conn)
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({pd.Timestamp(r.as_of_date): r.pct_above_20dma for r in rows})


def weekly_signal_sessions(sessions: Sequence[pd.Timestamp], weekday: int
                           ) -> list[pd.Timestamp]:
    """Last aligned session of each ISO week — the holiday-tolerant evaluation date."""
    if not len(sessions):
        return []
    s = pd.Series(sessions, index=pd.DatetimeIndex(sessions))
    grouped = s.groupby([s.index.isocalendar().year, s.index.isocalendar().week]).max()
    return sorted(grouped.tolist())


# =====================================================================================
# replay
# =====================================================================================
class SignalTape:
    """Precomputed per-session signal states for one index.

    The confirmed-state series is CAUSAL — state[t] depends only on closes up to t — so
    computing it once over the full history and indexing into it gives byte-identical
    results to rebuilding from scratch each week, at O(n) instead of O(n^2). The same
    pure engine functions are used either way.
    """

    def __init__(self, name: str, ohlc: pd.DataFrame, cfg: R.RegimeConfig):
        self.name = name
        self.dates = list(ohlc.index)
        self.closes = ohlc["close"].astype(float).tolist()
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self.mas: dict[int, list] = {}
        self.states: dict[int, list] = {}
        for length in cfg.ma_lengths:
            mas = R.moving_average(self.closes, length)
            self.mas[length] = mas
            self.states[length] = R.confirmed_state_series(
                self.closes, mas, buffer=cfg.buffer, confirm_days=cfg.confirm_days)
        self.cfg = cfg

    def at(self, session: pd.Timestamp) -> R.IndexSignals:
        i = self.pos[session]
        close = self.closes[i]
        sigs = {}
        for length in self.cfg.ma_lengths:
            ma = self.mas[length][i]
            state, run = self.states[length][i]
            sigs[length] = R.MaSignal(
                ma_length=length, close=close, ma=ma,
                distance_pct=None if not ma else (close / ma - 1.0) * 100.0,
                state=state, confirming_closes=run)
        return R.IndexSignals(index_name=self.name, as_of_date=session.date(),
                              close=close, signals=sigs, is_stale=False, has_data=True)


def replay(conn, cfg: R.RegimeConfig, variant: Variant, *, start: dt.date, end: dt.date,
           tapes: dict | None = None) -> pd.DataFrame:
    """Weekly tier decisions through the LIVE pure engine. One row per evaluation."""
    names = list(cfg.structural_indices.values())
    sentinel_name = cfg.momentum_sentinel
    ohlc = {n: load_ohlc(conn, n) for n in names + [sentinel_name]}
    missing = [n for n, d in ohlc.items() if d.empty]
    if missing:
        raise SystemExit(f"no cached history for {missing} — run with --fetch first")

    aligned = sorted(set.intersection(*[set(d.index) for d in ohlc.values()]))
    aligned = [d for d in aligned if start <= d.date() <= end]
    weeks = weekly_signal_sessions(aligned, cfg.weekly_evaluation_weekday)
    brd = breadth_history(conn)
    if tapes is None:
        tapes = {n: SignalTape(n, ohlc[n], cfg) for n in names + [sentinel_name]}

    book = R.resolve_book_weights(cfg)
    prev: R.PreviousRegime | None = None
    out = []

    for session in weeks:
        # Signals are built ONLY from candles at or before the signal session.
        sigs = {n: tapes[n].at(session) for n in names + [sentinel_name]}

        structural = {n: sigs[n] for n in names}
        if not variant.use_structural:
            # Neutralise the structural leg without pretending it is bullish: drop it and
            # let the remaining rules decide.
            structural = {n: replace(sigs[n], signals={}) for n in names}
        sentinel = sigs[sentinel_name] if variant.use_sentinel else None

        reading = None
        if variant.use_breadth and len(brd):
            past = brd[brd.index <= session]
            if len(past):
                reading = R.BreadthReading(
                    as_of_date=past.index[-1].date(), pct_above_20dma=float(past.iloc[-1]),
                    eligible_count=500, observed_count=500, coverage_pct=100.0,
                    universe_id="scan", universe_hash="replay",
                    audit_run_id="replay", calculation_version="replay")

        st = R.evaluate(
            as_of_date=session.date(), scheduled_week_end=session.date(),
            signal_session_date=session.date(), index_signals=structural,
            sentinel=sentinel, breadth=reading, book=book,
            exposure=R.ExposureSnapshot(actual_equity_pct=0.0), cfg=cfg, previous=prev)

        if variant.kind == "none":
            exposure = 100.0
        elif variant.kind == "binary":
            sig = sigs[sentinel_name]
            if variant.buffered:
                below = sig.is_below(variant.ma)
            else:
                ma = sig.signals[variant.ma].ma
                below = bool(ma and sig.close is not None and sig.close < ma)
            exposure = 0.0 if below else 100.0
        else:
            exposure = cfg.cap_for(st.policy_tier)

        out.append({"session": session, "tier": st.policy_tier.value,
                    "raw_tier": st.raw_candidate_tier.value, "exposure": exposure,
                    "new_buys": st.new_buys.value, "data_stale": st.data_stale,
                    "breadth": reading.pct_above_20dma if reading else np.nan,
                    "H20": st.health.get(20), "H50": st.health.get(50),
                    "H200": st.health.get(200),
                    "sentinel_50": sigs[sentinel_name].state(50).value,
                    "sentinel_200": sigs[sentinel_name].state(200).value,
                    "reason_codes": ";".join(st.reason_codes)})
        prev = R.PreviousRegime(policy_tier=st.policy_tier,
                                scheduled_week_end=session.date(),
                                last_transition_date=st.last_transition_date,
                                recovery_confirmations=0, book_weights=book)
    return pd.DataFrame(out).set_index("session")


# =====================================================================================
# simulation
# =====================================================================================
def simulate(schedule: pd.DataFrame, proxy: pd.DataFrame, *, costs: CostModel,
             cash_rate: float = 0.0, execution: str = "next_open") -> dict:
    """Apply the weekly exposure schedule to the proxy series, next-open execution."""
    px = proxy["close"].copy()
    opens = proxy["open"].copy()
    sessions = list(px.index)
    pos = {d: i for i, d in enumerate(sessions)}

    # exposure change becomes effective at the NEXT session's open
    effective: dict[pd.Timestamp, float] = {}
    for sig_date, row in schedule.iterrows():
        i = pos.get(sig_date)
        if i is None or i + 1 >= len(sessions):
            continue
        effective[sessions[i + 1]] = float(row["exposure"])

    nav, exposure = 1.0, 100.0
    daily_r = px.pct_change().fillna(0.0)
    cash_daily = (1.0 + cash_rate) ** (1 / 252) - 1.0

    navs, exps, transitions = [], [], 0
    # Accumulated as FRACTIONS OF NAV AT TRADE TIME. Summing rupee notionals across a
    # compounding curve would make late trades dominate and produce turnover in the
    # thousands of percent — a units error, not a strategy property.
    traded_frac, cost_frac = 0.0, 0.0
    for i, d in enumerate(sessions):
        if d in effective and abs(effective[d] - exposure) > 1e-9:
            new = effective[d]
            delta = abs(new - exposure) / 100.0
            notional = nav * delta
            # Execution happens at the OPEN, so the cost lands before the day's return.
            cost = costs.one_way(notional, is_buy=new > exposure)
            cost_frac += cost / nav
            nav -= cost
            traded_frac += delta
            exposure = new
            transitions += 1
        r = daily_r.iloc[i]
        nav *= (1.0 + (exposure / 100.0) * r + (1.0 - exposure / 100.0) * cash_daily)
        navs.append(nav)
        exps.append(exposure)

    curve = pd.Series(navs, index=sessions) * 100.0
    return {"curve": curve, "exposure": pd.Series(exps, index=sessions),
            "traded_frac": traded_frac, "cost_frac": cost_frac,
            "transitions": transitions, "execution": execution}


# =====================================================================================
# whipsaw
# =====================================================================================
@dataclass(frozen=True)
class WhipsawSpec:
    drop_points: float = 20.0
    restore_frac: float = 0.80
    window_evals: int = 4
    benchmark_dd: float = 0.05


def count_whipsaws(schedule: pd.DataFrame, benchmark: pd.Series,
                   spec: WhipsawSpec) -> dict:
    """A reduction that reverses quickly WITHOUT the benchmark having fallen."""
    exp = schedule["exposure"].to_numpy()
    dates = list(schedule.index)
    hits = []
    for i in range(1, len(exp)):
        drop = exp[i - 1] - exp[i]
        if drop < spec.drop_points:
            continue
        j_end = min(i + spec.window_evals, len(exp) - 1)
        restored = max(exp[i:j_end + 1]) - exp[i]
        if restored < spec.restore_frac * drop:
            continue
        window = benchmark.loc[dates[i]:dates[j_end]]
        if len(window) < 2:
            continue
        dd = abs(M.max_drawdown(window)["depth"])
        if dd < spec.benchmark_dd:
            hits.append({"date": dates[i].date().isoformat(), "drop": float(drop),
                         "restored": float(restored), "benchmark_dd": round(dd, 4)})
    years = max((dates[-1] - dates[0]).days / 365.0, 1e-9) if len(dates) > 1 else 1.0
    return {"count": len(hits), "per_year": round(len(hits) / years, 3), "events": hits}


def exposure_reversals(schedule: pd.DataFrame) -> int:
    d = np.sign(schedule["exposure"].diff().fillna(0.0).to_numpy())
    d = d[d != 0]
    return int(np.sum(d[1:] != d[:-1]))


# =====================================================================================
# reporting
# =====================================================================================
def performance(sim: dict, schedule: pd.DataFrame, benchmark: pd.Series,
                spec: WhipsawSpec) -> dict:
    curve = sim["curve"]
    r = curve.pct_change().dropna()
    dd = M.max_drawdown(curve)
    exp = sim["exposure"]
    years = max((curve.index[-1] - curve.index[0]).days / 365.0, 1e-9)

    rolling = M.rolling_returns(curve, {"1Y": 252, "3Y": 756, "5Y": 1260})
    tier_share = (schedule["tier"].value_counts(normalize=True) * 100).round(2).to_dict()

    bench_r = benchmark.pct_change().dropna()
    bull = bench_r[bench_r > 0].index
    bear_months = M.monthly_returns(benchmark)
    port_months = M.monthly_returns(curve)

    return {
        "CAGR": round(M.cagr(curve) * 100, 2),
        "max_drawdown_pct": round(dd["depth"] * 100, 2),
        "dd_duration_days": dd["duration_days"],
        "dd_recovered": dd["recovered"],
        "volatility_pct": round(M.ann_volatility(r) * 100, 2),
        "sharpe": round(M.sharpe(r), 3),
        "sortino": round(M.sortino(r), 3),
        "calmar": round(M.calmar(curve), 3),
        "roll_1Y_worst_pct": round(float(rolling["1Y"].min() * 100), 2)
        if rolling["1Y"].notna().any() else None,
        "roll_3Y_med_pct": round(float(rolling["3Y"].median() * 100), 2)
        if rolling["3Y"].notna().any() else None,
        "roll_5Y_med_pct": round(float(rolling["5Y"].median() * 100), 2)
        if rolling["5Y"].notna().any() else None,
        "pct_time_in_cash": round(float((exp < 1e-9).mean() * 100), 2),
        "avg_equity_exposure": round(float(exp.mean()), 2),
        "tier_time_pct": tier_share,
        "transitions": sim["transitions"],
        "transitions_per_year": round(sim["transitions"] / years, 2),
        "exposure_reversals": exposure_reversals(schedule),
        "annual_turnover_pct": round(sim["traded_frac"] / years * 100, 1),
        "total_cost_drag_pct_yr": round(sim["cost_frac"] / years * 100, 3),
        "whipsaws": count_whipsaws(schedule, benchmark, spec)["count"],
        "whipsaws_per_year": count_whipsaws(schedule, benchmark, spec)["per_year"],
        "up_capture": round(M.up_capture(port_months, bear_months), 3)
        if len(bear_months) > 2 else None,
        "down_capture": round(M.down_capture(port_months, bear_months), 3)
        if len(bear_months) > 2 else None,
    }


def run_all(conn, cfg: R.RegimeConfig, *, start: dt.date, end: dt.date,
            costs: CostModel, cash_rate: float, spec: WhipsawSpec,
            proxy_name: str) -> tuple[pd.DataFrame, dict]:
    proxy = load_ohlc(conn, proxy_name)
    proxy = proxy.loc[str(start):str(end)]
    benchmark = proxy["close"]

    names = list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]
    tapes = {n: SignalTape(n, load_ohlc(conn, n), cfg) for n in names}
    has_breadth = breadth_covers_window(conn, start, end)

    rows, detail = {}, {}
    for v in VARIANTS:
        if not v.available:
            rows[v.key] = {"label": v.label, "status": "NOT AVAILABLE",
                           "reason": v.unavailable_reason}
            continue
        if v.use_breadth and not has_breadth and v.kind == "tiered":
            note = ("breadth history unavailable — evaluated as a PRICE-ONLY PROXY with "
                    "breadth rules disabled; this is NOT the full live regime")
        else:
            note = ""
        vcfg = replace(cfg, breadth_required=v.use_breadth and has_breadth)
        sched = replay(conn, vcfg, v, start=start, end=end, tapes=tapes)
        sim = simulate(sched, proxy, costs=costs, cash_rate=cash_rate)
        perf = performance(sim, sched, benchmark, spec)
        perf.update({"label": v.label, "status": "ok", "note": note})
        rows[v.key] = perf
        detail[v.key] = {"schedule": sched, "sim": sim}
    return pd.DataFrame(rows).T, detail


# =====================================================================================
# CLI
# =====================================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="regime overlay backtest")
    ap.add_argument("--db", default=None)
    ap.add_argument("--fetch", action="store_true", help="download max index history first")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--robustness", action="store_true")
    ap.add_argument("--timelines", action="store_true")
    ap.add_argument("--cash-rate", type=float, default=0.0)
    ap.add_argument("--slippage", choices=list(SLIPPAGE_SCENARIOS), default="base")
    ap.add_argument("--reversal-windows", default="2,4,8")
    ap.add_argument("--save", nargs="?", const="data/outputs/regime_backtest.json",
                    default=None, metavar="PATH",
                    help="write results as JSON for the /regime/backtest page")
    ap.add_argument("--portfolio-series", default=None,
                    help="CSV of an existing historical portfolio return series")
    a = ap.parse_args()

    cfg = C.regime_config()
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today()
    costs = CostModel(slippage_pct=SLIPPAGE_SCENARIOS[a.slippage])
    spec = WhipsawSpec()

    if a.list:
        print(f"{'key':<22}{'status':<16}{'design'}")
        for v in VARIANTS:
            print(f"  {v.key:<20}{'ok' if v.available else 'NOT AVAILABLE':<16}{v.label}")
        return

    with db.connect(a.db) as conn:
        db.migrate(conn)

        if a.fetch:
            from app.kite_client import Kite
            res = asyncio.run(IC.update_all(Kite(), conn, cfg, start=start,
                                            full_refresh=True))
            for r in res:
                print(f"  {r['index_name']:<26} {r['written']:>6} rows  "
                      f"{r.get('first')} -> {r.get('last')}")
            if not a.run:
                return

        if not a.run:
            ap.print_help()
            return

        print("=" * 92)
        print("REGIME BACKTEST — LIMITATIONS")
        print("=" * 92)
        brd = breadth_history(conn)
        print(f"  portfolio series : NIFTY 500 MOMENTUM 50 index — PROXY, not the live book")
        print(f"  return type      : PRICE RETURN (Kite indices carry no TRI)")
        covers = breadth_covers_window(conn, start, end)
        print(f"  breadth history  : {len(brd)} stored readings — "
              f"{'sufficient coverage' if covers else 'INSUFFICIENT for this window; '
                 'tiered variants run as a PRICE-ONLY PROXY with breadth rules disabled'}")
        print(f"  execution        : next session OPEN after the weekly close (no lookahead)")
        print(f"  book weights     : configured defaults (no historical full-risk snapshots)")
        print(f"  window           : {start} -> {end}")
        print(f"  slippage         : {a.slippage} ({SLIPPAGE_SCENARIOS[a.slippage]*100:.2f}%)")
        print(f"  cash return      : {a.cash_rate*100:.2f}%")
        print()

        # One config for every downstream section. Without this the robustness grid and
        # timelines silently fall back to the breadth-required config and every row comes
        # out identical — the lock, not the parameter, doing the deciding.
        bt_cfg = replace(cfg, breadth_required=breadth_covers_window(conn, start, end))
        table, detail = run_all(conn, cfg, start=start, end=end, costs=costs,
                                cash_rate=a.cash_rate, spec=spec,
                                proxy_name=cfg.momentum_sentinel)

        cols = ["label", "status", "CAGR", "max_drawdown_pct", "volatility_pct", "sharpe",
                "sortino", "calmar", "avg_equity_exposure", "pct_time_in_cash",
                "transitions", "whipsaws_per_year", "annual_turnover_pct",
                "total_cost_drag_pct_yr"]
        print("=" * 92)
        print("VARIANTS")
        print("=" * 92)
        print(table.reindex(columns=cols).to_string())
        print()
        for k, row in table.iterrows():
            if row.get("status") == "NOT AVAILABLE":
                print(f"  {k}: NOT AVAILABLE — {row.get('reason')}")
            elif row.get("note"):
                print(f"  {k}: {row['note']}")

        # cash-return sensitivity
        print()
        print("=" * 92)
        print("CASH RETURN SENSITIVITY (variant F)")
        print("=" * 92)
        vf = next(v for v in VARIANTS if v.key == "F")
        sched = replay(conn, bt_cfg, vf, start=start, end=end)
        proxy = load_ohlc(conn, cfg.momentum_sentinel).loc[str(start):str(end)]
        for rate in (0.0, 0.065):
            sim = simulate(sched, proxy, costs=costs, cash_rate=rate)
            perf = performance(sim, sched, proxy["close"], spec)
            print(f"  cash {rate*100:>5.2f}%  CAGR {perf['CAGR']:>7.2f}%  "
                  f"maxDD {perf['max_drawdown_pct']:>7.2f}%  Sharpe {perf['sharpe']:>6.3f}")

        # slippage scenarios
        print()
        print("SLIPPAGE SCENARIOS (variant F)")
        for name, slip in SLIPPAGE_SCENARIOS.items():
            sim = simulate(sched, proxy, costs=replace(costs, slippage_pct=slip),
                           cash_rate=a.cash_rate)
            perf = performance(sim, sched, proxy["close"], spec)
            print(f"  {name:<20} CAGR {perf['CAGR']:>7.2f}%  "
                  f"cost drag {perf['total_cost_drag_pct_yr']:>6.3f}%/yr")

        # cost breakdown on a representative move
        print()
        print("COST BREAKDOWN — Rs 1 crore book, R1 -> R2 (30-point exposure change)")
        notional = 10_000_000 * 0.30
        print(f"  traded notional        Rs {notional:>12,.0f}")
        for k, v in costs.breakdown(notional, is_buy=False).items():
            print(f"    {k:<20} Rs {v:>12,.0f}")
        print(f"  one-way total          Rs {costs.one_way(notional, is_buy=False):>12,.0f}")
        print(f"  exit + re-entry        Rs {costs.round_trip(notional):>12,.0f}")

        # reversal-window sensitivity
        print()
        print("WHIPSAW SENSITIVITY (variant F)")
        for w in [int(x) for x in a.reversal_windows.split(",")]:
            res = count_whipsaws(sched, proxy["close"], replace(spec, window_evals=w))
            print(f"  reversal window {w:>2} evals: {res['count']} whipsaws "
                  f"({res['per_year']}/yr)")

        if a.robustness:
            print()
            print("=" * 92)
            print("ROBUSTNESS GRID (variant F)")
            print("=" * 92)
            print(f"{'buffer':>8}{'confirm':>9}{'R4 eq':>8}{'CAGR':>9}{'maxDD':>9}"
                  f"{'turnover':>11}{'whip/yr':>9}")
            for bps in (100, 150, 200):
                for cd in (2, 3, 5):
                    for r4 in (0.0, 10.0):
                        g = replace(bt_cfg, buffer_bps=bps, confirm_days=cd,
                                    tier_exposure_pct={"R1": 100.0, "R2": 70.0,
                                                       "R3": 40.0, "R4": r4})
                        sch = replay(conn, g, vf, start=start, end=end)
                        sim = simulate(sch, proxy, costs=costs, cash_rate=a.cash_rate)
                        p = performance(sim, sch, proxy["close"], spec)
                        print(f"{bps/100:>7.1f}%{cd:>9}{r4:>7.0f}%{p['CAGR']:>9.2f}"
                              f"{p['max_drawdown_pct']:>9.2f}"
                              f"{p['annual_turnover_pct']:>11.1f}"
                              f"{p['whipsaws_per_year']:>9.2f}")
            print("\n  Reported, not ranked: a wider buffer is not automatically better "
                  "because it lowers whipsaw count.")

        if a.timelines:
            windows = [("2018 smallcap bear", "2018-01-01", "2019-03-31"),
                       ("Feb-Jun 2020", "2020-02-01", "2020-06-30"),
                       ("2021-22 top/correction", "2021-10-01", "2022-07-31")]
            print()
            print("=" * 92)
            print("EVENT TIMELINES (variant F)")
            print("=" * 92)
            for label, w0, w1 in windows:
                sl = sched.loc[str(w0):str(w1)]
                print(f"\n-- {label} ({w0} .. {w1}) --")
                if sl.empty:
                    print("   NO DATA in the cached window — index history does not "
                          "extend here. Re-run with --fetch --start earlier.")
                    continue
                print(f"{'session':<12}{'raw':>5}{'tier':>6}{'exp%':>7}{'H20':>6}{'H50':>6}"
                      f"{'H200':>7}{'M50/50':>9}{'M50/200':>9}{'breadth':>9}")
                for d, r in sl.iterrows():
                    print(f"{d.date().isoformat():<12}{r['raw_tier']:>5}{r['tier']:>6}"
                          f"{r['exposure']:>7.0f}{(r['H20'] or 0):>6.2f}"
                          f"{(r['H50'] or 0):>6.2f}{(r['H200'] or 0):>7.2f}"
                          f"{r['sentinel_50']:>9}{r['sentinel_200']:>9}"
                          f"{'' if pd.isna(r['breadth']) else format(r['breadth'], '.1f'):>9}")

        # subperiods
        print()
        print("=" * 92)
        print("SUBPERIODS (variant F vs A)")
        print("=" * 92)
        for label, s0 in (("2007-2009 GFC", "2007-01-01"), ("2009 onward", "2009-01-01"),
                          ("2015 onward", "2015-01-01"), ("2020 onward", "2020-01-01")):
            s0d = max(dt.date.fromisoformat(s0), start)
            if s0d >= end:
                continue
            sub = proxy.loc[str(s0d):str(end)]
            if len(sub) < 260:
                print(f"  {label:<18} INSUFFICIENT DATA ({len(sub)} sessions cached)")
                continue
            out = []
            for v in (next(x for x in VARIANTS if x.key == "A"), vf):
                sch = replay(conn, bt_cfg, v, start=s0d, end=end)
                sim = simulate(sch, sub, costs=costs, cash_rate=a.cash_rate)
                p = performance(sim, sch, sub["close"], spec)
                out.append(f"{v.key}: CAGR {p['CAGR']:>6.2f}% maxDD {p['max_drawdown_pct']:>7.2f}%")
            print(f"  {label:<18} " + "   |   ".join(out))

        if a.save:
            import os
            os.makedirs(os.path.dirname(a.save) or ".", exist_ok=True)
            covers = breadth_covers_window(conn, start, end)
            payload = {
                "generated_for": {"start": start.isoformat(), "end": end.isoformat()},
                "limitations": {
                    "portfolio_series": f"{cfg.momentum_sentinel} index — PROXY, not the live book",
                    "return_type": "PRICE RETURN (Kite indices carry no TRI)",
                    "breadth": ("sufficient coverage" if covers else
                                "INSUFFICIENT for this window; tiered variants run as a "
                                "PRICE-ONLY PROXY with breadth rules disabled"),
                    "execution": "next session OPEN after the weekly close (no lookahead)",
                    "book_weights": "configured defaults (no historical full-risk snapshots)",
                    "slippage": f"{a.slippage} ({SLIPPAGE_SCENARIOS[a.slippage]*100:.2f}%)",
                    "cash_rate": f"{a.cash_rate*100:.2f}%",
                },
                "variants": json.loads(table.to_json(orient="index")),
                "tier_distribution": {
                    k: {kk: float(vv) for kk, vv in (d["schedule"]["tier"]
                        .value_counts(normalize=True).mul(100).round(2).to_dict()).items()}
                    for k, d in detail.items()},
                "cost_breakdown": costs.breakdown(3_000_000, is_buy=False),
                "cost_one_way": costs.one_way(3_000_000, is_buy=False),
                "cost_round_trip": costs.round_trip(3_000_000),
                "whipsaw_sensitivity": {
                    str(w): count_whipsaws(sched, proxy["close"],
                                           replace(spec, window_evals=w))["per_year"]
                    for w in [int(x) for x in a.reversal_windows.split(",")]},
                "unavailable": {v.key: v.unavailable_reason for v in VARIANTS
                                if not v.available},
            }
            with open(a.save, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            print(f"\nsaved: {a.save}")

        print()
        print("Results are reported, not endorsed. Run this before enabling enforce mode "
              "and judge the thresholds yourself.")


if __name__ == "__main__":
    main()
