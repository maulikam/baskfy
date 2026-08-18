"""Build a rebalance plan: scored scan + live holdings + cash → concrete orders.
Rules per .claude/skills/momentum-rebalance/SKILL.md."""
from __future__ import annotations
import os
import uuid
import pandas as pd
import numpy as np
from . import config as C
from . import costs
from .scoring import stop_from_vol

SECTORS_FILE = "data/sectors.csv"   # optional: symbol,cluster


def _cash_pct(breadth20: float) -> float:
    if C.FULLY_INVESTED:
        return 0.0
    for min_b, cash in C.CASH_BANDS:
        if breadth20 >= min_b:
            return cash
    return C.CASH_BANDS[-1][1]


def _load_clusters() -> dict:
    if os.path.exists(SECTORS_FILE):
        s = pd.read_csv(SECTORS_FILE)
        return dict(zip(s.symbol, s.cluster))
    return {}


def build_plan(scored: pd.DataFrame, holdings: list[dict], cash: float,
               live_prices: dict[str, float] | None = None) -> dict:
    """holdings: [{symbol, quantity(total incl pledged+t1), pledged_qty, last_price, average_price}]

    live_prices maps symbol -> last traded price and is REQUIRED for any name not already
    held. A new position was previously sized and limit-priced from the scan CSV's close
    while held names used live LTP, so every entry was priced one gap stale at best: the
    quantity is capital*weight/price, and a price that is wrong by a factor is a position
    size wrong by that factor. A candidate with no live price is dropped from the plan and
    reported in `unpriced` rather than sized from a stale figure.
    """
    live_prices = {k: float(v) for k, v in (live_prices or {}).items() if v and v > 0}
    idx = scored.set_index("symbol")
    # THE GUARD, not an exact-match set. C.EXCLUDED_SYMBOLS holds "SGBDE31III" while the
    # actual holding is "SGBDE31III-GB", so the set missed it and the planner proposed
    # EXIT -392 — selling a Rs 60 lakh position it must never touch. The route happened to
    # filter SGB* itself, so /analyze was safe; every other caller of build_plan was not,
    # and a plan is not made safe by the layer that happens to execute it.
    #
    # guards.assert_tradeable is prefix- and series-aware and is the same check the gateway
    # applies, so a plan can no longer propose something the order path would refuse.
    from .core.guards import UntouchableInstrumentError, assert_tradeable

    def _tradeable(sym: str) -> bool:
        try:
            assert_tradeable(sym)
            return True
        except UntouchableInstrumentError:
            return False

    hold = {h["symbol"]: h for h in holdings if _tradeable(h["symbol"])}
    excluded = [h for h in holdings if not _tradeable(h["symbol"])]
    clusters = _load_clusters()

    def price_of(s: str) -> float | None:
        """Live price only. Held names carry an LTP refreshed by the caller."""
        p = live_prices.get(s)
        if p:
            return p
        h = hold.get(s)
        return float(h["last_price"]) if h and h.get("last_price") else None

    # Only ELIGIBLE names matter here: a filter-rejected symbol is never bought, so its
    # lack of a live price is not a problem to report. Held names are covered by the
    # caller's LTP refresh and fall back to their stored last_price.
    #
    # `scored` itself is deliberately NOT filtered: breadth is measured across the whole
    # scan, and removing rows from it moved breadth from 83.9% to 100% — which feeds the
    # cash target and the regime overlay. Unpriced names are excluded at SELECTION only.
    unpriced = sorted({str(r.symbol) for _, r in scored.iterrows()
                       if not r["reject"] and str(r.symbol) not in hold
                       and price_of(str(r.symbol)) is None})

    book_val = sum(h["quantity"] * h["last_price"] for h in hold.values())
    capital = book_val + cash
    breadth = float((scored.close > scored.ma_20).mean() * 100)
    cash_pct = _cash_pct(breadth)
    invest = capital * (1 - cash_pct / 100)

    # --- classify holdings ---
    runners, exits, keepers = {}, [], {}
    for s, h in hold.items():
        row = idx.loc[s] if s in idx.index else None
        if row is None:
            exits.append((s, "not in scan universe"))
        elif row["reject"]:
            if h["quantity"] * h["last_price"] > C.RUNNER_MAX_VALUE:
                runners[s] = ("filter-rejected → capped runner", C.RUNNER_MAX_VALUE)
            else:
                runners[s] = ("filter-rejected → hold as runner", h["quantity"] * h["last_price"])
        elif float(row["rsi_one_month"]) >= C.PARABOLIC_RSI:
            runners[s] = (f"parabolic RSI {row['rsi_one_month']:.0f} → trim to runner",
                          capital * C.TRIM_TO_WEIGHT / 100)
        else:
            keepers[s] = float(row["SCORE"])

    # --- selection ---
    # A name with no live price cannot be sized, so it is not eligible to be selected.
    elig = scored[(scored.reject == "") & (~scored.symbol.isin(unpriced))].sort_values("rank")
    n_lo, n_hi = C.TARGET_POSITIONS
    n = int(np.clip(len(elig[elig.SCORE >= elig.SCORE.quantile(.85)]) + len(keepers), n_lo, n_hi))
    cutoff_rank = n + C.RETENTION_BUFFER

    selected: dict[str, float] = {}
    for s, sc in keepers.items():
        r = int(idx.loc[s, "rank"])
        if r <= cutoff_rank:
            selected[s] = sc
        else:
            challenger = elig.iloc[len(selected)] if len(selected) < len(elig) else None
            if challenger is not None and challenger.SCORE - sc >= C.REPLACEMENT_EDGE:
                exits.append((s, f"rank {r} beyond N+{C.RETENTION_BUFFER}, "
                                 f"replaced (+{challenger.SCORE - sc:.0f} pts)"))
            else:
                selected[s] = sc  # retained: challenger edge insufficient
    for _, row in elig.iterrows():
        if len(selected) >= n:
            break
        if row.symbol not in selected and row.symbol not in runners:
            selected[row.symbol] = float(row.SCORE)

    # --- weights: score-proportional, capped, cluster-checked ---
    runner_val = sum(v for _, v in runners.values())
    pool = invest - runner_val
    raw = {s: sc for s, sc in selected.items()}
    tot = sum(raw.values())
    w = {s: min(sc / tot * 100 * (invest / capital) * (pool / invest) / (invest / capital),
                C.MAX_SINGLE_WEIGHT) for s, sc in raw.items()}
    # normalise to pool, enforce min & half-size
    scale = (pool / capital * 100) / sum(w.values())
    w = {s: v * scale for s, v in w.items()}
    for s in list(w):
        listed_hist_ok = True  # extend: derive from listing date feed if available
        if not listed_hist_ok:
            w[s] = min(w[s], C.HALF_SIZE_WEIGHT)
        w[s] = max(w[s], C.MIN_POSITION_WEIGHT) if w[s] > C.MIN_POSITION_WEIGHT / 2 else w[s]
    scale = (pool / capital * 100) / sum(w.values())
    w = {s: round(v * scale, 2) for s, v in w.items()}

    cluster_warn = []
    if clusters:
        agg: dict[str, float] = {}
        for s, v in w.items():
            agg[clusters.get(s, "other")] = agg.get(clusters.get(s, "other"), 0) + v
        cluster_warn = [f"{k} {v:.1f}% > cap {C.CLUSTER_CAP}%" for k, v in agg.items()
                        if v > C.CLUSTER_CAP and k != "other"]

    # --- orders ---
    orders = []
    for s, weight in sorted(w.items(), key=lambda x: -x[1]):
        px = price_of(s)
        if px is None:                      # unreachable: filtered above, guarded anyway
            continue
        vol = float(idx.loc[s, "volatility_one_year"]) if s in idx.index else 0.36
        tgt_qty = round(capital * weight / 100 / px)
        cur_qty = hold[s]["quantity"] if s in hold else 0
        dq = tgt_qty - cur_qty
        day_val = float(idx.loc[s, "median_volume_one_year"]) if s in idx.index else np.inf
        if tgt_qty * px > day_val * C.MAX_POS_VS_DAY_VALUE:
            tgt_qty = int(day_val * C.MAX_POS_VS_DAY_VALUE / px)
            dq = tgt_qty - cur_qty
        orders.append(dict(symbol=s, action="BUY" if cur_qty == 0 else ("ADD" if dq > 0 else "HOLD" if dq == 0 else "TRIM"),
                           qty_now=cur_qty, delta=dq, qty_final=tgt_qty, ref_price=px,
                           weight=weight, value=round(tgt_qty * px),
                           stop=stop_from_vol(px, vol),
                           pledged=hold.get(s, {}).get("pledged_qty", 0),
                           rank=int(idx.loc[s, "rank"]) if s in idx.index else None,
                           score=float(idx.loc[s, "SCORE"]) if s in idx.index else None))
    for s, (reason, val) in runners.items():
        h = hold[s]; px = h["last_price"]
        tgt_qty = min(h["quantity"], int(val / px))
        orders.append(dict(symbol=s, action="TRIM" if tgt_qty < h["quantity"] else "HOLD",
                           qty_now=h["quantity"], delta=tgt_qty - h["quantity"], qty_final=tgt_qty,
                           ref_price=px, weight=round(tgt_qty * px / capital * 100, 2),
                           value=round(tgt_qty * px),
                           stop=stop_from_vol(px, float(idx.loc[s, "volatility_one_year"]) if s in idx.index else 0.4),
                           pledged=h.get("pledged_qty", 0), rank=None, score=None, note=reason))
    for s, reason in exits:
        h = hold[s]
        orders.append(dict(symbol=s, action="EXIT", qty_now=h["quantity"], delta=-h["quantity"],
                           qty_final=0, ref_price=h["last_price"], weight=0,
                           value=0, stop=None, pledged=h.get("pledged_qty", 0),
                           rank=None, score=None, note=reason))

    # --- no-trade band -------------------------------------------------------------------
    # Applied AFTER sizing, so the plan still reports the target it wanted; only the order
    # is suppressed. A full exit is exempt: the band exists to avoid paying fees for
    # nothing, not to keep the book in a name the strategy has rejected.
    for o in orders:
        if o["delta"] == 0 or o["qty_final"] == 0:
            continue
        val = abs(o["delta"]) * o["ref_price"]
        pct = (abs(o["delta"]) / o["qty_now"] * 100.0) if o["qty_now"] else 100.0
        side = "BUY" if o["delta"] > 0 else "SELL"
        cpct = costs.cost_pct(val, side)
        o["est_cost"] = round(costs.order_cost(val, side).total, 2)
        o["est_cost_pct"] = round(cpct, 3)
        if val < C.MIN_TRADE_VALUE or pct < C.MIN_TRADE_PCT or cpct > C.MAX_TRADE_COST_PCT:
            skipped = ("would move {:.2f}% of the position for Rs {:,.0f} at a cost of "
                       "Rs {:,.0f} ({:.2f}% of the trade); below the no-trade band "
                       "({:.0f}% / Rs {:,.0f} / max {:.2f}% cost)").format(
                           pct, val, o["est_cost"], cpct, C.MIN_TRADE_PCT,
                           C.MIN_TRADE_VALUE, C.MAX_TRADE_COST_PCT)
            o["note"] = (o.get("note") + " · " if o.get("note") else "") + skipped
            o["skipped_delta"] = o["delta"]
            o["action"], o["delta"], o["qty_final"] = "HOLD", 0, o["qty_now"]
            o["value"] = round(o["qty_now"] * o["ref_price"])
            o["est_cost"], o["est_cost_pct"] = 0.0, 0.0

    buys = sum(o["delta"] * o["ref_price"] for o in orders if o["delta"] > 0)
    sells = sum(-o["delta"] * o["ref_price"] for o in orders if o["delta"] < 0)
    pledged_sells = [o["symbol"] for o in orders if o["delta"] < 0 and o["pledged"] > 0]  # info: collateral margin will reduce

    return dict(plan_id=uuid.uuid4().hex[:12], capital=round(capital), book_value=round(book_val),
                cash=round(cash), breadth_above_20dma=round(breadth, 1), cash_target_pct=cash_pct,
                buys_value=round(buys), sells_value=round(sells),
                net_cash_use=round(buys - sells), cluster_warnings=cluster_warn,
                pledged_sells_margin_note=pledged_sells,
                excluded=[h["symbol"] for h in excluded], unpriced=unpriced,
                est_costs=costs.plan_cost(orders),
                orders=orders)
