"""Read-only view model for the options page.

The options program is PAPER ONLY and this page is its only window. Two things therefore
have to be legible at a glance and must never be asserted where they can be derived:

  - that nothing has been traded. Counted from the order journal, which is the record of
    everything the gateway has ever done, rather than stated as a claim about intent.
  - that the sample is nowhere near decisive. A high-win-rate condor needs roughly 550-710
    observations to separate a real edge from noise at 80% power after costs, and a page
    that showed a mean P&L without that denominator would invite exactly the conclusion
    the experiment exists to avoid.

Nothing here writes, and nothing here can reach the broker.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .. import config as C
from ..core.guards import CARRY_PRODUCTS, is_option
from ..strategies import options_experiment as X

# Power analysis for the difference this experiment could detect. Quoted as a range
# because it moves with the assumed win rate and cost drag; the point is the order of
# magnitude, not the third digit.
SAMPLE_NEEDED = (550, 710)


def _orders_ever(journal: str | None = None) -> dict:
    """Option orders the gateway has ever placed, from its own journal.

    Evidence rather than assertion: the journal records every placement, dry run, guard
    refusal and error, so a non-zero count here would mean the paper-only claim is false.
    """
    path = journal or "data/outputs/orders_journal.jsonl"
    placed = blocked = 0
    if not os.path.exists(path):
        return {"placed": 0, "blocked": 0, "journal_present": False}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sym = str(rec.get("symbol") or rec.get("tradingsymbol") or "")
            if not is_option(sym):
                continue
            if rec.get("event") == "overnight_option_block":
                blocked += 1
            elif rec.get("event") in ("placed", "dry_run"):
                placed += 1
    return {"placed": placed, "blocked": blocked, "journal_present": True}


def _token_coverage(arms: list[dict]) -> dict:
    """How many arms could still be replayed from Kite.

    An arm whose contracts were not recorded cannot be re-fetched at all once its options
    expire: the instruments dump lists only live contracts, and historical_data takes a
    token. This is the difference between a dataset and a summary.
    """
    with_tokens = sum(1 for a in arms if a.get("contracts_json"))
    return {"with_tokens": with_tokens, "without": len(arms) - with_tokens,
            "total": len(arms),
            "pct": round(with_tokens / len(arms) * 100, 1) if arms else None}


def page(conn, *, journal: str | None = None) -> dict[str, Any]:
    """Everything the options page renders. Read-only."""
    rep = X.report(conn)
    all_arms = [dict(r) for r in conn.execute(
        "SELECT * FROM option_arms ORDER BY entry_at DESC")]
    closed = [a for a in all_arms if a["exit_at"]]
    n = len(closed)
    low, high = SAMPLE_NEEDED

    return {
        # --- posture ------------------------------------------------------------------
        "gates": {
            "options_enabled": bool(C.OPTIONS_ENABLED),
            "intraday_enabled": bool(C.INTRADAY_ENABLED),
            # Not a flag. Hard-coded in core/guards.py, which is the point.
            "carry_products_blocked": sorted(CARRY_PRODUCTS),
        },
        "orders": _orders_ever(journal),
        # --- preregistration ----------------------------------------------------------
        "variants": [{
            "variant_id": v["variant_id"], "name": v["name"], "arm": v["arm"],
            "spec": json.loads(v["spec_json"]), "registered_at": v["registered_at"],
            "note": v["note"] or "",
            "closed": (rep["by_variant"].get(v["variant_id"]) or {}).get("n") or 0,
        } for v in X.variants(conn)],
        "trials": rep["trials_registered"],
        # --- collection ---------------------------------------------------------------
        "open_arms": [_arm_row(a) for a in all_arms if not a["exit_at"]],
        "recent": [_arm_row(a) for a in closed[:15]],
        "closed_count": n,
        "summary": rep["by_arm"].get(X.INTRADAY) or {},
        "mean_vs_zero": rep.get("mean_vs_zero"),
        "tokens": _token_coverage(all_arms),
        "sample": {
            "have": n, "need_low": low, "need_high": high,
            "pct": round(min(n / low, 1.0) * 100, 1) if low else 0.0,
            # Sessions, not calendar days: roughly one observation per trading day.
            "sessions_remaining": max(low - n, 0),
        },
    }


def _arm_row(a: dict) -> dict:
    settled = json.loads(a["settled_json"]) if a.get("settled_json") else {}
    contracts = json.loads(a["contracts_json"]) if a.get("contracts_json") else []
    return {
        "arm_id": a["arm_id"], "variant_id": a["variant_id"],
        "session_date": a["session_date"], "expiry": a["expiry"],
        "entry_at": (a["entry_at"] or "")[11:16],
        "exit_at": (a["exit_at"] or "")[11:16] if a["exit_at"] else None,
        "exit_reason": a["exit_reason"] or "",
        "entry_credit": a["entry_credit"], "margin": a["margin"],
        "spot_move_pct": a["spot_move_pct"],
        "net_pnl": settled.get("net_pnl"),
        "return_on_margin_pct": settled.get("return_on_margin_pct"),
        "cost_complete": settled.get("cost_complete"),
        "breach_seen": bool(a["breach_seen"]),
        "legs": len(contracts),
        # The arms that can still be replayed. Shown per row because it is a property of
        # the row, not of the collection: a gap is permanent for exactly those sessions.
        "replayable": bool(contracts),
        "tokens": [c.get("token") for c in contracts],
    }
