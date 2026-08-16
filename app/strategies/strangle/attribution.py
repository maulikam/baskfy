"""What rolling actually contributed, per session.

The V2 acceptance criterion. Without it you cannot tell whether Lever A earns its costs or
just moves money around: a session that ends +6 says nothing about the roll unless you also
know what the untouched book would have been worth at the same instant.

THE COUNTERFACTUAL IS A COMPARISON, NOT A PROOF.
It marks the ORIGINAL legs at the current quotes and nets an entry and one exit. What it
cannot know is whether the un-rolled book would still have been open: if the original legs
would have breached the stop earlier, the true V1 outcome is the stop, not this number.
That case is flagged rather than hidden, because silently comparing against a book that
would already have been closed overstates what rolling saved.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .book import cost_of


def counterfactual_pnl(original_legs: Sequence, marks: Mapping[str, float],
                       lot_size: int) -> float | None:
    """Value of the never-rolled book right now, net of an entry and a single exit."""
    if any(l.symbol not in marks for l in original_legs):
        return None
    gross = sum(l.unrealised(marks[l.symbol]) for l in original_legs)
    entry = [{"side": l.side, "price": l.entry_price, "quantity": l.quantity}
             for l in original_legs]
    exit_ = [{"side": "BUY" if l.is_short else "SELL", "price": marks[l.symbol],
              "quantity": l.quantity} for l in original_legs]
    return gross - cost_of(entry, lot_size) - cost_of(exit_, lot_size)


def attribution(book, original_legs: Sequence, marks: Mapping[str, float],
                lot_size: int, *, adjustments: int = 0) -> dict[str, Any]:
    """Actual against never-rolled, in rupees and in points."""
    actual = book.pnl(marks)
    cf = counterfactual_pnl(original_legs, marks, lot_size)
    units = book.units_at_entry or 1
    out: dict[str, Any] = {
        "adjustments": adjustments,
        "actual_pnl": round(actual, 2),
        "actual_points": round(actual / units, 3),
        "counterfactual_pnl": None if cf is None else round(cf, 2),
        "roll_contribution": None if cf is None else round(actual - cf, 2),
        "roll_contribution_points": None if cf is None else round((actual - cf) / units, 3),
    }
    if cf is not None:
        # If the untouched book would already have stopped out, the honest V1 comparison is
        # the stop, not this mark — and the contribution measured against it is optimistic.
        stopped = cf <= -book.risk_budget
        out["counterfactual_would_have_stopped"] = stopped
        out["comparison_valid"] = not stopped
        if stopped:
            out["note"] = ("the un-rolled book would have breached the stop before now, so "
                           "the true comparison is -risk_budget and this contribution is "
                           "an overstatement")
    return out


def session_summary(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate attribution across sessions, reporting only the valid comparisons."""
    valid = [r for r in rows if r.get("comparison_valid", True)
             and r.get("roll_contribution") is not None]
    rolled = [r for r in valid if r.get("adjustments", 0) > 0]
    total = sum(r["roll_contribution"] for r in valid)
    return {
        "sessions": len(rows),
        "comparable": len(valid),
        "excluded_would_have_stopped": len(rows) - len(valid),
        "sessions_with_a_roll": len(rolled),
        "total_roll_contribution": round(total, 2),
        "mean_per_rolled_session": (round(sum(r["roll_contribution"] for r in rolled)
                                          / len(rolled), 2) if rolled else None),
        "positive_sessions": sum(1 for r in rolled if r["roll_contribution"] > 0),
        "negative_sessions": sum(1 for r in rolled if r["roll_contribution"] < 0),
    }
