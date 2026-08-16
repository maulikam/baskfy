"""Pure decision functions. Snapshot in, Decision out.

NO I/O. NO CLOCK READS. NO BROKER CALLS. Every input arrives as an argument, including the
current time. That is what lets the same rules drive paper, live and backtest without any
chance of the three diverging, and it is why every threshold in the strategy can be tested
by constructing a state rather than by running a session.

V1 implements entry gating and exits only. The two adjustment levers are V2/V3; their
decision types exist here so the state machine is complete, but nothing emits them yet.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Action(str, Enum):
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    SKIP = "SKIP"
    ROLL_WINNER = "ROLL_WINNER"     # V2
    ROLL_LOSER = "ROLL_LOSER"       # V3


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    code: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"action": self.action.value, "code": self.code, "reason": self.reason,
                **({"detail": dict(self.detail)} if self.detail else {})}


@dataclass(frozen=True)
class Snapshot:
    """Everything a decision may depend on, at one instant."""
    now: dt.datetime
    spot: float
    asp: float | None
    marks: Mapping[str, float]              # symbol -> liquidation price
    straddle_open: float | None = None
    straddle_prev_close: float | None = None
    prev_close_spot: float | None = None
    open_spot: float | None = None
    feed_age_seconds: float = 0.0
    secondary_broker_ok: bool = True
    event_today: str = ""
    consecutive_loss_days: int = 0
    at_min_lots: bool = False
    # --- V2 ---------------------------------------------------------------------------
    vwaps: Mapping[str, float] = field(default_factory=dict)
    resistance: float | None = None
    support: float | None = None


# =====================================================================================
# day vetoes — section 4.4
# =====================================================================================
def day_vetoes(snap: Snapshot, cfg: dict, *, reference_band: Mapping | None = None,
               dte_bucket: str = "") -> list[str]:
    """Every reason to skip the day. Returns all of them, not the first.

    Reporting only the first veto makes a skipped day look like a single marginal call
    when it may have failed four gates at once, which matters when deciding whether the
    gates are too tight.
    """
    g = cfg["gates"]
    out: list[str] = []

    if snap.event_today:
        out.append(f"event day: {snap.event_today}")

    if snap.open_spot is not None and snap.prev_close_spot:
        gap = abs(snap.open_spot - snap.prev_close_spot) / snap.prev_close_spot
        if gap > float(g["gap_veto_pct"]):
            out.append(f"gap {gap:.2%} > {float(g['gap_veto_pct']):.2%}")

    band = (reference_band or {}).get(dte_bucket) if dte_bucket else None
    if snap.asp is None:
        out.append("no ATM straddle price — the chain is incomplete")
    elif band:
        lo = float(band["low"]) * float(g["iv_low_multiplier"])
        hi = float(band["high"]) * float(g["iv_high_multiplier"])
        if snap.asp < lo:
            out.append(f"ASP {snap.asp:.1f} below {lo:.1f} — nothing worth selling")
        elif snap.asp > hi:
            out.append(f"ASP {snap.asp:.1f} above {hi:.1f} — priced for an event")
    elif cfg.get("reference_band_required_in_paper", True):
        # Refusing in paper too, on purpose. If paper runs with the IV gates silently off,
        # the paper expectancy that gates live trading is measured on a different rule set
        # than live, which makes the whole paper phase non-transferable.
        out.append(f"no reference_band for dte bucket {dte_bucket!r} — run "
                   "calibrate/straddle_bands.py; the gates are not optional in paper")

    if snap.feed_age_seconds > float(g["max_data_staleness_seconds"]):
        out.append(f"feed stale {snap.feed_age_seconds:.1f}s")
    if g.get("require_secondary_broker", True) and not snap.secondary_broker_ok:
        out.append("secondary broker not authenticated")
    if snap.consecutive_loss_days >= int(g["max_consecutive_loss_days"]) and snap.at_min_lots:
        out.append(f"{snap.consecutive_loss_days} losing days and size already at floor")
    return out


def entry_time_for(snap: Snapshot, cfg: dict) -> tuple[str, str]:
    """Section 4.3. Returns (entry_key, reason); entry_key '' means skip the day."""
    t = cfg["entry_timing_gate"]
    if snap.straddle_open is None or not snap.straddle_prev_close:
        return "", "no previous straddle close to compare against"
    ratio = snap.straddle_open / snap.straddle_prev_close
    if ratio >= float(t["same_or_higher_than_prev_close"]):
        return "entry_early", f"straddle opened at {ratio:.0%} of prev close — fear intact"
    if ratio >= float(t["min_fraction_of_prev_close"]):
        return "entry_normal", f"straddle opened at {ratio:.0%} — wait for 09:30"
    return "", f"straddle opened at {ratio:.0%} of prev close — nothing to sell"


# =====================================================================================
# exits — section 4.7. First match wins, evaluated BEFORE any adjustment.
# =====================================================================================
def evaluate_exits(book, snap: Snapshot, cfg: dict, *, state: Mapping[str, Any]
                   ) -> Decision | None:
    """The exit ladder. Returns None when the book should stay open.

    Order is deliberate and must not be reordered: the hard stop is checked before
    anything that could keep a losing book alive.
    """
    if book.is_flat:
        return None
    ex = cfg["exits"]
    pnl = book.pnl(snap.marks)
    units = book.units_at_entry

    # E1 — hard stop. Ends the day. No re-entry, ever.
    if pnl <= -book.risk_budget:
        return Decision(Action.EXIT, code="E1",
                        reason=f"book {pnl:,.0f} hit the stop {-book.risk_budget:,.0f}",
                        detail={"ends_day": True})

    # R6 kill switch — the stop was breached twice over. Something is broken.
    if pnl <= -float(cfg["operational"]["kill_switch_multiple_of_stop"]) * book.risk_budget:
        return Decision(Action.EXIT, code="R6",
                        reason=f"book {pnl:,.0f} is beyond 2x the stop — halting",
                        detail={"ends_day": True, "halt": True})

    # E2 — trailing stop, ratcheting off the high-water mark.
    if state.get("trail_active"):
        give_back = float(ex["trail_give_back_points"]) * units
        if pnl <= book.high_water_mark - give_back:
            return Decision(Action.EXIT, code="E2",
                            reason=f"gave back {book.high_water_mark - pnl:,.0f} from the "
                                   f"high-water mark {book.high_water_mark:,.0f}",
                            detail={"ends_day": True})

    # E3 — time.
    force = _hhmm(cfg["timing"]["force_exit"])
    if snap.now.time() >= force:
        return Decision(Action.EXIT, code="E3",
                        reason=f"force exit at {cfg['timing']['force_exit']}",
                        detail={"ends_day": True})

    # E5 — premium exhausted, as a fraction of the credit actually received.
    if book.entry_credit > 0:
        close_cost = book.net_close_cost(snap.marks)
        if close_cost <= float(ex["premium_exhausted_pct_of_credit"]) * book.entry_credit:
            return Decision(Action.EXIT, code="E5",
                            reason=f"costs {close_cost:,.0f} to close against a "
                                   f"{book.entry_credit:,.0f} credit — the trade is done",
                            detail={"ends_day": True})

    # E4a — both legs above their own session VWAP: volatility is expanding on both sides
    # and there is short-covering pressure on each. Checked before proximity because it
    # describes the whole book, not one strike.
    if snap.vwaps:
        shorts = [l for l in book.open_legs if l.is_short]
        above = [l.symbol for l in shorts
                 if snap.vwaps.get(l.symbol) is not None
                 and snap.marks.get(l.symbol, 0) > snap.vwaps[l.symbol]]
        if len(shorts) == 2 and len(above) == 2:
            return Decision(Action.EXIT, code="E4a",
                            reason="both shorts are above their session VWAP — the "
                                   "average short is losing on each side",
                            detail={"allows_reentry": True, "legs": above})

    # E4b — proximity. After rolls a strike can drift too close to spot to be worth holding.
    if snap.asp:
        nearest = min((abs(l.strike - snap.spot) for l in book.open_legs
                       if l.is_short), default=None)
        if nearest is not None and nearest < float(ex["proximity_abort_asp_fraction"]) * snap.asp:
            return Decision(Action.EXIT, code="E4b",
                            reason=f"nearest short is {nearest:.0f} pts from spot, inside "
                                   f"{float(ex['proximity_abort_asp_fraction']):.0%} of ASP "
                                   f"{snap.asp:.0f}",
                            detail={"allows_reentry": True})

    # E4c — efficiency, bounded to a window so the re-entry path is always still open.
    lo, hi = (_hhmm(t) for t in ex["efficiency_abort_window"])
    if lo <= snap.now.time() <= hi and 0 < pnl < float(ex["efficiency_abort_pct_of_target"]) \
            * book.target_cash:
        stalled = state.get("minutes_since_pnl_improved", 0)
        if stalled >= float(ex["efficiency_abort_no_improvement_minutes"]):
            return Decision(Action.EXIT, code="E4c",
                            reason=f"{pnl:,.0f} after {stalled:.0f} min without progress, "
                                   "inside the abort window",
                            detail={"allows_reentry": True})
    return None


def should_activate_trail(book, snap: Snapshot, cfg: dict) -> bool:
    return book.pnl(snap.marks) >= float(cfg["exits"]["trail_trigger_pct"]) * book.target_cash


def _hhmm(text: str) -> dt.time:
    h, m = str(text).split(":")
    return dt.time(int(h), int(m))


# =====================================================================================
# diagnostics the config asks for by name
# =====================================================================================
def entry_cost_headroom(entry_pnl_points: float, stop_points: float) -> dict:
    """How much of the risk budget the entry itself consumed.

    At dte>=3 the stop is 2.5 points while liquidation marks open the book at -1.0 to -1.5,
    so 40-60% of the budget is gone before the market moves — and the risk-off lever, which
    arms at 60% of the stop, is armed AT ENTRY. This does not decide anything; it is logged
    every session so the size of the problem is measured rather than argued about.
    """
    used = abs(entry_pnl_points) / stop_points if stop_points else float("inf")
    return {"entry_cost_points": round(abs(entry_pnl_points), 3),
            "stop_points": stop_points,
            "fraction_of_stop_consumed": round(used, 3),
            "risk_off_already_armed": used >= 0.60,
            "healthy": used < 0.50}


# =====================================================================================
# V2 — the winner roll
# =====================================================================================
def evaluate_adjustment(book, snap: Snapshot, cfg: dict, *, state: Mapping[str, Any],
                        confirmed_break: str | None = None) -> Decision:
    """Whether to roll, and which leg. Pure: the caller supplies break state and VWAPs.

    Runs only AFTER evaluate_exits has returned None. An exit and an adjustment are never
    both valid, and checking them in the other order would let a book that should be closed
    spend money on a roll first.
    """
    from .adjust import check_rebalance, threatened_legs

    if book.is_flat:
        return Decision(Action.HOLD, reason="no position")

    chk = check_rebalance(book, marks=snap.marks, now=snap.now, state=state, cfg=cfg)
    if not chk.allowed:
        return Decision(Action.HOLD, reason=chk.reason, code="throttled",
                        detail={"ratio": chk.ratio})

    # A level TOUCH is not a break. Without this the loop adjusts on noise.
    if cfg["management"]["break_confirmation_closes"] and confirmed_break is None:
        return Decision(Action.HOLD, code="unconfirmed",
                        reason="no confirmed break; a touch that closes back inside the "
                               "range is not a signal",
                        detail={"ratio": chk.ratio})

    # Lever A moves the WINNER toward spot, which adds risk on the winner's side. If that
    # leg is itself above its session VWAP it is already under pressure, and rolling it
    # closer would be adding to the losing side of a book that is squeezing on both.
    if snap.vwaps:
        threatened = threatened_legs([chk.winner], snap.marks, snap.vwaps)
        if chk.winner.symbol in threatened:
            return Decision(Action.HOLD, code="vwap_veto",
                            reason=f"{chk.winner.symbol} is above its own VWAP; rolling it "
                                   "toward spot would add to the threatened side",
                            detail={"ratio": chk.ratio})

    return Decision(Action.ROLL_WINNER, code="lever_a",
                    reason=f"loser/winner premium {chk.ratio:.2f}, break {confirmed_break}",
                    detail={"winner": chk.winner.symbol, "loser": chk.loser.symbol,
                            "ratio": chk.ratio, "break": confirmed_break})
