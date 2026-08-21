"""Persistence for regime decisions, exposure reconciliation and book snapshots.

TWO SEPARATE FACTS, TWO SEPARATE TABLES
`regime_evaluations` records what the market said. `regime_exposure` records what the
portfolio actually is. Finalising a policy decision and completing its orders are
different events, so a committed tier is never evidence that the cap was achieved and a
failed or partial plan stays reconcilable without minting a new weekly transition.

COMMIT-ONCE SEMANTICS
`evaluation_id` is deterministic (week end + session + algorithm + config hash), so the
same week always addresses the same decision. DRY_RUN and observe-mode previews are stored
with their own run_id purely for audit; a partial unique index reserves run_id='canonical'
for the single committed record. A preview therefore can never block or pre-empt the later
live commit of the same deterministic decision.

DRY_RUN is enforced HERE as well as upstream: commit_evaluation() refuses outright when
dry_run is true. Defence in depth — the rule must not depend on a caller remembering it.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from typing import Any, Mapping

from ..core.regime import (BookWeights, ExecutionStatus, PreviousRegime, RegimeMode,
                           RegimeState, RegimeTier, WeightSource)
from . import db

CANONICAL = "canonical"


class DryRunCommitError(RuntimeError):
    """Raised when something tries to commit a policy tier during DRY_RUN."""


class AlreadyCommittedError(RuntimeError):
    """Raised when an evaluation_id already holds a canonical committed record."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _j(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# =====================================================================================
