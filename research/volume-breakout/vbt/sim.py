"""Portfolio simulator for the volume-breakout signals — EOD, next-open entries, explicit exits.

Order of operations on session j (columns of the panel):
  1. exits at the open: pending EOD exits (EMA/time rules decided at j-1's close) fill at open_j;
     a stop gapped through fills at open_j; otherwise a stop touched by low_j fills at the stop.
  2. entries at the open: signals from j-1's close, ranked, subject to slots / cash / liquidity /
     the market gate as read at j-1's close; fill at open_j.
  3. the same session's low can take a fresh entry out (conservative: stop before target).
  4. at the close: trailing stops ratchet, targets and time stops fire at close_j, EOD rules
     queue an exit for j+1's open, equity is marked.
Money is float here (research); every fill is rounded to the exchange's 2-decimal tick.
"""
from __future__ import annotations

import dataclasses as dc
import math
from typing import Literal

import numpy as np
import pandas as pd

from .data import Panel
from .scan import Indicators


@dc.dataclass(frozen=True)
class Rules:
    name: str = "base"
    # entry
    entry: Literal["next_open", "limit_close"] = "next_open"  # limit_close: bid the signal close for k sessions
    entry_valid: int = 3                    # sessions a limit order stays working
    fill_through_pct: float = 0.0           # require low <= limit*(1-x/100) before assuming a fill
    max_gap_pct: float | None = None        # skip if open_j > signal close * (1 + x/100)
    min_gap_pct: float | None = None        # require the gap up (rare)
    rank: Literal["rvol", "change", "turnover", "close_pos", "none"] = "rvol"
    max_positions: int = 10
    max_new_per_day: int = 3
    sizing: Literal["equal", "risk"] = "equal"
    risk_pct: float = 1.0                   # of equity, risk sizing
    max_weight_pct: float = 12.5            # cap per position, % of equity
    max_pct_of_turnover: float = 1.0        # position <= x% of 20-day avg turnover
    min_trade_inr: float = 10_000
    # stop
    stop_mode: Literal["pct", "signal_low", "atr", "min_low_pct"] = "pct"
    stop_pct: float = 8.0
    atr_mult: float = 2.0
    max_stop_pct: float | None = None       # skip the trade if the initial stop is wider than this
    # trail / exits
    trail_mode: Literal["none", "chandelier", "ema", "nlow", "pct"] = "none"
    trail_atr_mult: float = 3.0
    trail_ema: int = 10
    trail_n: int = 10
    trail_pct: float = 10.0
    breakeven_r: float | None = None
    target_r: float | None = None
    target_pct: float | None = None
    partial_r: float | None = None
    partial_frac: float = 0.5
    max_hold: int | None = None             # sessions after entry; exit at that session's close
    exit_close_below_ema: int | None = None # queue exit for next open if close < EMA(n)
    exit_close_below_sma: int | None = None # 20 or 50
    # regime
    gate: Literal["none", "idx_10_20", "idx_above_50", "idx_above_200"] = "none"
    # costs
    cost_bps_side: float = 25.0             # STT/charges + slippage, each side
    initial_capital: float = 1_000_000.0


@dc.dataclass
class Position:
    row: int
    qty: int
    entry: float
    entry_col: int
    stop: float
    init_stop: float
    high_since: float
    partial_done: bool = False
    pending_exit: str | None = None
    realised: float = 0.0


@dc.dataclass
class Trade:
    symbol: str
    entry_date: np.datetime64
    exit_date: np.datetime64
    entry: float
    exit: float
    qty: int
    pnl: float
    ret_pct: float
    r_mult: float
    hold: int
    reason: str


