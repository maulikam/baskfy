"""Rebalance plan construction: scored scan + live holdings + cash -> concrete orders.

Moved from the desk to core at M15 (P3.2). Rules per
`kite-momentum-rebalancer/.claude/skills/momentum-rebalance/SKILL.md`. **Every rule below is
byte-for-byte the arithmetic the live account was rebalanced on** -- target weights, cluster caps,
breadth-driven cash bands, the minimum trade value, the cost ceiling, short-history half-sizing,
runner caps and vol-scaled stops.

Three things arrive as arguments now, and each was I/O or ambient state before:

* **`cfg`** -- the seventeen strategy constants. The desk's `config.py` reads the environment at
  import; core's first law is that it touches nothing (docs/04 §2).
* **`clusters`** -- the symbol-to-sector mapping. It used to be a `pd.read_csv` of
  `data/sectors.csv` inside the function. Reading a file is a boundary, so the read stays at the
  desk and the mapping comes in.
* **`tradeable`** -- the untouchable-instrument guard, and this one is a **required** parameter
  with no default on purpose.

That last point is worth stating plainly, because it is the one place this refactor could have made
the system less safe. The guard exists because `EXCLUDED_SYMBOLS` held `"SGBDE31III"` while the
actual holding was `"SGBDE31III-GB"`, so an exact-match set missed it and the planner proposed
**EXIT -392** on a Rs 60 lakh position it must never touch. Making the check injectable means a
caller could pass a permissive predicate -- so it has no default, which turns "forgot the guard"
from a silent behaviour change into a `TypeError` at the call site. The desk's wrapper always
passes `core.guards.assert_tradeable`, and
`kite-momentum-rebalancer/tests/test_basket_moved_to_core.py` asserts that it does.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Protocol

import numpy as np
import pandas as pd

from baskfy_core.costs import cost_pct, order_cost, plan_cost
from baskfy_core.rank_buffer import inside_hold_band
from baskfy_core.score import stop_from_vol
from baskfy_core.windows import subtract_months


class BasketConfig(Protocol):
    """The seventeen constants plan construction needs. The desk's `app.config` satisfies it."""

    CASH_BANDS: list[tuple[float, float]]
    CLUSTER_CAP: float
    EXCLUDED_SYMBOLS: object
    FULLY_INVESTED: bool
    HALF_SIZE_WEIGHT: float
    MAX_POS_VS_DAY_VALUE: float
    MAX_SINGLE_WEIGHT: float
    MAX_TRADE_COST_PCT: float
    MIN_POSITION_WEIGHT: float
    MIN_TRADE_PCT: float
    MIN_TRADE_VALUE: float
    PARABOLIC_RSI: float
    REPLACEMENT_EDGE: float
    RETENTION_BUFFER: int
    RUNNER_MAX_VALUE: float
    TARGET_POSITIONS: tuple[int, int]
    TRIM_TO_WEIGHT: float
    STOP_VOL_MULT: float
    STOP_MIN: float
    STOP_MAX: float


def cash_pct_for(breadth20: float, cfg: BasketConfig) -> float:
    """The cash band breadth puts the book in. Percent of the sleeve held back."""
    if cfg.FULLY_INVESTED:
        return 0.0
    for min_b, cash in cfg.CASH_BANDS:
        if breadth20 >= min_b:
            return cash
    return cfg.CASH_BANDS[-1][1]