# evaluations
# =====================================================================================
def _insert(conn, state: RegimeState, *, run_id: str, dry_run: bool, mode: RegimeMode,
            committed: bool, input_snapshot: Mapping[str, Any] | None) -> int:
    snapshot = dict(input_snapshot or {})
    snapshot.setdefault("index_diagnostics", state.index_diagnostics)
    snapshot.setdefault("health", {str(k): v for k, v in state.health.items()})
    snapshot.setdefault("bearish_stack_weight", state.bearish_stack_weight)
    snapshot.setdefault("breadth_pct", state.breadth_pct)
    snapshot.setdefault("breadth_coverage_pct", state.breadth_coverage_pct)

    cur = conn.execute(
        """INSERT INTO regime_evaluations(
               evaluation_id, run_id, committed, dry_run, mode,
               scheduled_week_end, signal_session_date, data_as_of,
               raw_candidate_tier, previous_policy_tier, policy_tier, transition_limited,
               new_buys, forced_action, override_active, data_stale,
               manual_action_required, breadth_pct, breadth_coverage_pct,
               book_weights_json, book_weight_source, input_snapshot_json,
               reason_codes_json, reasons_json, next_evaluation_date,
               last_transition_date, algorithm_version, config_hash, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (state.evaluation_id, run_id, int(committed), int(dry_run), mode.value,
         state.scheduled_week_end.isoformat(), state.signal_session_date.isoformat(),
         state.as_of_date.isoformat(),
         state.raw_candidate_tier.value,
         state.previous_policy_tier.value if state.previous_policy_tier else None,
         state.policy_tier.value, int(state.transition_limited),
         state.new_buys.value, state.forced_action.value, int(state.override_active),
         int(state.data_stale), int(state.manual_action_required),
         state.breadth_pct, state.breadth_coverage_pct,
         _j(dict(state.book_category_weights)), state.book_weight_source.value,
         _j(snapshot), _j(list(state.reason_codes)), _j(list(state.reasons)),
         state.next_evaluation_date.isoformat(),
         state.last_transition_date.isoformat() if state.last_transition_date else None,
         state.algorithm_version, state.config_hash, _now()))
    return int(cur.lastrowid)


def save_preview(conn, state: RegimeState, *, mode: RegimeMode, dry_run: bool = True,
                 run_id: str | None = None,
                 input_snapshot: Mapping[str, Any] | None = None) -> str:
    """Persist a non-binding preview. Never touches the canonical record."""
    rid = run_id or f"preview-{uuid.uuid4().hex[:12]}"
    if rid == CANONICAL:
        raise ValueError("run_id 'canonical' is reserved for committed decisions")
    with db.transaction(conn):
        _insert(conn, state, run_id=rid, dry_run=dry_run, mode=mode, committed=False,
                input_snapshot=input_snapshot)
    return rid


def commit_evaluation(conn, state: RegimeState, *, mode: RegimeMode, dry_run: bool,
                      input_snapshot: Mapping[str, Any] | None = None) -> str:
    """Commit the canonical weekly policy decision. Exactly once per evaluation_id.

    Refuses during DRY_RUN — committing a tier transition is one of the three things
    DRY_RUN must never do, alongside submitting orders and marking a plan executed.
    """
    if dry_run:
        raise DryRunCommitError(
            f"DRY_RUN is active: refusing to commit policy tier "
            f"{state.policy_tier.value} for {state.scheduled_week_end}. "
            f"Use save_preview() instead.")
    if mode is not RegimeMode.ENFORCE and mode is not RegimeMode.PROPOSE:
        raise DryRunCommitError(
            f"mode={mode.value} does not commit policy tiers; use save_preview()")
    try:
        with db.transaction(conn):
            _insert(conn, state, run_id=CANONICAL, dry_run=False, mode=mode,
                    committed=True, input_snapshot=input_snapshot)
    except sqlite3.IntegrityError as exc:
        raise AlreadyCommittedError(
            f"evaluation {state.evaluation_id} ({state.scheduled_week_end}) is already "
            f"committed — a week's policy transition happens once") from exc
    return CANONICAL


def get_canonical(conn, evaluation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id=?",
        (evaluation_id, CANONICAL)).fetchone()


def previews_for(conn, evaluation_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id<>? "
        "ORDER BY created_at", (evaluation_id, CANONICAL)))


def latest_committed(conn) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM regime_evaluations WHERE run_id=? "
        "ORDER BY scheduled_week_end DESC, id DESC LIMIT 1", (CANONICAL,)).fetchone()


def latest_evaluation(conn) -> sqlite3.Row | None:
    """The most recent evaluation of ANY kind, committed or preview.

    Distinct from latest_committed on purpose. That one answers "what policy is in force",
    which only a committed decision can set. This answers "which regime view was on screen
    when this happened", which is what a stored plan needs to be interpretable later —
    otherwise, in observe mode, every plan is stamped with nothing at all.

    A preview never AUTHORISES anything; `committed` on the row says which kind it was.
    """
    return conn.execute(
        "SELECT * FROM regime_evaluations "
        "ORDER BY scheduled_week_end DESC, id DESC LIMIT 1").fetchone()


def previous_regime(conn, *, recovery_confirmations: int | None = None
                    ) -> PreviousRegime | None:
    """The engine's `previous` input, rebuilt from the last COMMITTED decision only.

    Previews are deliberately invisible here: a DRY_RUN run must not be able to move the
    live policy baseline.
    """
    row = latest_committed(conn)
    if row is None:
        return None
    weights = json.loads(row["book_weights_json"])
    snap = BookWeights(weights, WeightSource(row["book_weight_source"]),
                       row["breadth_coverage_pct"] or 0.0)
    return PreviousRegime(
        policy_tier=RegimeTier(row["policy_tier"]),
        scheduled_week_end=dt.date.fromisoformat(row["scheduled_week_end"]),
        last_transition_date=dt.date.fromisoformat(row["last_transition_date"])
        if row["last_transition_date"] else None,
        recovery_confirmations=(recovery_confirmations
                                if recovery_confirmations is not None
                                else count_recovery_confirmations(conn)),
        book_weights=snap)


def count_recovery_confirmations(conn) -> int:
    """Consecutive most-recent committed evaluations whose recovery conditions held.

    Counted in completed WEEKLY evaluations, never by forward-filling a weekly audit
    value into daily breadth — that would manufacture history that never existed.
    """
    rows = conn.execute(
        "SELECT override_active, reason_codes_json FROM regime_evaluations "
        "WHERE run_id=? ORDER BY scheduled_week_end DESC", (CANONICAL,)).fetchall()
    n = 0
    for r in rows:
        codes = json.loads(r["reason_codes_json"])
        if r["override_active"] or "RECOVERY_OVERRIDE_UNCONFIRMED" in codes:
            n += 1
        else:
            break
    return n


def committed_history(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end",
        (CANONICAL,)))


# =====================================================================================
# exposure reconciliation
# =====================================================================================
def record_exposure(conn, *, evaluation_id: str, actual_equity_pct: float,
                    target_equity_cap_pct: float, pending_buy_pct: float = 0.0,
                    pending_sell_pct: float = 0.0,
                    execution_status: ExecutionStatus = ExecutionStatus.NOT_PLANNED,
                    observed_at: str | None = None, note: str | None = None) -> dict:
    """Append an exposure observation. Safe to call on every invocation."""
    when = observed_at or _now()
    gap = round(actual_equity_pct - target_equity_cap_pct, 6)
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO regime_exposure(evaluation_id, observed_at, actual_equity_pct,
                   target_equity_cap_pct, exposure_gap_pct, pending_buy_pct,
                   pending_sell_pct, execution_status, note)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evaluation_id, observed_at) DO UPDATE SET
                   actual_equity_pct=excluded.actual_equity_pct,
                   target_equity_cap_pct=excluded.target_equity_cap_pct,
                   exposure_gap_pct=excluded.exposure_gap_pct,
                   pending_buy_pct=excluded.pending_buy_pct,
                   pending_sell_pct=excluded.pending_sell_pct,
                   execution_status=excluded.execution_status,
                   note=excluded.note""",
            (evaluation_id, when, actual_equity_pct, target_equity_cap_pct, gap,
             pending_buy_pct, pending_sell_pct, execution_status.value, note))
    return {"evaluation_id": evaluation_id, "observed_at": when, "exposure_gap_pct": gap,
            "execution_status": execution_status.value}


