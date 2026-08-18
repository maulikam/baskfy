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
from typing import Any, Mapping

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


def strangles() -> list[dict[str, Any]]:
    """One block per configured underlying, in registry order.

    Each keeps its own bands, paper record and locks. Pooling them would build a reference
    band from three different distributions — not a wider band, a meaningless one.
    """
    from ..strategies.strangle import instruments as _ins
    return [{**strangle(u.config, u), "slug": u.slug, "label": u.label,
             "series": u.series, "exchange": u.exchange}
            for u in _ins.configured()]


def strangle(cfg_path: str = "config/strangle.yaml", und=None) -> dict[str, Any]:
    """State of one intraday strangle, computed WITHOUT a Kite session.

    The page must answer "what is this doing and may it trade" when logged out, because
    that is exactly when someone asks. Everything here comes from config, the forward
    record and the journal — no broker call, so the page never blocks on a token.
    """
    from ..strategies.strangle import calibrate as _cal
    from ..strategies.strangle import config as _sc
    from ..strategies.strangle import instruments as _ins
    from ..strategies.strangle import journal as _sj
    from ..strategies.strangle import live as _live

    und = und or _ins.get(_ins.DEFAULT)
    try:
        cfg = _sc.load(cfg_path)
    except Exception as exc:                                  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    cfg["operational"]["journal_path"] = und.journal()
    cfg["operational"]["lockout_path"] = und.lockout()

    forward = _cal.load_forward(und.forward())
    bands = _cal.build_bands(forward) if forward else {}
    ready = _cal.readiness(bands) if bands else {
        "ready": False, "missing_buckets": ["3+", "2", "1"], "thin_buckets": [],
        "note": "no observations yet — run 'Record today's straddle' each session"}
    jr = _sj.Journal(cfg["operational"]["journal_path"])
    pf = _live.preflight(cfg, jr)
    sessions = _live.completed_paper_sessions(jr)

    tm = cfg["timing"]
    return {
        "available": True,
        "mode": cfg["meta"]["mode"],
        "underlying": cfg["instrument"]["underlying"],
        "lot_size": cfg["instrument"]["lot_size"],
        "expiry_weekday": cfg["instrument"]["expiry_weekday"].title(),
        "expiry_series": cfg["instrument"].get("expiry_series", "weekly"),
        "max_dte": cfg["session"].get("max_dte"),
        "allow_expiry_day": bool(cfg["session"].get("allow_expiry_day", False)),
        "entry_window_end": tm["entry_window_end"],
        "allow_late_entry": bool(tm.get("allow_late_entry", False)),
        "late_entry_size_mult": tm.get("late_entry_size_mult"),
        "no_new_entry_after": tm["no_new_entry_after"],
        "allocation_pct": round(float(cfg["margin"]["max_utilisation_pct"]) * 100),
        "lots": cfg["sizing"]["lots"],
        "wing_width": cfg["structure"]["wing_width_points"],
        "structure": cfg["structure"]["type"],
        "margin_per_lot": cfg["margin"]["margin_per_lot_estimate"],
        "max_utilisation_pct": round(float(cfg["margin"].get(
            "portfolio_max_utilisation_pct", cfg["margin"]["max_utilisation_pct"])) * 100),
        "min_stop_to_cost": cfg["session"]["min_stop_to_entry_cost_ratio"],
        "extra_holidays": cfg["session"].get("extra_holidays") or [],
        "forward_sessions": len(forward),
        "bands": {b: {"low": r["low"], "high": r["high"], "n": r["n"],
                      "sufficient": r["sufficient"],
                      "vetoes_pct": r["would_have_vetoed_pct"]}
                  for b, r in bands.items()},
        "bands_ready": ready,
        "paper_sessions": len(sessions),
        "paper_needed": int(cfg["live"]["min_paper_sessions_before_live"]),
        "expectancy": _live.observed_expectancy(sessions),
        "locks": pf.as_dict(),
        # The blocking sequence, in the order it has to happen. Shown as a path rather
        # than a list of switches, because "why can I not trade" is one question with one
        # answer at a time.
        "next_action": _next_action(len(forward), ready, len(sessions),
                                    int(cfg["live"]["min_paper_sessions_before_live"])),
    }


def _next_action(forward: int, ready: Mapping, paper: int, needed: int) -> dict:
    if not ready.get("ready"):
        return {"op": "strangle_collect", "label": "Record today's straddle",
                "why": f"the IV gates need reference bands and {forward} observations "
                       "have been recorded; every tradeable bucket needs a sample before "
                       "any session can be entered"}
    if paper < needed:
        return {"op": "strangle_session", "label": "Run a paper session",
                "why": f"{paper} of {needed} completed paper sessions"}
    return {"op": "strangle_check", "label": "Check the strangle",
            "why": "bands are calibrated and the paper record is complete; the remaining "
                   "locks are deliberate switches, not progress"}


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
        "strangle": strangle(),
        "strangles": strangles(),
        "commitments": _commitments(),
        "jobs": _jobs(conn),
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


def _commitments() -> dict:
    """Margin held right now by live sessions, and what is left for the next one.

    The account is one account. Three instruments each sized to their own allocation is
    only safe while something adds them up, and this is where an operator can see it.
    """
    from ..strategies.strangle import allocation as _alloc
    from ..strategies.strangle import config as _sc
    from ..strategies.strangle import instruments as _ins
    live = _alloc.live()
    total = float(sum(float(r.get("margin") or 0.0) for r in live.values()))
    usable = ceiling = None
    try:
        cfg = _sc.load(_ins.get(_ins.DEFAULT).config)
        from ..strategies.strangle import sizing as _z
        usable = _z.capital_from_config(cfg).usable
        ceiling = usable * float(cfg["margin"].get("portfolio_max_utilisation_pct", 0.40))
    except Exception:                                          # noqa: BLE001
        pass
    return {"live": {k: {"margin": round(float(v.get("margin") or 0)),
                         "at": v.get("at")} for k, v in live.items()},
            "committed": round(total),
            "usable": None if usable is None else round(usable),
            "ceiling": None if ceiling is None else round(ceiling),
            "free": None if ceiling is None else round(max(ceiling - total, 0)),
            "pct": None if not ceiling else round(total / ceiling * 100, 1)}


# =====================================================================================
# the run controls
# =====================================================================================
STRANGLE_OPS = ("strangle_check", "strangle_collect", "strangle_calibrate",
                "strangle_calibrate_write", "strangle_session")


def _jobs(conn) -> dict:
    """What is running and what each strangle control did last time.

    Operations share one lock and one broker session, so a control is disabled while
    anything is in flight — including the equity daily job. Showing WHICH job holds the
    lock matters: a disabled button with no explanation reads as broken.
    """
    from . import ops as _ops
    try:
        running = _ops.running_job(conn)
        last = _ops.last_run(conn)
    except Exception:                                        # noqa: BLE001
        return {"running": None, "last": {}, "controls": []}
    controls = []
    for name in STRANGLE_OPS:
        op = _ops.BY_NAME.get(name)
        if op is None:
            continue
        controls.append({
            "name": name, "label": op.label, "summary": op.summary,
            "cli": op.cli(), "writes": op.writes, "long_running": op.long_running,
            "params": [{"key": p.key, "label": p.label, "kind": p.kind,
                        "default": p.default, "choices": list(p.choices), "help": p.help}
                       for p in op.params],
            "last": last.get(name),
        })
    return {"running": running, "last": {k: v for k, v in last.items()
                                         if k in STRANGLE_OPS},
            "controls": controls}