def _tick(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def _round_tick(x: float) -> float:
    return round(x * 100) / 100


def index_gate(p: Panel, idx_dates: np.ndarray, idx_close: np.ndarray, mode: str) -> np.ndarray:
    """(D,) bool: may the book enter at session j+1's open, as read at session j's close."""
    if mode == "none":
        return np.ones(p.d, dtype=bool)
    s = pd.Series(idx_close, index=pd.to_datetime(idx_dates))
    s = s.reindex(pd.to_datetime(p.dates), method="ffill")
    if mode == "idx_10_20":
        ok = s.rolling(10).mean() > s.rolling(20).mean()
    elif mode == "idx_above_50":
        ok = s > s.rolling(50).mean()
    elif mode == "idx_above_200":
        ok = s > s.rolling(200).mean()
    else:
        raise ValueError(mode)
    return ok.fillna(False).to_numpy()


def run(p: Panel, ind: Indicators, sig: np.ndarray, rules: Rules, gate_ok: np.ndarray | None = None,
        start_col: int = 0) -> "Result":
    R = rules
    N, D = p.n, p.d
    if gate_ok is None:
        gate_ok = np.ones(D, dtype=bool)
    ema_tr = {10: ind.ema10, 21: ind.ema21}
    sma_tr = {20: ind.sma20, 50: ind.sma50}
    cost = R.cost_bps_side / 10_000
    cash = R.initial_capital
    positions: dict[int, Position] = {}
    working: dict[int, tuple[int, float]] = {}   # limit_close orders: row -> (signal col, limit)
    trades: list[Trade] = []
    equity = np.full(D, np.nan)
    n_open = np.zeros(D, dtype=np.int16)
    skipped = {"gap": 0, "slots": 0, "cash": 0, "liquidity": 0, "gate": 0, "stop_wide": 0, "no_bar": 0, "locked": 0}

    def close_pos(pos: Position, col: int, price: float, qty: int, reason: str) -> None:
        nonlocal cash
        price = _round_tick(price)
        proceeds = price * qty * (1 - cost)
        cash += proceeds
        pnl = proceeds - pos.entry * qty
        pos.realised += pnl
        if qty == pos.qty:
            total_pnl = pos.realised
            cost_basis = pos.entry * (pos.qty if not pos.partial_done else pos.qty / (1 - R.partial_frac))
            risk = pos.entry - pos.init_stop
            trades.append(Trade(
                symbol=str(p.symbols[pos.row]), entry_date=p.dates[pos.entry_col], exit_date=p.dates[col],
                entry=pos.entry, exit=price, qty=qty, pnl=total_pnl, ret_pct=total_pnl / cost_basis * 100,
                r_mult=(total_pnl / cost_basis) / (risk / pos.entry) if risk > 0 else float("nan"),
                hold=col - pos.entry_col, reason=reason,
            ))
            del positions[pos.row]
        else:
            pos.qty -= qty
            pos.partial_done = True

    for j in range(start_col, D):
        # ---- 1. exits at the open
        for row in list(positions):
            pos = positions[row]
            o, h, l, c = p.open[row, j], p.high[row, j], p.low[row, j], p.close[row, j]
            if not np.isfinite(o):
                # no bar: delisted / suspended. After 5 blank sessions, write it off at the last close.
                last = j - 1
                while last > pos.entry_col and not np.isfinite(p.close[row, last]):
                    last -= 1
                if j - last >= 5:
                    close_pos(pos, j, p.close[row, last] if np.isfinite(p.close[row, last]) else pos.stop, pos.qty, "no_bar")
                continue
            if pos.pending_exit:
                close_pos(pos, j, o, pos.qty, pos.pending_exit); continue
            if o <= pos.stop:
                close_pos(pos, j, o, pos.qty, "stop_gap"); continue
            if l <= pos.stop:
                close_pos(pos, j, pos.stop, pos.qty, "stop"); continue

        # ---- 2. entries at the open (signals from j-1; or working limit orders)
        if R.entry == "limit_close" and j >= 1:
            # new working orders from j-1's signals; stale ones expire
            for r in np.nonzero(sig[:, j - 1])[0]:
                if r not in positions and r not in working:
                    working[r] = (j - 1, p.close[r, j - 1])
            for r in [r for r, (c0, _) in working.items() if j - c0 > R.entry_valid]:
                skipped["gap"] += 1; del working[r]
        if j >= 1 and gate_ok[j - 1]:
            if R.entry == "limit_close":
                cand = [r for r in working if r not in positions]
            else:
                cand = np.nonzero(sig[:, j - 1])[0]
                cand = [r for r in cand if r not in positions]
            if cand:
                key = {"rvol": ind.rvol, "change": ind.change_pct, "turnover": ind.turnover, "close_pos": ind.close_pos}
                if R.rank != "none":
                    kc = [working[r][0] if R.entry == "limit_close" else j - 1 for r in cand]
                    k = key[R.rank][cand, kc]
                    cand = [cand[i] for i in np.argsort(-np.nan_to_num(k, nan=-1e9))]
                new = 0
                equity_now = cash + sum(pos.qty * (p.close[pos.row, j - 1] if np.isfinite(p.close[pos.row, j - 1]) else pos.entry) for pos in positions.values())
                for row in cand:
                    if new >= R.max_new_per_day:
                        skipped["slots"] += 1; break
                    if len(positions) >= R.max_positions:
                        skipped["slots"] += 1; break
                    o, h, l = p.open[row, j], p.high[row, j], p.low[row, j]
                    if not np.isfinite(o) or o <= 0:
                        skipped["no_bar"] += 1; continue
                    if o == h == l:  # locked limit at the open, no fill possible
                        skipped["locked"] += 1; continue
                    sig_col = working[row][0] if R.entry == "limit_close" else j - 1
                    sc = p.close[row, sig_col]
                    if R.entry == "limit_close":
                        if l > working[row][1] * (1 - R.fill_through_pct / 100):
                            continue            # not touched today, order keeps working
                        o = min(o, working[row][1])  # fill at the limit (or better at the open)
                        del working[row]
                    gap = (o / sc - 1) * 100
                    if R.max_gap_pct is not None and gap > R.max_gap_pct:
                        skipped["gap"] += 1; continue
                    if R.min_gap_pct is not None and gap < R.min_gap_pct:
                        skipped["gap"] += 1; continue
                    # initial stop
                    if R.stop_mode == "pct":
                        stop = o * (1 - R.stop_pct / 100)
                    elif R.stop_mode == "signal_low":
                        stop = p.low[row, sig_col]
                    elif R.stop_mode == "min_low_pct":
                        stop = max(p.low[row, sig_col], o * (1 - R.stop_pct / 100))
                    elif R.stop_mode == "atr":
                        a = ind.atr14[row, sig_col]
                        stop = o - R.atr_mult * a if np.isfinite(a) else o * (1 - R.stop_pct / 100)
                    else:
                        raise ValueError(R.stop_mode)
                    stop = _tick(stop)
                    if stop >= o:
                        stop = _tick(o * (1 - R.stop_pct / 100))
                    stop_dist_pct = (o - stop) / o * 100
                    if R.max_stop_pct is not None and stop_dist_pct > R.max_stop_pct:
                        skipped["stop_wide"] += 1; continue
                    # size
                    entry_px = o * (1 + cost)
                    if R.sizing == "equal":
                        target_val = equity_now / R.max_positions
                    else:
                        target_val = (R.risk_pct / 100 * equity_now) / (stop_dist_pct / 100)
                    target_val = min(target_val, R.max_weight_pct / 100 * equity_now)
                    liq = ind.turnover_sma20[row, j - 1]
                    if np.isfinite(liq):
                        target_val = min(target_val, R.max_pct_of_turnover / 100 * liq)
                    target_val = min(target_val, cash)
                    qty = int(target_val // entry_px)
                    if qty * entry_px < R.min_trade_inr:
                        if cash < R.min_trade_inr:
                            skipped["cash"] += 1
                        else:
                            skipped["liquidity"] += 1
                        continue
                    cash -= qty * entry_px
                    positions[row] = Position(row=row, qty=qty, entry=entry_px, entry_col=j, stop=stop,
                                              init_stop=stop, high_since=o)
                    new += 1
        elif j >= 1 and not gate_ok[j - 1]:
            skipped["gate"] += int(sig[:, j - 1].sum())

        # ---- 3. same-session stop on fresh entries; 4. close-of-day management
        for row in list(positions):
            pos = positions[row]
            o, h, l, c = p.open[row, j], p.high[row, j], p.low[row, j], p.close[row, j]
            if not np.isfinite(c):
                continue
            if pos.entry_col == j and l <= pos.stop:
                close_pos(pos, j, pos.stop if o > pos.stop else o, pos.qty, "stop_day0"); continue
            pos.high_since = max(pos.high_since, h)
            risk = pos.entry - pos.init_stop
            r_now = (c - pos.entry) / risk if risk > 0 else 0.0
            # partial at R (fills at the R level, assumed touched if high >= level)
            if R.partial_r is not None and not pos.partial_done and risk > 0:
                lvl = pos.entry + R.partial_r * risk
                if h >= lvl:
                    q = int(pos.qty * R.partial_frac)
                    if q > 0:
                        close_pos(pos, j, max(lvl, o) if o > lvl else lvl, q, "partial")
                        pos = positions.get(row)
                        if pos is None:
                            continue
            # full target
            if R.target_r is not None and risk > 0 and h >= pos.entry + R.target_r * risk:
                lvl = pos.entry + R.target_r * risk
                close_pos(pos, j, max(lvl, o), pos.qty, "target"); continue
            if R.target_pct is not None and h >= pos.entry * (1 + R.target_pct / 100):
                lvl = pos.entry * (1 + R.target_pct / 100)
                close_pos(pos, j, max(lvl, o), pos.qty, "target"); continue
            # time stop
            if R.max_hold is not None and j - pos.entry_col >= R.max_hold:
                close_pos(pos, j, c, pos.qty, "time"); continue
            # ratchets
            new_stop = pos.stop
            if R.breakeven_r is not None and r_now >= R.breakeven_r:
                new_stop = max(new_stop, pos.entry)
            if R.trail_mode == "chandelier":
                a = ind.atr14[row, j]
                if np.isfinite(a):
                    new_stop = max(new_stop, pos.high_since - R.trail_atr_mult * a)
            elif R.trail_mode == "ema":
                e = ema_tr[R.trail_ema][row, j]
                if np.isfinite(e):
                    new_stop = max(new_stop, e * 0.99)
            elif R.trail_mode == "nlow":
                lo = ind.lo10[row, j] if R.trail_n == 10 else np.nanmin(p.low[row, max(0, j - R.trail_n + 1): j + 1])
                if np.isfinite(lo):
                    new_stop = max(new_stop, lo)
            elif R.trail_mode == "pct":
                new_stop = max(new_stop, pos.high_since * (1 - R.trail_pct / 100))
            pos.stop = _tick(min(new_stop, c * 0.9999)) if new_stop < c else _tick(c * 0.999)
            # EOD rule → exit at next open
            if R.exit_close_below_ema is not None:
                e = ema_tr[R.exit_close_below_ema][row, j]
                if np.isfinite(e) and c < e:
                    pos.pending_exit = "ema_close"
            if R.exit_close_below_sma is not None:
                e = sma_tr[R.exit_close_below_sma][row, j]
                if np.isfinite(e) and c < e:
                    pos.pending_exit = "sma_close"

        equity[j] = cash + sum(pos.qty * (p.close[pos.row, j] if np.isfinite(p.close[pos.row, j]) else pos.entry) for pos in positions.values())
        n_open[j] = len(positions)

    # liquidate at the end for a clean equity read
    last = D - 1
    for row in list(positions):
        pos = positions[row]
        close_pos(pos, last, p.close[row, last] if np.isfinite(p.close[row, last]) else pos.entry, pos.qty, "end")
    equity[last] = cash
    return Result(rules=R, dates=p.dates[start_col:], equity=equity[start_col:], n_open=n_open[start_col:],
                  trades=trades, skipped=skipped)


@dc.dataclass
class Result:
    rules: Rules
    dates: np.ndarray
    equity: np.ndarray
    n_open: np.ndarray
    trades: list[Trade]
    skipped: dict[str, int]

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([dc.asdict(t) for t in self.trades])

    def metrics(self) -> dict[str, float]:
        e = pd.Series(self.equity, index=pd.to_datetime(self.dates)).dropna()
        if len(e) < 2:
            return {}
        years = (e.index[-1] - e.index[0]).days / 365.25
        total = e.iloc[-1] / e.iloc[0]
        cagr = total ** (1 / years) - 1 if years > 0 else float("nan")
        dd = e / e.cummax() - 1
        r = e.pct_change().dropna()
        sharpe = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else float("nan")
        t = self.trades_df()
        m = {
            "start": str(e.index[0].date()), "end": str(e.index[-1].date()), "years": round(years, 2),
            "final_equity": round(e.iloc[-1]), "total_return_pct": round((total - 1) * 100, 1),
            "cagr_pct": round(cagr * 100, 2), "max_dd_pct": round(dd.min() * 100, 2),
            "calmar": round(cagr / abs(dd.min()), 2) if dd.min() < 0 else float("nan"),
            "sharpe": round(sharpe, 2), "trades": len(t),
            "avg_open_positions": round(float(np.nanmean(self.n_open)), 2),
            "exposure_pct": round(float(np.nanmean(self.n_open) / self.rules.max_positions * 100), 1),
        }
        if len(t):
            w = t[t.pnl > 0]; l = t[t.pnl <= 0]
            m.update({
                "win_rate_pct": round(len(w) / len(t) * 100, 1),
                "avg_win_pct": round(w.ret_pct.mean(), 2) if len(w) else 0.0,
                "avg_loss_pct": round(l.ret_pct.mean(), 2) if len(l) else 0.0,
                "avg_ret_pct": round(t.ret_pct.mean(), 2),
                "median_ret_pct": round(t.ret_pct.median(), 2),
                "profit_factor": round(w.pnl.sum() / -l.pnl.sum(), 2) if len(l) and l.pnl.sum() < 0 else float("inf"),
                "avg_r": round(t.r_mult.mean(), 2), "avg_hold": round(t.hold.mean(), 1),
                "best_pct": round(t.ret_pct.max(), 1), "worst_pct": round(t.ret_pct.min(), 1),
            })
        return m

    def yearly(self) -> pd.DataFrame:
        e = pd.Series(self.equity, index=pd.to_datetime(self.dates)).dropna()
        y = e.resample("YE").last()
        first = pd.Series([e.iloc[0]], index=[e.index[0] - pd.Timedelta(days=1)])
        yy = pd.concat([first, y])
        ret = (yy.pct_change().dropna() * 100).round(1)
        ret.index = ret.index.year
        t = self.trades_df()
        if len(t):
            t["year"] = pd.to_datetime(t.exit_date).dt.year
            g = t.groupby("year").agg(trades=("pnl", "size"), win_rate=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
                                      avg_ret=("ret_pct", "mean"))
            out = pd.DataFrame({"return_pct": ret}).join(g)
        else:
            out = pd.DataFrame({"return_pct": ret})
        return out
