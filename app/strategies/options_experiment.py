"""Paper-only measurement of intraday defined-risk option selling.

WHAT HAPPENED TO THE OVERNIGHT ARM
This began as an A/B of intraday against overnight. The overnight arm was dropped on
16 Aug 2026 by a risk decision, not by a result: the system now refuses to hold any option
past the close, enforced in core/guards.py. An arm that can never be executed is not worth
the collection effort, so only the intraday arm remains.

That leaves a real question deliberately unanswered, and it should stay visible rather than
be quietly dropped from the file. Bhat (2024, J. Futures Markets) reports that on NIFTY the
variance risk premium is earned OVERNIGHT and given back intraday; Muravyev & Ni (2020,
JFE) find the same sign pattern on SPX. If that holds after 2026 costs and at retail fills,
an intraday short-premium strategy is on the wrong side of the clock — and this module can
now measure whether the intraday side pays, but not what is being forgone by refusing the
other. A negative intraday result is therefore consistent with the published finding and is
NOT evidence that the overnight trade would have worked, because gap risk is exactly what
the policy declined to take.

WHAT THIS MODULE IS AND IS NOT
It is bookkeeping and arithmetic. It pairs an ENTRY observation with a later EXIT
observation, prices both at executable quotes, applies the full cost stack, and reports
net P&L against the margin actually required. It does NOT decide anything, does not place
orders, and contains no code path that can.

It deliberately does not compute a "3x better overnight" theta ratio. That number comes
from removing 17.75 hours instead of 6.25 from Black-Scholes while holding spot and vol
fixed, which is a statement about the model's clock, not about the market: the same
continuous variance process is applied to the time removed either way. The gap
distribution, the open auction spread, and overnight margin are exactly what the ratio
omits and exactly what decides the trade. So this measures realised P&L instead.

FOUR THINGS IT REFUSES TO DO
- Price at mid or at a theoretical value. Entry sells at the bid and buys at the ask; exit
  reverses. A midpoint fill is the most common way a paper result fails to survive live.
- Report a total when any leg lacked a quote. An unquoted leg costed at zero manufactures
  edge.
- Collapse the two exits. The short-strike breach and the mark-loss stop are recorded
  independently, both as flags and as timestamps, so their separate contributions can be
  measured after the fact without re-running the experiment.
- Accept a variant that was not preregistered. Expected maximum Sharpe from ten trials on
  a worthless strategy is about 1.6 (Bailey, Borwein, Lopez de Prado & Zhu), so the number
  of variants tried is itself a result and must be fixed before the data arrives.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..analytics import db
from .options_costs import CostRates, Fill, option_costs

INTRADAY = "intraday"
# One arm. The tuple stays so `arm` remains a validated field rather than free text, and so
# a second arm (a different exit time, say) can be added without reshaping the table.
ARMS = (INTRADAY,)


class ExperimentError(ValueError):
    pass


# =====================================================================================
# preregistration
# =====================================================================================
@dataclass(frozen=True)
class Variant:
    """One preregistered configuration. The hash is over the SPEC only, so re-registering
    an identical spec is idempotent while any change produces a new variant."""

    name: str
    arm: str
    spec: Mapping[str, Any]
    note: str = ""

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ExperimentError(f"arm must be one of {ARMS}, got {self.arm!r}")
        if not self.name.strip():
            raise ExperimentError("a variant needs a name")

    @property
    def spec_hash(self) -> str:
        blob = json.dumps({"arm": self.arm, **dict(self.spec)}, sort_keys=True,
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def variant_id(self) -> str:
        return f"{self.arm}:{self.name}:{self.spec_hash[:8]}"


def register(conn, variant: Variant) -> str:
    """Record a variant before any data is collected. Idempotent on identical specs."""
    with db.transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO option_variants(variant_id, name, arm, spec_json,"
            " spec_hash, registered_at, note) VALUES(?,?,?,?,?,?,?)",
            (variant.variant_id, variant.name, variant.arm,
             json.dumps(dict(variant.spec), sort_keys=True), variant.spec_hash,
             dt.datetime.now().isoformat(timespec="seconds"), variant.note))
    return variant.variant_id


def variants(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM option_variants ORDER BY registered_at, variant_id")]


def trials_registered(conn) -> int:
    """N for the deflated-Sharpe / minimum-backtest-length calculation."""
    return conn.execute("SELECT COUNT(*) n FROM option_variants").fetchone()["n"]


# =====================================================================================
# executable pricing
# =====================================================================================
def executable_fill(side: str, bid: float | None, ask: float | None,
                    quantity: int, label: str = "") -> Fill:
    """Price a leg at the touch it must actually cross.

    A SELL hits the bid, a BUY lifts the ask. There is no configuration to soften this:
    assuming a mid fill is the single largest source of paper-to-live divergence, and a
    limit resting at mid is an order that may simply never fill.
    """
    s = side.upper()
    if s not in ("BUY", "SELL"):
        raise ExperimentError(f"side must be BUY or SELL, got {side!r}")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        raise ExperimentError(f"{label or s}: needs a two-sided quote, got bid={bid} ask={ask}")
    return Fill(side=s, price=ask if s == "BUY" else bid, quantity=quantity,
                bid=bid, ask=ask, label=label)


def reverse(fills: Sequence[Fill], quotes: Mapping[str, tuple[float, float]]) -> list[Fill]:
    """The closing fills for an open position, priced at the exit quotes.

    `quotes` maps a leg label to (bid, ask). A label with no quote raises rather than
    being skipped: a position closed on three of four legs is not a closed position.
    """
    out = []
    for f in fills:
        if f.label not in quotes:
            raise ExperimentError(f"no exit quote for leg {f.label!r}")
        bid, ask = quotes[f.label]
        out.append(executable_fill("BUY" if f.side == "SELL" else "SELL",
                                   bid, ask, f.quantity, f.label))
    return out


# =====================================================================================
# settlement
# =====================================================================================
@dataclass(frozen=True)
class Settlement:
    gross_pnl: float
    statutory_cost: float
    spread_cost: float | None
    total_cost: float | None
    net_pnl: float | None
    margin: float | None
    return_on_margin_pct: float | None
    max_loss: float | None
    net_vs_max_loss_pct: float | None
    cost_complete: bool
    entry_credit: float
    exit_debit: float

    def as_dict(self) -> dict:
        return {k: (None if v is None else (round(v, 4) if isinstance(v, float) else v))
                for k, v in self.__dict__.items()}


def _signed_cash(fills: Sequence[Fill]) -> float:
    """Cash received minus cash paid, ignoring charges."""
    return sum((f.turnover if f.side == "SELL" else -f.turnover) for f in fills)


def settle(entry: Sequence[Fill], exit_: Sequence[Fill], *,
           margin: float | None = None, max_loss: float | None = None,
           rates: CostRates | None = None, lot_size: int = 65) -> Settlement:
    """Net P&L of one closed arm, with costs applied to every leg of both sides."""
    if not entry or not exit_:
        raise ExperimentError("settlement needs both entry and exit fills")
    credit = _signed_cash(entry)
    debit = _signed_cash(exit_)
    gross = credit + debit

    costs = option_costs(list(entry) + list(exit_), rates=rates, lot_size=lot_size)
    net = None if costs.total is None else gross - costs.total
    rom = (net / margin * 100.0) if (net is not None and margin) else None
    vs_max = (net / abs(max_loss) * 100.0) if (net is not None and max_loss) else None
    return Settlement(
        gross_pnl=gross, statutory_cost=costs.statutory_total,
        spread_cost=costs.spread_cost, total_cost=costs.total, net_pnl=net,
        margin=margin, return_on_margin_pct=rom, max_loss=max_loss,
        net_vs_max_loss_pct=vs_max, cost_complete=costs.spread_complete,
        entry_credit=credit, exit_debit=debit)


# =====================================================================================
# lifecycle
# =====================================================================================
def open_arm(conn, *, variant_id: str, strategy: str, arm: str, expiry: dt.date,
             lots: int, lot_size: int, entry_fills: Sequence[Fill],
             entry_at: dt.datetime, entry_spot: float, dte_at_entry: float | None = None,
             max_loss: float | None = None, margin: float | None = None,
             margin_source: str = "", note: str = "") -> str:
    """Record an opened paper position. Returns the arm id."""
    row = conn.execute("SELECT 1 FROM option_variants WHERE variant_id=?",
                       (variant_id,)).fetchone()
    if row is None:
        raise ExperimentError(
            f"variant {variant_id!r} is not registered — preregister it before collecting "
            "data, otherwise the trial count that governs the deflated Sharpe is unknown")
    if arm not in ARMS:
        raise ExperimentError(f"arm must be one of {ARMS}")
    arm_id = uuid.uuid4().hex[:12]
    credit = _signed_cash(entry_fills)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO option_arms(arm_id, variant_id, strategy, arm, session_date,"
            " expiry, dte_at_entry, lots, lot_size, entry_at, entry_spot,"
            " entry_fills_json, entry_credit, max_loss, margin, margin_source, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (arm_id, variant_id, strategy, arm, entry_at.date().isoformat(),
             expiry.isoformat(), dte_at_entry, lots, lot_size,
             entry_at.isoformat(timespec="seconds"), entry_spot,
             json.dumps([_fill_json(f) for f in entry_fills]), credit, max_loss,
             margin, margin_source, note))
    return arm_id


def observe(conn, arm_id: str, *, breach: bool = False, mark_stop: bool = False,
            at: dt.datetime | None = None) -> None:
    """Record that an exit CONDITION was met, without closing the position.

    Both conditions are tracked independently and only the first occurrence of each is
    kept. This is what lets the short-strike breach exit and the mark-loss stop be
    evaluated separately afterwards — including the counterfactual where one is disabled —
    from a single run of the experiment rather than two.
    """
    stamp = (at or dt.datetime.now()).isoformat(timespec="seconds")
    with db.transaction(conn):
        if breach:
            conn.execute("UPDATE option_arms SET breach_seen=1,"
                         " breach_at=COALESCE(breach_at, ?) WHERE arm_id=?",
                         (stamp, arm_id))
        if mark_stop:
            conn.execute("UPDATE option_arms SET mark_stop_seen=1,"
                         " mark_stop_at=COALESCE(mark_stop_at, ?) WHERE arm_id=?",
                         (stamp, arm_id))


def close_arm(conn, arm_id: str, *, exit_fills: Sequence[Fill], exit_at: dt.datetime,
              exit_spot: float, exit_reason: str,
              rates: CostRates | None = None) -> dict:
    """Close an arm, settle it, and store the result."""
    row = conn.execute("SELECT * FROM option_arms WHERE arm_id=?", (arm_id,)).fetchone()
    if row is None:
        raise ExperimentError(f"unknown arm {arm_id!r}")
    if row["exit_at"]:
        raise ExperimentError(f"arm {arm_id!r} is already closed")

    entry_fills = [_fill_from_json(x) for x in json.loads(row["entry_fills_json"])]
    # The underlying's move across the hold. Named for what it is now that the hold is
    # always a single session: an intraday move, not an overnight gap.
    move = ((exit_spot / row["entry_spot"] - 1.0) * 100.0
            if row["entry_spot"] else None)

    s = settle(entry_fills, exit_fills, margin=row["margin"], max_loss=row["max_loss"],
               rates=rates, lot_size=row["lot_size"])
    with db.transaction(conn):
        conn.execute(
            "UPDATE option_arms SET exit_at=?, exit_spot=?, exit_fills_json=?,"
            " exit_reason=?, spot_move_pct=?, settled_json=? WHERE arm_id=?",
            (exit_at.isoformat(timespec="seconds"), exit_spot,
             json.dumps([_fill_json(f) for f in exit_fills]), exit_reason, move,
             json.dumps(s.as_dict()), arm_id))
    return {"arm_id": arm_id, "spot_move_pct": move, **s.as_dict()}


def _fill_json(f: Fill) -> dict:
    return {"side": f.side, "price": f.price, "quantity": f.quantity,
            "bid": f.bid, "ask": f.ask, "label": f.label}


def _fill_from_json(d: Mapping[str, Any]) -> Fill:
    return Fill(side=d["side"], price=float(d["price"]), quantity=int(d["quantity"]),
                bid=d.get("bid"), ask=d.get("ask"), label=d.get("label", ""))


def latest_arm(conn, *, arm: str, session_date: dt.date | None = None) -> dict | None:
    """The most recent arm of a kind, optionally restricted to one session."""
    sql = "SELECT * FROM option_arms WHERE arm=?"
    args: list = [arm]
    if session_date is not None:
        sql += " AND session_date=?"
        args.append(session_date.isoformat())
    row = conn.execute(sql + " ORDER BY entry_at DESC LIMIT 1", tuple(args)).fetchone()
    return dict(row) if row else None


def open_arms(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM option_arms WHERE exit_at IS NULL ORDER BY entry_at")]


def closed_arms(conn, *, variant_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM option_arms WHERE exit_at IS NOT NULL"
    args: tuple = ()
    if variant_id:
        sql += " AND variant_id=?"
        args = (variant_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY entry_at", args)]


# =====================================================================================
# reporting
# =====================================================================================
def _mean_sd(xs: Sequence[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, var ** 0.5


def sharpe_se(sharpe: float, n: int) -> float | None:
    """Standard error of a Sharpe estimate (Lo 2002, FAJ 58(4)).

        SE = sqrt((1 + SR^2 / 2) / T)

    The i.i.d. form. Option-selling P&L is autocorrelated, so on real data this
    UNDERSTATES the error — which is the direction that flatters the strategy, and the
    reason it is reported alongside n rather than on its own.
    """
    if n < 2:
        return None
    return ((1 + sharpe * sharpe / 2) / n) ** 0.5


def summarise(arms: Sequence[Mapping]) -> dict:
    """Everything point 6 asks for, per group, with no verdict attached."""
    rows = []
    for a in arms:
        s = json.loads(a["settled_json"] or "{}")
        if s.get("net_pnl") is None:
            continue          # an incomplete cost is not a result
        rows.append({**dict(a), **s})
    if not rows:
        return {"n": 0, "note": "no arms with a complete cost measurement"}

    net = [r["net_pnl"] for r in rows]
    gross = [r["gross_pnl"] for r in rows]
    m, sd = _mean_sd(net)
    sr = (m / sd) if sd else None
    wins = [x for x in net if x > 0]
    return {
        "n": len(rows),
        "gross_pnl_total": round(sum(gross), 2),
        "statutory_cost_total": round(sum(r["statutory_cost"] for r in rows), 2),
        "spread_cost_total": round(sum(r["spread_cost"] or 0.0 for r in rows), 2),
        "net_pnl_total": round(sum(net), 2),
        "net_pnl_mean": round(m, 2),
        "net_pnl_sd": round(sd, 2),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 1),
        "worst": round(min(net), 2),
        "best": round(max(net), 2),
        "sharpe_per_trade": None if sr is None else round(sr, 4),
        "sharpe_se": None if sr is None else (
            None if sharpe_se(sr, len(rows)) is None else round(sharpe_se(sr, len(rows)), 4)),
        # A cost ratio above 1 means frictions exceeded everything the position earned
        # before them — the single most useful number in this whole experiment.
        "cost_over_gross": (round(sum(r["total_cost"] for r in rows) / abs(sum(gross)), 3)
                            if sum(gross) else None),
        "mean_return_on_margin_pct": (
            round(sum(r["return_on_margin_pct"] for r in rows
                      if r.get("return_on_margin_pct") is not None)
                  / max(1, sum(1 for r in rows
                               if r.get("return_on_margin_pct") is not None)), 3)),
        "breach_rate_pct": round(
            sum(1 for r in rows if r.get("breach_seen")) / len(rows) * 100, 1),
        "mark_stop_rate_pct": round(
            sum(1 for r in rows if r.get("mark_stop_seen")) / len(rows) * 100, 1),
    }


def report(conn) -> dict:
    """What the intraday arm has done so far, overall and per variant.

    Deliberately returns no verdict. There is no longer a second arm to difference against,
    so the only question this can answer is whether intraday short premium pays its own
    costs — and that is a test against zero, which needs a sample this does not yet have.
    A function that printed "it works" on twelve observations would be worse than useless.

    `mean_vs_zero` is the honest form of that test: the mean net P&L against the standard
    error of the mean. It is reported without a conclusion attached.
    """
    closed = closed_arms(conn)
    by_arm = {a: [r for r in closed if r["arm"] == a] for a in ARMS}
    out = {
        "trials_registered": trials_registered(conn),
        "closed_arms": len(closed),
        "open_arms": len(open_arms(conn)),
        "by_arm": {a: summarise(rows) for a, rows in by_arm.items()},
        "by_variant": {v["variant_id"]: summarise(
            [r for r in closed if r["variant_id"] == v["variant_id"]])
            for v in variants(conn)},
    }
    a = out["by_arm"][INTRADAY]
    if a.get("n"):
        mean = a.get("net_pnl_mean") or 0.0
        sd = a.get("net_pnl_sd") or 0.0
        se = sd / (a["n"] ** 0.5) if sd and a["n"] else None
        out["mean_vs_zero"] = {
            "net_pnl_mean": round(mean, 2),
            "standard_error": round(se, 2) if se else None,
            "t": round(mean / se, 2) if se else None,
            "conclusive": bool(se and abs(mean / se) >= 2.0),
        }
    return out
