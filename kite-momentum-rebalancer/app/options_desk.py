"""The desk's `/nifty-options` page and its two POSTs (`docs/options/05` §3, `06` OP10).

The desk console is the only place an options plan becomes an order (`02` Track C §4: the web app
gets no route that can reach the gateway). This module is that place's HTTP face:

* `GET /nifty-options` — today's plans per sleeve with their legs in send order, each confirmable
  plan with its **"Confirm — simulated"** button and the sleeve's exit sentence verbatim (05 §3);
  open positions with **Close now**;
* `GET /nifty-options/data` — the same view as JSON;
* `POST /nifty-options/execute` — form `plan_id`, `confirm`; `options_execute.execute_entry`;
* `POST /nifty-options/close` — form `session_id`, `confirm`; a MANUAL exit plan, then
  `options_execute.execute_exit`.

CSRF, the host allowlist and sign-in are the desk's `DeskSecurity` middleware, for every POST. No
scheduler reaches either POST, and there is no route that confirms without a request.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from baskfy_core.options.config import Sleeve
from baskfy_core.options.exits import ExitVerdict

from . import config as C
from . import options_execute as _execute

router = APIRouter()
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA = "public"

#: `05` §3, verbatim, under each sleeve's Confirm button.
EXIT_SENTENCES: dict[str, str] = {
    "O1": (
        "Confirming this plan also authorises its rule-driven exits — profit at ½C, stop at 1.5C, a "
        "short-strike touch, and the mandatory flat at 14:30 — without a second click."
    ),
    "O2": (
        "Confirming this plan also authorises its exits — the 30 % stop, the 60 % target, a close "
        "back inside the opening range, the 45-minute time stop, and the flat at 15:00 — without a "
        "second click."
    ),
    "O3": (
        "Confirming this plan also authorises its exits — 80 % of width, half the debit lost, the "
        "setup's invalidation, and the flat at 14:45 — without a second click."
    ),
}


def sentence_for(sleeve: str) -> str:
    return EXIT_SENTENCES[sleeve[:2]]


@contextlib.contextmanager
def open_store() -> Iterator[_execute.PgOptionsStore]:
    """The sole user's store over the desk's connection. Tests replace this with their own."""
    from .analytics import db as _db  # noqa: PLC0415 - the desk imports its DB lazily

    with _db.connect() as conn:
        yield _execute.PgOptionsStore(conn, user_id=C.SOLE_USER_ID, schema=SCHEMA)


def gateway_for(sleeve: Sleeve) -> Any:  # noqa: ANN401 - an OrderGateway
    """This sleeve's gateway, built lazily with the desk's Kite client and shared risk manager."""
    from . import main as _main  # noqa: PLC0415 - main mounts this router; at call time

    _main.gateway()  # builds `_risk` as a side effect
    return _execute.options_gateway(sleeve, _main.kite().kc, _main._risk)


def quotes() -> _execute.QuoteSource:
    from . import main as _main  # noqa: PLC0415

    return _execute.kite_quotes(_main.kite())


def mode_of(sleeve: Sleeve) -> Any:  # noqa: ANN401 - a Mode
    from .options_gates import options_gates  # noqa: PLC0415

    return options_gates(sleeve).mode


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def build_view(store: _execute.PgOptionsStore, *, now: dt.datetime) -> dict[str, Any]:
    """Today's entry plans and open positions, as the page and `/data` show them."""
    day = now.date()
    rows = store.conn.execute(
        f"SELECT p.plan_id, p.sleeve, p.status, p.expires_at, p.structure, s.state "
        f"FROM {store.t('op_plan')} p JOIN {store.t('op_session')} s ON s.id = p.session_id "
        "WHERE p.user_id = ? AND s.trade_date = ? AND p.kind = 'ENTRY' ORDER BY p.issued_at",
        (store.user_id, day),
    ).fetchall()
    plans: list[dict[str, Any]] = []
    for row in rows:
        plan = store.plan(str(row["plan_id"]))
        if plan is None:
            continue
        expires = plan.expires_at
        confirmable = plan.status == "ISSUED" and plan.session_state == "PLANNED" and now < expires
        plans.append(
            {
                "plan_id": plan.plan_id,
                "sleeve": plan.sleeve.value,
                "structure": plan.structure.value,
                "status": plan.status,
                "session_state": plan.session_state,
                "expires_at": expires.isoformat(),
                "confirmable": confirmable,
                "paper": mode_of(plan.sleeve).value == "PAPER",
                "sentence": sentence_for(plan.sleeve.value),
                "legs": [
                    {"seq": lg.seq, "role": lg.role.value, "symbol": lg.tradingsymbol,
                     "side": lg.side.value, "quantity": lg.quantity}
                    for lg in plan.legs
                ],
            }
        )  # fmt: skip
    positions = [
        {
            "session_id": int(r["session_id"]),
            "sleeve": str(r["sleeve"]),
            "entry_points": str(r["entry_points"]),
            "last_mark_points": None if r["last_mark_points"] is None else str(r["last_mark_points"]),
            "exit_plan_id": r["exit_plan_id"],
            "hard_exit_at": r["hard_exit_at"].isoformat() if r["hard_exit_at"] else None,
        }
        for r in store.conn.execute(
            f"SELECT p.session_id, s.sleeve, p.entry_points, p.last_mark_points, p.exit_plan_id, "
            f"p.hard_exit_at FROM {store.t('op_position')} p "
            f"JOIN {store.t('op_session')} s ON s.id = p.session_id "
            "WHERE p.user_id = ? AND p.closed_at IS NULL ORDER BY p.session_id",
            (store.user_id,),
        ).fetchall()
    ]
    return {"day": day.isoformat(), "plans": plans, "positions": positions}