def build_plan(
    scored: pd.DataFrame,
    holdings: list[dict],
    cash: float,
    *,
    cfg: BasketConfig,
    tradeable: Callable[[str], bool],
    clusters: Mapping[str, str] | None = None,
    live_prices: dict[str, float] | None = None,
    breadth_override: float | None = None,
) -> dict:
    """holdings: [{symbol, quantity(total incl pledged+t1), pledged_qty, last_price, average_price}]

    breadth_override replaces the scan-derived 20-DMA breadth that drives the cash band. See
    the note at its use below; when it is None nothing about this function changes.

    live_prices maps symbol -> last traded price and is REQUIRED for any name not already
    held. A new position was previously sized and limit-priced from the scan CSV's close
    while held names used live LTP, so every entry was priced one gap stale at best: the
    quantity is capital*weight/price, and a price that is wrong by a factor is a position
    size wrong by that factor. A candidate with no live price is dropped from the plan and
    reported in `unpriced` rather than sized from a stale figure.
    """
    live_prices = {k: float(v) for k, v in (live_prices or {}).items() if v and v > 0}
    idx = scored.set_index("symbol")
    # THE GUARD, not an exact-match set. cfg.EXCLUDED_SYMBOLS holds "SGBDE31III" while the
    # actual holding is "SGBDE31III-GB", so the set missed it and the planner proposed
    # EXIT -392 — selling a Rs 60 lakh position it must never touch. The route happened to
    # filter SGB* itself, so /analyze was safe; every other caller of build_plan was not,
    # and a plan is not made safe by the layer that happens to execute it.
    #
    # guards.assert_tradeable is prefix- and series-aware and is the same check the gateway
    # applies, so a plan can no longer propose something the order path would refuse. It is
    # INJECTED here rather than imported, because core cannot reach the desk -- and it is a
    # REQUIRED parameter with no default, so the guard cannot be forgotten by omission. The
    # desk's wrapper always passes the real one, and a test asserts that it does.
    _tradeable = tradeable

    hold = {h["symbol"]: h for h in holdings if _tradeable(h["symbol"])}
    excluded = [h for h in holdings if not _tradeable(h["symbol"])]
    clusters = dict(clusters or {})

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
    unpriced = sorted(
        {
            str(r.symbol)
            for _, r in scored.iterrows()
            if not r["reject"] and str(r.symbol) not in hold and price_of(str(r.symbol)) is None
        }
    )

    book_val = sum(h["quantity"] * h["last_price"] for h in hold.values())
    capital = book_val + cash
    # M14 §1: the pipeline computes this same figure over a named index universe, and when the
    # scan's population is that universe the two agree exactly (68.6347% on 2026-08-18, both
    # sides). The caller decides which to use, because only the caller knows whether the scan it
    # was handed covers the universe the pipeline measured. `None` keeps the original behaviour.
    breadth = (
        float((scored.close > scored.ma_20).mean() * 100)
        if breadth_override is None
        else float(breadth_override)
    )
    cash_pct = cash_pct_for(breadth, cfg)
    invest = capital * (1 - cash_pct / 100)

    # --- classify holdings ---
    runners, exits, keepers = {}, [], {}
    for s, h in hold.items():
        row = idx.loc[s] if s in idx.index else None
        if row is None:
            exits.append((s, "not in scan universe"))
        elif row["reject"]:
            if h["quantity"] * h["last_price"] > cfg.RUNNER_MAX_VALUE:
                runners[s] = ("filter-rejected → capped runner", cfg.RUNNER_MAX_VALUE)
            else:
                runners[s] = ("filter-rejected → hold as runner", h["quantity"] * h["last_price"])
        elif float(row["rsi_one_month"]) >= cfg.PARABOLIC_RSI:
            runners[s] = (
                f"parabolic RSI {row['rsi_one_month']:.0f} → trim to runner",
                capital * cfg.TRIM_TO_WEIGHT / 100,
            )
        else:
            keepers[s] = float(row["SCORE"])

    # --- selection ---
    # A name with no live price cannot be sized, so it is not eligible to be selected.
    elig = scored[(scored.reject == "") & (~scored.symbol.isin(unpriced))].sort_values("rank")
    n_lo, n_hi = cfg.TARGET_POSITIONS
    n = int(np.clip(len(elig[elig.SCORE.quantile(0.85) <= elig.SCORE]) + len(keepers), n_lo, n_hi))
    selected: dict[str, float] = {}
    for s, sc in keepers.items():
        r = int(idx.loc[s, "rank"])
        # THE SAME RULE THE SCREENER'S TRACKER USES, and now literally the same function.
        # This read `r <= n + RETENTION_BUFFER` and the screener's `plan_rebalance` had the
        # identical comparison written out separately; a rule embodied twice is one that drifts.
        # The desk's version is this band PLUS the replacement-edge hurdle below, which is why
        # it keeps a name the tracker would exit (M17, docs/03 §3a).
        if inside_hold_band(r, n, cfg.RETENTION_BUFFER):
            selected[s] = sc
        else:
            challenger = elig.iloc[len(selected)] if len(selected) < len(elig) else None
            if challenger is not None and challenger.SCORE - sc >= cfg.REPLACEMENT_EDGE:
                exits.append(
                    (
                        s,
                        f"rank {r} beyond N+{cfg.RETENTION_BUFFER}, "
                        f"replaced (+{challenger.SCORE - sc:.0f} pts)",
                    )
                )
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

    # An empty selection is a legitimate answer, not an error: a market where every candidate sits
    # below both its 50- and 200-day averages should produce no buys. It became reachable the
    # moment M13 allowed a generated scan, whose universe is smaller than an upload's whenever
    # unadjusted corporate actions trip `far_from_high`. Before this guard the next line raised
    # ZeroDivisionError — on the desk, a 500 in the middle of a rebalance.
    w = (
        {}
        if tot <= 0
        else {
            s: min(
                sc / tot * 100 * (invest / capital) * (pool / invest) / (invest / capital),
                cfg.MAX_SINGLE_WEIGHT,
            )
            for s, sc in raw.items()
        }
    )
    # normalise to pool, enforce min, then half-size short-history listings.
    # Half-size is AFTER the final scale on purpose: renormalising afterwards would push a
    # capped name back above HALF_SIZE_WEIGHT (AF 3.9). Residual stays cash.
    scale = (pool / capital * 100) / sum(w.values()) if w else 0.0
    w = {s: v * scale for s, v in w.items()}
    for s in list(w):
        w[s] = max(w[s], cfg.MIN_POSITION_WEIGHT) if w[s] > cfg.MIN_POSITION_WEIGHT / 2 else w[s]
    scale = (pool / capital * 100) / sum(w.values()) if w else 0.0
    w = {s: round(v * scale, 2) for s, v in w.items()}
    as_of_raw = scored["date"].iloc[0] if len(scored) else None
    as_of = pd.Timestamp(as_of_raw).date() if as_of_raw is not None else None
    half_cutoff = subtract_months(as_of, 18) if as_of is not None else None
    has_listed = "listed_on" in idx.columns
    for s in list(w):
        if half_cutoff is None or not has_listed or s not in idx.index:
            continue
        raw_listed = idx.loc[s, "listed_on"]
        if raw_listed is None or (isinstance(raw_listed, float) and np.isnan(raw_listed)):
            continue
        listed = pd.Timestamp(raw_listed).date()
        if listed > half_cutoff:
            w[s] = min(w[s], cfg.HALF_SIZE_WEIGHT)

    cluster_warn = []
    if clusters:
        agg: dict[str, float] = {}
        for s, v in w.items():
            agg[clusters.get(s, "other")] = agg.get(clusters.get(s, "other"), 0) + v
        cluster_warn = [
            f"{k} {v:.1f}% > cap {cfg.CLUSTER_CAP}%"
            for k, v in agg.items()
            if v > cfg.CLUSTER_CAP and k != "other"
        ]

    # --- orders ---
    orders = []
    for s, weight in sorted(w.items(), key=lambda x: -x[1]):
        px = price_of(s)
        if px is None:  # unreachable: filtered above, guarded anyway
            continue
        vol = float(idx.loc[s, "volatility_one_year"]) if s in idx.index else 0.36
        tgt_qty = round(capital * weight / 100 / px)
        cur_qty = hold[s]["quantity"] if s in hold else 0
        dq = tgt_qty - cur_qty
        day_val = float(idx.loc[s, "median_volume_one_year"]) if s in idx.index else np.inf
        if tgt_qty * px > day_val * cfg.MAX_POS_VS_DAY_VALUE:
            tgt_qty = int(day_val * cfg.MAX_POS_VS_DAY_VALUE / px)
            dq = tgt_qty - cur_qty
        orders.append(
            dict(
                symbol=s,
                action="BUY"
                if cur_qty == 0
                else ("ADD" if dq > 0 else "HOLD" if dq == 0 else "TRIM"),
                qty_now=cur_qty,
                delta=dq,
                qty_final=tgt_qty,
                ref_price=px,
                weight=weight,
                value=round(tgt_qty * px),
                stop=stop_from_vol(px, vol, cfg),
                pledged=hold.get(s, {}).get("pledged_qty", 0),
                rank=int(idx.loc[s, "rank"]) if s in idx.index else None,
                score=float(idx.loc[s, "SCORE"]) if s in idx.index else None,
            )
        )
    for s, (reason, val) in runners.items():
        h = hold[s]
        px = h["last_price"]
        tgt_qty = min(h["quantity"], int(val / px))
        orders.append(
            dict(
                symbol=s,
                action="TRIM" if tgt_qty < h["quantity"] else "HOLD",
                qty_now=h["quantity"],
                delta=tgt_qty - h["quantity"],
                qty_final=tgt_qty,
                ref_price=px,
                weight=round(tgt_qty * px / capital * 100, 2),
                value=round(tgt_qty * px),
                stop=stop_from_vol(
                    px, float(idx.loc[s, "volatility_one_year"]) if s in idx.index else 0.4, cfg
                ),
                pledged=h.get("pledged_qty", 0),
                rank=None,
                score=None,
                note=reason,
            )
        )
    for s, reason in exits:
        h = hold[s]
        orders.append(
            dict(
                symbol=s,
                action="EXIT",
                qty_now=h["quantity"],
                delta=-h["quantity"],
                qty_final=0,
                ref_price=h["last_price"],
                weight=0,
                value=0,
                stop=None,
                pledged=h.get("pledged_qty", 0),
                rank=None,
                score=None,
                note=reason,
            )
        )

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
        cpct = cost_pct(val, side)
        o["est_cost"] = round(order_cost(val, side).total, 2)
        o["est_cost_pct"] = round(cpct, 3)
        if val < cfg.MIN_TRADE_VALUE or pct < cfg.MIN_TRADE_PCT or cpct > cfg.MAX_TRADE_COST_PCT:
            skipped = (
                "would move {:.2f}% of the position for Rs {:,.0f} at a cost of "
                "Rs {:,.0f} ({:.2f}% of the trade); below the no-trade band "
                "({:.0f}% / Rs {:,.0f} / max {:.2f}% cost)"
            ).format(
                pct,
                val,
                o["est_cost"],
                cpct,
                cfg.MIN_TRADE_PCT,
                cfg.MIN_TRADE_VALUE,
                cfg.MAX_TRADE_COST_PCT,
            )
            o["note"] = (o.get("note") + " · " if o.get("note") else "") + skipped
            o["skipped_delta"] = o["delta"]
            o["action"], o["delta"], o["qty_final"] = "HOLD", 0, o["qty_now"]
            o["value"] = round(o["qty_now"] * o["ref_price"])
            o["est_cost"], o["est_cost_pct"] = 0.0, 0.0

    buys = sum(o["delta"] * o["ref_price"] for o in orders if o["delta"] > 0)
    sells = sum(-o["delta"] * o["ref_price"] for o in orders if o["delta"] < 0)
    pledged_sells = [
        o["symbol"] for o in orders if o["delta"] < 0 and o["pledged"] > 0
    ]  # info: collateral margin will reduce

    return dict(
        plan_id=uuid.uuid4().hex[:12],
        capital=round(capital),
        book_value=round(book_val),
        cash=round(cash),
        breadth_above_20dma=round(breadth, 1),
        cash_target_pct=cash_pct,
        buys_value=round(buys),
        sells_value=round(sells),
        net_cash_use=round(buys - sells),
        cluster_warnings=cluster_warn,
        pledged_sells_margin_note=pledged_sells,
        excluded=[h["symbol"] for h in excluded],
        unpriced=unpriced,
        est_costs=plan_cost(orders),
        orders=orders,
    )