def latest_exposure(conn, evaluation_id: str | None = None) -> sqlite3.Row | None:
    if evaluation_id:
        return conn.execute(
            "SELECT * FROM regime_exposure WHERE evaluation_id=? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1", (evaluation_id,)).fetchone()
    return conn.execute(
        "SELECT * FROM regime_exposure ORDER BY observed_at DESC, id DESC LIMIT 1"
    ).fetchone()


def open_exposure_gap(conn, tolerance_pct: float = 1.0) -> sqlite3.Row | None:
    """The most recent observation still above its cap — the retry queue, in effect."""
    row = latest_exposure(conn)
    if row and row["exposure_gap_pct"] > tolerance_pct:
        return row
    return None


def exposure_history(conn, evaluation_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM regime_exposure WHERE evaluation_id=? ORDER BY observed_at",
        (evaluation_id,)))


# =====================================================================================
# book snapshots
# =====================================================================================
def save_book_snapshot(conn, *, evaluation_id: str, scheduled_week_end: dt.date,
                       policy_tier: RegimeTier, weights: BookWeights) -> None:
    """Freeze the full-risk book composition for this evaluation week."""
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO regime_book_snapshots(evaluation_id, scheduled_week_end,
                   policy_tier, weights_json, source, coverage_pct, created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(evaluation_id) DO UPDATE SET
                   weights_json=excluded.weights_json, source=excluded.source,
                   coverage_pct=excluded.coverage_pct, policy_tier=excluded.policy_tier""",
            (evaluation_id, scheduled_week_end.isoformat(), policy_tier.value,
             _j(dict(weights.weights)), weights.source.value, weights.coverage_pct, _now()))


def last_r1_snapshot(conn) -> BookWeights | None:
    """Most recent R1 full-risk weights — the engine's second-choice weight source.

    Only R1 snapshots qualify: at R2/R3/R4 the book has already been reduced, and reusing
    a reduced composition as next week's strategic weights is the feedback loop the spec
    forbids.
    """
    row = conn.execute(
        "SELECT * FROM regime_book_snapshots WHERE policy_tier='R1' "
        "ORDER BY scheduled_week_end DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return BookWeights(json.loads(row["weights_json"]), WeightSource(row["source"]),
                       row["coverage_pct"])