def _view() -> dict[str, Any]:
    try:
        with open_store() as store:
            return {"ok": True, **build_view(store, now=_now())}
    except Exception as exc:  # the page must render, and say why it has nothing
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "plans": [], "positions": []}


def _templates() -> Any:  # noqa: ANN401 - Jinja2Templates, owned by app.main
    from . import main as _main  # noqa: PLC0415

    return _main.templates


@router.get("/nifty-options", response_class=HTMLResponse)
async def nifty_options_page(request: Request) -> HTMLResponse:
    return _templates().TemplateResponse(request, "nifty_options.html", {"v": _view()})


@router.get("/nifty-options/data")
async def nifty_options_data() -> JSONResponse:
    return JSONResponse(_view())


def _refusal(exc: _execute.Refused) -> JSONResponse:
    return JSONResponse({"code": exc.code, "detail": exc.message}, status_code=exc.status)


@router.post("/nifty-options/execute")
async def nifty_options_execute(
    plan_id: str = Form(...), confirm: str = Form(...)
) -> JSONResponse:
    """The one entry route. `confirm` must be the literal `true`; nothing reaches a store first."""
    if confirm != "true":
        return JSONResponse({"code": "CONFIRM_REQUIRED", "detail": "confirm must be true"}, 400)
    try:
        with open_store() as store:
            plan = store.plan(plan_id)
            sleeve = plan.sleeve if plan is not None else Sleeve.O2
            outcome = await _execute.execute_entry(
                store, gateway_for(sleeve), quotes=quotes(), plan_id=plan_id, confirm=True,
                mode_of=mode_of,
            )  # fmt: skip
    except _execute.Refused as exc:
        return _refusal(exc)
    return JSONResponse(_execute.outcome_json(outcome))


@router.post("/nifty-options/close")
async def nifty_options_close(
    session_id: int = Form(...), confirm: str = Form(...)
) -> JSONResponse:
    """**Close now** (MANUAL): a MANUAL exit plan, then the closes, shorts first."""
    if confirm != "true":
        return JSONResponse({"code": "CONFIRM_REQUIRED", "detail": "confirm must be true"}, 400)
    from .options_monitor import PgPositionStore  # noqa: PLC0415

    try:
        with open_store() as store:
            plan = _execute.entry_plan_for(store, session_id)
            if plan is None:
                return JSONResponse({"code": "UNKNOWN_SESSION", "detail": "no such position"}, 404)
            positions = PgPositionStore(store.conn, user_id=store.user_id, schema=store.schema)
            tracked = next(
                (t for t in positions.open_positions(plan.trade_date) if t.session_id == session_id),
                None,
            )
            if tracked is None:
                return JSONResponse({"code": "NOT_OPEN", "detail": "the position is not open"}, 409)
            now = _now()
            exit_id = tracked.exit_plan_id or positions.raise_exit(
                tracked, ExitVerdict("MANUAL", "RULE", None, None, False), now
            )
            outcome = await _execute.execute_exit(
                store, gateway_for(plan.sleeve), quotes=quotes(), plan=plan, exit_plan_id=exit_id,
                reason="MANUAL", mode=mode_of(plan.sleeve),
            )  # fmt: skip
    except _execute.Refused as exc:
        return _refusal(exc)
    return JSONResponse(_execute.outcome_json(outcome))


def _money(value: object) -> str:
    return "—" if value is None else str(Decimal(str(value)).quantize(Decimal("0.01")))
