"""The Python side of ``nightly_diff.py``: the production worker with the replay provider injected.

    nightly_py.py nightly --date D --replay-bundle DIR
    nightly_py.py step NAME --date D --run-id N [--context FILE] --replay-bundle DIR
    nightly_py.py prime --until NAME --date D --replay-bundle DIR --context-out FILE

Harness code only — production Python is unchanged. At process start it

* arms a **network guard** (``socket.connect``/``getaddrinfo`` refuse anything but loopback and
  Unix sockets — the providers suite's ``network_guard.py`` rule), so a call that escaped the
  replay would fail loudly rather than reach Kite, NSE or R2;
* swaps ``baskfy_worker.providers.build_provider_stack`` for the replay adapter behind the
  production ``CompositeProvider`` (``replay_provider.replay_stack``);
* turns ``kite_session_cli.refresh_quietly`` into a no-op (it would read the desk's Kite session).

``nightly`` calls the Celery task's own body, ``nightly_pipeline(trade_date)`` — three
transactions, the alert, the return value — and prints that value as JSON (or ``{"raised", ...}``
when the chain raised, after the task recorded the failure). It is the production path.

``step`` and ``prime`` need the chain one block at a time, which Python's ``_run_chain`` cannot
do, so :func:`run_block` **mirrors** it block by block, calling the same task functions in the same
order (``rust/crates/baskfy-worker/src/orchestrator.rs`` has the same split). A mirror can drift:
:data:`ORCHESTRATOR_SHA256` pins the source of ``_run_chain`` and the step wrappers it was written
against, and both modes refuse to run when that source changes — re-read the orchestrator, update
the mirror, then the hash.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import inspect
import ipaddress
import json
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


# --- the network guard (before anything else imports a client) ---------------------------------

_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _loopback(host: object) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback or str(host) == "0.0.0.0"
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if self.family == socket.AF_UNIX or (isinstance(address, tuple) and _loopback(address[0])):
        return _real_connect(self, address)
    raise OSError(f"nightly_py network guard: refused a connection to {address!r}")


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if _loopback(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise OSError(f"nightly_py network guard: refused to resolve {host!r}")


setattr(socket.socket, "connect", _guarded_connect)
setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


# --- the worker, with the replay injected ------------------------------------------------------

from replay_provider import replay_stack  # noqa: E402

from baskfy_worker import kite_session_cli  # noqa: E402
from baskfy_worker import orchestrator as orch  # noqa: E402
from baskfy_worker import providers as worker_providers  # noqa: E402

#: sha256 of the orchestrator source the mirror below was written against.
ORCHESTRATOR_SHA256 = "3599857442ef6c9633808e5d784870e5be89f75f3708bd2f76a79235c5d964a3"


def orchestrator_source_hash() -> str:
    parts = [
        inspect.getsource(orch._run_chain),
        inspect.getsource(orch.run_compute_desk_score_step),
        inspect.getsource(orch.run_compute_swing_step),
        inspect.getsource(orch.run_compute_vbt_step),
        inspect.getsource(orch.run_compute_twt_step),
        inspect.getsource(orch.run_nightly_pipeline),
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def inject(bundle: Path) -> None:
    setattr(worker_providers, "build_provider_stack", lambda *a, **k: replay_stack(bundle))
    setattr(kite_session_cli, "refresh_quietly", lambda: False)


@dataclass
class ChainState:
    touched: set[int] | None = None


@dataclass
class Outcome:
    trade_date: dt.date
    run_id: int
    gate: Any = None
    data_version: int | None = None
    failed_step: str | None = None
    steps_completed: list[str] = field(default_factory=list)


class Hard(Exception):
    pass


async def run_block(session: Any, run: Any, trade_date: dt.date, deps: Any, step: str,
                    outcome: Outcome, state: ChainState) -> None:
    """One block of ``orchestrator._run_chain`` — the same calls, in the same order."""
    from baskfy_providers.errors import ProviderError  # noqa: PLC0415
    from baskfy_providers.publication import bhavcopy_publication_check  # noqa: PLC0415
    from baskfy_worker.calendar import CALENDAR_LOOKBACK_DAYS, reconcile_calendar  # noqa: PLC0415
    from baskfy_worker.steps import PipelineStep, StepOutcome, StepStatus, record_step  # noqa: PLC0415
    from baskfy_worker.tasks import (  # noqa: PLC0415
        adjustments,
        bars,
        basket,
        corporate_actions,
        factors,
        listings,
        market_health,
        membership,
        publish,
        quality,
        snapshots,
    )
    from baskfy_worker.tasks import fundamentals as fundamentals_task  # noqa: PLC0415
    from baskfy_worker.tasks import instruments as instruments_task  # noqa: PLC0415
    from baskfy_worker.window import DateWindow  # noqa: PLC0415

    window = deps.bars_window or DateWindow.single(trade_date)
    S = PipelineStep(step)
    if S is PipelineStep.REFRESH_INSTRUMENTS:
        async with record_step(session, run.id, S, trade_date) as st:
            await instruments_task.run_refresh_instruments(session, deps.provider, st, as_of=trade_date)
            register = StepOutcome()
            await listings.run_refresh_listings(session, deps.provider, register)
            st.note(listings=register.detail, listings_rows=register.rows_out)
    elif S is PipelineStep.FETCH_DAILY_BARS:
        active = await instruments_task.active_instruments(session)
        async with record_step(session, run.id, S, trade_date) as st:
            await bars.run_fetch_daily_bars(session, deps.provider, st, active, window)
        calendar_start = min(window.start, trade_date - dt.timedelta(days=CALENDAR_LOOKBACK_DAYS))
        calendar = await reconcile_calendar(
            session, calendar_start, window.end, published=bhavcopy_publication_check(deps.provider)
        )
        if calendar.missed_sessions:
            print("sessions NSE published but we did not ingest: "
                  + ", ".join(d.isoformat() for d in calendar.missed_sessions), file=sys.stderr)
    elif S is PipelineStep.FETCH_CORPORATE_ACTIONS:
        async with record_step(session, run.id, S, trade_date) as st:
            since = trade_date - dt.timedelta(days=orch.CORPORATE_ACTION_LOOKBACK_DAYS)
            state.touched = await corporate_actions.run_fetch_corporate_actions(
                session, deps.provider, st, since
            )
    elif S is PipelineStep.APPLY_ADJUSTMENTS:
        if state.touched is None:
            raise RuntimeError(
                "apply_adjustments needs fetch_corporate_actions' touched instruments; run "
                "the chain, or pass them in the step context"
            )
        touched = state.touched
        async with record_step(session, run.id, S, trade_date) as st:
            if not touched:
                st.status = StepStatus.SKIPPED
            await adjustments.run_apply_adjustments(session, st, touched)
    elif S is PipelineStep.REFRESH_INDEX_MEMBERSHIP:
        async with record_step(session, run.id, S, trade_date) as st:
            await membership.run_refresh_index_membership(session, deps.provider, st, trade_date)
    elif S is PipelineStep.REFRESH_INDEX_SNAPSHOTS:
        async with record_step(session, run.id, S, trade_date) as st:
            await snapshots.run_refresh_index_snapshots(session, deps.provider, st, trade_date)
            fundamentals = StepOutcome()
            try:
                await fundamentals_task.run_fetch_fundamentals(
                    session, deps.provider, fundamentals, trade_date,
                    await fundamentals_task.fundamentals_scope(session, trade_date),
                )
            except ProviderError as exc:
                fundamentals.note(error=str(exc))
            st.note(fundamentals=fundamentals.detail, fundamentals_rows=fundamentals.rows_out)
    elif S is PipelineStep.COMPUTE_FACTORS:
        async with record_step(session, run.id, S, trade_date) as st:
            await factors.run_compute_factors(session, st, trade_date, deps.factor_engine)
            desk = await orch.run_compute_desk_score_step(session, trade_date)
            st.note(desk_score={**desk.detail, "status": desk.status.value}, desk_score_rows=desk.rows_out)
    elif S is PipelineStep.COMPUTE_MARKET_HEALTH:
        async with record_step(session, run.id, S, trade_date) as st:
            await market_health.run_compute_market_health(session, st, trade_date)
    elif S is PipelineStep.DATA_QUALITY_GATE:
        async with record_step(session, run.id, S, trade_date) as st:
            report = await quality.run_data_quality_gate(session, st, trade_date)
            outcome.gate = report
            if not report.passed:
                st.status = StepStatus.FAILED
        outcome.steps_completed.append(step)
        if not report.passed:
            outcome.failed_step = step
            raise Hard("the data-quality gate failed: "
                       + "; ".join(f"[{r.assertion}] {r.message}" for r in report.failures))
        return
    elif S is PipelineStep.PUBLISH:
        async with record_step(session, run.id, S, trade_date) as st:
            result = await publish.run_publish(session, st, run, deps.cache)
            outcome.data_version = result.data_version
    elif S is PipelineStep.REFRESH_BASKET:
        async with record_step(session, run.id, S, trade_date) as st:
            names = await basket.run_refresh_basket(session, st, trade_date)
            if names == 0:
                st.status = StepStatus.SKIPPED
    elif S is PipelineStep.COMPUTE_SWING:
        await orch.run_compute_swing_step(session, run.id, trade_date, deps)
    elif S is PipelineStep.COMPUTE_VBT:
        await orch.run_compute_vbt_step(session, run.id, trade_date, deps)
    elif S is PipelineStep.COMPUTE_TWT:
        await orch.run_compute_twt_step(session, run.id, trade_date, deps)
    outcome.steps_completed.append(step)


def emit(payload: Any) -> None:
    print(json.dumps(payload, default=str), flush=True)


def cmd_nightly(day: dt.date) -> int:
    from baskfy_worker import ops  # noqa: PLC0415
    from baskfy_worker.tasks.celery_tasks import nightly_pipeline  # noqa: PLC0415

    seen: dict[str, int] = {}
    real_begin = ops.begin_run

    async def begin_run(session: Any, trade_date: dt.date) -> int:
        run_id = await real_begin(session, trade_date)
        seen["run_id"] = run_id
        return run_id

    setattr(ops, "begin_run", begin_run)
    try:
        result = nightly_pipeline(day.isoformat())
    except Exception as exc:  # the task re-raises after recording the failure; so do we, as JSON
        emit({"run_id": seen.get("run_id"), "trade_date": day.isoformat(),
              "raised": type(exc).__name__, "error": str(exc)})
        return 1
    emit(result)
    return 0 if result.get("status") == "succeeded" else 1


async def _single(session: Any, day: dt.date, run_id: int, step: str, state: ChainState) -> dict[str, Any]:
    from baskfy_worker.steps import adopt_run  # noqa: PLC0415
    from baskfy_worker.providers import build_pipeline_dependencies  # noqa: PLC0415

    if not await orch.try_lock_pipeline(session):
        raise RuntimeError(orch_lock_message())
    run = await adopt_run(session, run_id)
    outcome = Outcome(day, run.id)
    deps = build_pipeline_dependencies()
    hard = None
    try:
        await run_block(session, run, day, deps, step, outcome, state)
    except Hard as exc:
        hard = str(exc)
    return {
        "step": step,
        "trade_date": day.isoformat(),
        "run_id": run_id,
        "result": "hard_failure" if hard else "completed",
        "data_version": outcome.data_version,
        "gate_passed": None if outcome.gate is None else bool(outcome.gate.passed),
        "hard_failure": hard,
    }


def orch_lock_message() -> str:
    return ("another pipeline run holds the chain lock; this attempt was abandoned rather "
            "than queued behind it")


def require_mirror() -> None:
    actual = orchestrator_source_hash()
    if actual != ORCHESTRATOR_SHA256:
        raise SystemExit(
            f"orchestrator.py changed since the step mirror was written (sha256 {actual}); "
            "re-read _run_chain, update run_block, then ORCHESTRATOR_SHA256"
        )


def read_state(path: Path | None) -> ChainState:
    if path is None:
        return ChainState()
    data = json.loads(path.read_text())
    touched = data.get("touched")
    return ChainState(touched=None if touched is None else {int(x) for x in touched})


def cmd_step(day: dt.date, run_id: int, step: str, context: Path | None) -> int:
    from baskfy_worker.db import session_scope  # noqa: PLC0415

    require_mirror()
    state = read_state(context)

    async def go() -> dict[str, Any]:
        async with session_scope() as session:
            return await _single(session, day, run_id, step, state)

    try:
        payload = asyncio.run(go())
    except Exception as exc:
        emit({"step": step, "trade_date": day.isoformat(), "run_id": run_id, "result": "raised",
              "error_type": type(exc).__name__, "error": str(exc)})
        return 1
    emit(payload)
    return 1 if payload["result"] == "hard_failure" else 0


def cmd_prime(day: dt.date, until: str, context_out: Path) -> int:
    """Begin a run (committed) and run every block before ``until`` on one transaction."""
    from baskfy_worker import ops  # noqa: PLC0415
    from baskfy_worker.db import run_in_session, session_scope  # noqa: PLC0415
    from baskfy_worker.providers import build_pipeline_dependencies  # noqa: PLC0415
    from baskfy_worker.steps import NIGHTLY_CHAIN, adopt_run  # noqa: PLC0415

    require_mirror()
    order = [s.value for s in NIGHTLY_CHAIN]
    if until not in order:
        raise SystemExit(f"unknown step {until!r}")
    before = order[: order.index(until)]
    run_id: int = run_in_session(lambda session: ops.begin_run(session, day))
    state = ChainState()

    async def go() -> None:
        async with session_scope() as session:
            if not await orch.try_lock_pipeline(session):
                raise RuntimeError(orch_lock_message())
            run = await adopt_run(session, run_id)
            outcome = Outcome(day, run.id)
            deps = build_pipeline_dependencies()
            for step in before:
                await run_block(session, run, day, deps, step, outcome, state)

    asyncio.run(go())
    context_out.write_text(json.dumps(
        {"run_id": run_id, "until": until,
         "touched": None if state.touched is None else sorted(state.touched)}
    ))
    emit({"run_id": run_id, "primed_through": before[-1] if before else None})
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    n = sub.add_parser("nightly")
    n.add_argument("--date", required=True)
    n.add_argument("--replay-bundle", required=True, type=Path)
    s = sub.add_parser("step")
    s.add_argument("name")
    s.add_argument("--date", required=True)
    s.add_argument("--run-id", required=True, type=int)
    s.add_argument("--context", type=Path)
    s.add_argument("--replay-bundle", required=True, type=Path)
    p = sub.add_parser("prime")
    p.add_argument("--until", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--replay-bundle", required=True, type=Path)
    p.add_argument("--context-out", required=True, type=Path)
    h = sub.add_parser("mirror-hash")
    del h
    args = parser.parse_args(argv)
    if args.command == "mirror-hash":
        print(orchestrator_source_hash())
        return 0
    inject(args.replay_bundle)
    day = dt.date.fromisoformat(args.date)
    if args.command == "nightly":
        return cmd_nightly(day)
    if args.command == "step":
        return cmd_step(day, args.run_id, args.name, args.context)
    return cmd_prime(day, args.until, args.context_out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
