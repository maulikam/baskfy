"""One visible lifecycle per trade, across the three sleeves, and the explicit adoption of a
holding bought by hand (LV6 — review P1.4, P2.3).

WHAT THE PAGE ANSWERS
---------------------
For every open position the swing, TWT and VBT books hold: how many shares filled and are still
open, the entry, the initial and current stop, the rupees at risk between entry and stop, what the
**broker** says about the stop — resting for the right quantity (``ARMED``), not resting at all
(``NAKED``), gone from the broker's list (``GTT_MISSING``), resting for more or fewer shares than
are held (``GTT_OVERSIZED`` / ``GTT_UNDERSIZED``), fired without the shares leaving the book
(``TRIGGERED_UNFILLED``), or not checkable because there is no Kite session (``UNVERIFIED``) — the
exact exit rule the sleeve runs, the next action, whether it is overdue, and every open finding the
reconciler (LV2) has recorded against it. A trade can be followed from entry to confirmed exit
with no orphan position, no over-sized residual GTT and no unexplained discrepancy left silent.

A GTT that fired is not a fill (Kite's GTT places a LIMIT order); the row says so and stays
unresolved until the position closes.

ADOPTION
--------
``POST /lifecycle/adopt`` with ``confirm=true`` puts a holding Maulik bought in the Kite app into a
sleeve's book **on his stated cost and quantity**, links the GTT he armed by hand or arms one through
that sleeve's own helper (the gateway; non-negotiable 4), and records the act in ``lv_adoption``. It
never claims a holding silently: a name the sleeve already holds is refused, an unknown symbol is
refused, a missing ``confirm`` writes nothing. The swing book's schema wants a setup and a trail,
so those are named on the form; the TWT and VBT books need only the numbers.

The page is read-only apart from that one route. Nothing here places a buy, sells, or cancels a
stop; the only broker write it can cause is a GTT armed for shares the person says he holds.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG

from . import exit_rules as _exit_rules
from . import reconcile as _reconcile

log = logging.getLogger("lifecycle")
router = APIRouter()

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
SLEEVES: Final[tuple[str, ...]] = ("swing", "twt", "vbt")

ARMED: Final = "ARMED"
NAKED: Final = "NAKED"
GTT_MISSING: Final = "GTT_MISSING"
GTT_OVERSIZED: Final = "GTT_OVERSIZED"
GTT_UNDERSIZED: Final = "GTT_UNDERSIZED"
TRIGGERED_UNFILLED: Final = "TRIGGERED_UNFILLED"
UNVERIFIED: Final = "UNVERIFIED"
STOP_STATES: Final[frozenset[str]] = frozenset(
    {ARMED, NAKED, GTT_MISSING, GTT_OVERSIZED, GTT_UNDERSIZED, TRIGGERED_UNFILLED, UNVERIFIED}
)

#: A finding open longer than this is overdue — long enough for the reconciler to have looked
#: several times and a person to have seen the page once.
OVERDUE_AFTER: Final = dt.timedelta(minutes=30)

_ZERO: Final = Decimal(0)


#: The exact exit rule each sleeve runs (``app.exit_rules``): the same sentence the sleeve
#: pages print under their books, so the two cannot disagree.
EXIT_RULES: Final[dict[str, str]] = _exit_rules.EXIT_RULES


@dataclass(frozen=True)
class TradeLifecycle:
    sleeve: str
    position_id: int
    symbol: str
    entry_date: dt.date | None
    entry_avg: Decimal
    filled_qty: int
    open_qty: int
    initial_stop: Decimal | None
    stop: Decimal | None
    rupee_risk: Decimal
    stop_state: str
    stop_qty: int | None
    gtt_id: str | None
    exit_rule: str
    next_action: str
    overdue: bool
    simulated: bool
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> bool:
        return self.stop_state == ARMED and not self.issues


def _dec(value: Any) -> Decimal | None:  # noqa: ANN401 - a store value
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _stop_of(position: dict) -> Decimal | None:
    """TWT and VBT say ``stop_price``; the swing book says ``stop``."""
    return _dec(position.get("stop_price", position.get("stop")))


def judge_stop(
    position: dict, gtts: dict[str, _reconcile.GttRow] | None
) -> tuple[str, int | None]:
    """The broker's word on one position's stop, and the quantity it rests for."""
    gtt_id = position.get("gtt_id")
    open_qty = int(position.get("quantity_open") or 0)
    if gtt_id is None:
        return NAKED, None
    if str(gtt_id).startswith(_reconcile.SIMULATED_GTT_PREFIX):
        return ARMED, open_qty  # a rehearsal's trigger: nothing rests, nothing is missing
    if gtts is None:
        return UNVERIFIED, None
    row = gtts.get(str(gtt_id))
    if row is None or row.status in _reconcile.GTT_GONE_STATUSES:
        return GTT_MISSING, None
    if row.status == _reconcile.GTT_TRIGGERED:
        return TRIGGERED_UNFILLED, row.quantity
    if row.quantity is not None and row.quantity > open_qty:
        return GTT_OVERSIZED, row.quantity
    if row.quantity is not None and row.quantity < open_qty:
        return GTT_UNDERSIZED, row.quantity
    return ARMED, row.quantity if row.quantity is not None else open_qty


NEXT_ACTION: Final[dict[str, str]] = {
    ARMED: "hold; the sleeve manages the exit",
    NAKED: "arm the stop (Re-arm on the sleeve page)",
    GTT_MISSING: "the broker no longer lists the stop — re-arm it, or record why it went",
    GTT_OVERSIZED: "re-size the GTT down to the open quantity, or it sells what is not there",
    GTT_UNDERSIZED: "re-size the GTT up to the open quantity; part of the position is unprotected",
    TRIGGERED_UNFILLED: "the stop fired and the shares are still here — check the LIMIT order it placed, sell by hand if it lapsed",
    UNVERIFIED: "log in to Kite so the stop can be checked against the broker",
}


def build_rows(
    positions: dict[str, list[dict]],
    *,
    gtts: dict[str, _reconcile.GttRow] | None,
    issues: list[dict],
    now: dt.datetime,
) -> list[TradeLifecycle]:
    """Every open position across the sleeves as one row, unresolved ones first."""
    by_position: dict[tuple[str, int], list[dict]] = {}
    for issue in issues:
        by_position.setdefault((str(issue["sleeve"]), int(issue["position_id"])), []).append(issue)
    rows: list[TradeLifecycle] = []
    for sleeve in SLEEVES:
        for p in positions.get(sleeve, []):
            open_qty = int(p.get("quantity_open") or 0)
            if open_qty <= 0:
                continue
            entry = _dec(p.get("entry_avg")) or _ZERO
            stop = _stop_of(p)
            initial = _dec(p.get("initial_stop"))
            risk = (entry - stop) * open_qty if stop is not None and stop < entry else _ZERO
            state, stop_qty = judge_stop(p, gtts)
            mine = by_position.get((sleeve, int(p["id"])), [])
            oldest = min((i["seen_at"] for i in mine if i.get("seen_at") is not None), default=None)
            overdue = state != ARMED and oldest is not None and now - oldest >= OVERDUE_AFTER
            next_action = NEXT_ACTION[state]
            if mine and state == ARMED:
                next_action = "resolve the reconciler's finding(s) — see the issue column"
            rows.append(
                TradeLifecycle(
                    sleeve=sleeve,
                    position_id=int(p["id"]),
                    symbol=str(p.get("symbol") or f"instrument:{p.get('instrument_id')}"),
                    entry_date=p.get("entry_date"),
                    entry_avg=entry,
                    filled_qty=int(p.get("quantity_entered") or open_qty),
                    open_qty=open_qty,
                    initial_stop=initial,
                    stop=stop,
                    rupee_risk=risk.quantize(Decimal("0.01")),
                    stop_state=state,
                    stop_qty=stop_qty,
                    gtt_id=None if p.get("gtt_id") is None else str(p["gtt_id"]),
                    exit_rule=EXIT_RULES[sleeve],
                    next_action=next_action,
                    overdue=bool(overdue),
                    simulated=bool(p.get("simulated")),
                    issues=tuple(f"{i['kind']}: {i['detail']}" for i in mine),
                )
            )
    rows.sort(key=lambda r: (r.resolved, not r.overdue, r.sleeve, r.symbol))
    return rows


# --- adoption ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Adoption:
    sleeve: str
    symbol: str
    quantity: int
    avg_cost: Decimal
    gtt_id: str | None
    setup: str | None
    note: str | None


def _instrument_id(store: Any, symbol: str) -> int | None:  # noqa: ANN401 - a sleeve store
    row = store.conn.execute(
        f"SELECT id FROM {store.t('instrument')} WHERE symbol = ? LIMIT 1", (symbol,)
    ).fetchone()
    return None if row is None else int(row["id"])


def _record_adoption(store: Any, *, position_id: int, adoption: Adoption, gtt_id: str | None) -> None:  # noqa: ANN401
    store.conn.execute(
        f"INSERT INTO {store.t('lv_adoption')} "
        "(user_id, sleeve, position_id, symbol, quantity, avg_cost, gtt_id, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            store.user_id,
            adoption.sleeve,
            int(position_id),
            adoption.symbol,
            int(adoption.quantity),
            str(adoption.avg_cost),
            gtt_id,
            adoption.note,
        ),
    )


def _stop_pct(sleeve: str) -> Decimal:
    if sleeve == "twt":
        return Decimal(str(DEFAULT_TWT_CONFIG.exits.stop_pct))
    if sleeve == "vbt":
        return Decimal(str(DEFAULT_VBT_CONFIG.exits.stop_pct))
    return Decimal(str(DEFAULT_SWING_CONFIG.stops.max_stop_distance_pct))


def _position_fields(sleeve: str, adoption: Adoption, *, instrument_id: int, stop: Decimal, day: dt.date, adj_factor: Decimal) -> dict:
    common = {
        "instrument_id": instrument_id,
        "entry_date": day,
        "entry_avg": adoption.avg_cost,
        "quantity_entered": adoption.quantity,
        "quantity_open": adoption.quantity,
        "initial_stop": stop,
        "state": "OPEN",
        "simulated": False,
    }
    if sleeve == "twt":
        return {
            **common,
            "signal_date": day,
            "entry_adj_factor": adj_factor,
            "stop_price": stop,
            "high_since": adoption.avg_cost,
            "high_since_date": day,
        }
    if sleeve == "vbt":
        return {**common, "stop_price": stop}
    return {
        **common,
        "symbol": adoption.symbol,
        "setup": adoption.setup or "FLAG",
        "stop": stop,
        "trail": "MA20",
        "partial_done": False,
        "half_risk": False,
    }


async def adopt(  # noqa: PLR0913 - an adoption is its store, its gateway and the person's numbers
    store: Any,  # noqa: ANN401 - the sleeve's store
    gateway: Any,  # noqa: ANN401 - the sleeve's gateway
    adoption: Adoption,
    *,
    confirm: str,
    now: dt.datetime,
    gtts: dict[str, _reconcile.GttRow] | None,
    arm: Callable[..., Any],
) -> dict[str, Any]:
    """A hand-bought holding becomes a position in ``adoption.sleeve``'s book, protected.

    ``arm`` is the sleeve's own GTT helper (``twt_execute._arm`` / ``vbt_execute._arm_stop`` /
    ``swing_execute._arm``), through the gateway, used only when no ``gtt_id`` is given. With one,
    the resting trigger is linked as it is — the person armed it, the book records it.
    """
    if str(confirm).lower() != "true":
        raise HTTPException(400, "Adopting a holding requires explicit confirmation.")
    if adoption.sleeve not in SLEEVES:
        raise HTTPException(400, f"unknown sleeve {adoption.sleeve!r}")
    if adoption.quantity <= 0 or adoption.avg_cost <= _ZERO:
        raise HTTPException(400, "quantity and average cost must be positive")
    if adoption.sleeve == "swing" and adoption.setup not in ("FLAG", "EP"):
        raise HTTPException(400, "a swing adoption names its setup: FLAG or EP")
    instrument_id = _instrument_id(store, adoption.symbol)
    if instrument_id is None:
        raise HTTPException(404, f"{adoption.symbol} is not an instrument this desk knows")
    if store.open_position_for(instrument_id) is not None:
        raise HTTPException(409, f"the {adoption.sleeve} book already holds {adoption.symbol}")
    day = now.astimezone(IST).date()
    pct = _stop_pct(adoption.sleeve)
    stop = (adoption.avg_cost * (Decimal(100) - pct) / Decimal(100)).quantize(Decimal("0.05"))
    linked: str | None = None
    if adoption.gtt_id:
        row = gtts.get(str(adoption.gtt_id)) if gtts is not None else None
        if row is not None and row.trigger is not None:
            stop = row.trigger
        linked = str(adoption.gtt_id)
    adj = store.adj_factor(instrument_id, day) if hasattr(store, "adj_factor") else Decimal(1)
    fields = _position_fields(adoption.sleeve, adoption, instrument_id=instrument_id, stop=stop, day=day, adj_factor=adj)
    position_id = int(store.create_position(fields))
    store.add_fill(
        {
            "position_id": position_id,
            **({"order_id": None} if adoption.sleeve != "swing" else {}),
            "side": "BUY",
            "quantity": adoption.quantity,
            "price": adoption.avg_cost,
            "filled_at": now,
            "simulated": False,
            **({"journal_ref": f"adopted:{position_id}"} if adoption.sleeve == "swing" else {}),
        }
    )
    outcome: dict[str, Any] = {"position_id": position_id, "sleeve": adoption.sleeve, "stop": str(stop)}
    if linked is not None:
        store.update_position(position_id, {"gtt_id": linked, "gtt_trigger": stop, "gtt_armed_at": now})
        outcome["gtt"] = {"linked": linked}
    else:
        gtt = await arm(gateway, symbol=adoption.symbol, qty=adoption.quantity, stop=stop, last_price=adoption.avg_cost, position_id=position_id, now=now)
        outcome["gtt"] = gtt
        placed = str((gtt or {}).get("status") or "")
        if placed.startswith("GTT_PLACED") or placed.startswith("DRY_RUN"):
            gtt_id = str((gtt or {}).get("gtt_id") or f"{_reconcile.SIMULATED_GTT_PREFIX}adopt:{position_id}")
            store.update_position(position_id, {"gtt_id": gtt_id, "gtt_trigger": stop, "gtt_armed_at": now})
            linked = gtt_id
        else:
            outcome["naked"] = True
    _record_adoption(store, position_id=position_id, adoption=adoption, gtt_id=linked)
    log.warning("adopted %s x%d into %s at %s, stop %s, gtt %s", adoption.symbol, adoption.quantity, adoption.sleeve, adoption.avg_cost, stop, linked)
    return outcome


# --- the sleeves' arm helpers, one signature -------------------------------------------------


async def _arm_twt(gateway: Any, *, symbol: str, qty: int, stop: Decimal, last_price: Decimal, position_id: int, now: dt.datetime) -> dict:  # noqa: ANN401, PLR0913
    from . import twt_execute  # noqa: PLC0415

    return await twt_execute._arm(gateway, symbol=symbol, qty=qty, trigger=stop, last_price=last_price, client_id=f"adopt:{position_id}:{symbol}:ARM_GTT")  # noqa: SLF001


async def _arm_vbt(gateway: Any, *, symbol: str, qty: int, stop: Decimal, last_price: Decimal, position_id: int, now: dt.datetime) -> dict:  # noqa: ANN401, PLR0913
    del position_id, now
    result = await gateway.place_gtt_stop(
        symbol=symbol, qty=int(qty), trigger=float(stop), last_price=float(last_price),
        limit_fraction=DEFAULT_VBT_CONFIG.exits.gtt_limit_fraction,
        tenant=_tenant(), plan_tenant=_tenant(),
    )
    return dict(result)


async def _arm_swing(gateway: Any, *, symbol: str, qty: int, stop: Decimal, last_price: Decimal, position_id: int, now: dt.datetime) -> dict:  # noqa: ANN401, PLR0913
    from . import swing_execute  # noqa: PLC0415

    del now
    return await swing_execute._arm(gateway, symbol=symbol, qty=qty, stop=stop, last_price=last_price, client_id=f"adopt:{position_id}:{symbol}:GTT")  # noqa: SLF001


ARM: Final[dict[str, Callable[..., Any]]] = {"twt": _arm_twt, "vbt": _arm_vbt, "swing": _arm_swing}


def _tenant() -> Any:  # noqa: ANN401 - TenantIds
    from . import vbt_execute  # noqa: PLC0415

    return vbt_execute._tenant()  # noqa: SLF001


# --- the desk's wiring --------------------------------------------------------------------------


@dataclass
class Sources:
    """Where the page reads: one store and one gateway per sleeve, the GTT list, the issues."""

    stores: dict[str, Any]
    gateways: dict[str, Callable[[], Any]]
    gtts: dict[str, _reconcile.GttRow] | None
    issues: list[dict]


@contextlib.contextmanager
def desk_sources() -> Iterator[Sources]:
    from . import config as C  # noqa: PLC0415
    from . import main as _main  # noqa: PLC0415
    from . import swing_desk, twt_desk, vbt_desk  # noqa: PLC0415
    from .analytics import db as _db  # noqa: PLC0415

    with (
        swing_desk.open_store() as swing_store,
        twt_desk.open_store() as twt_store,
        vbt_desk.open_store() as vbt_store,
        _db.connect() as conn,
    ):
        schema = "public" if _db.DB_BACKEND == "postgres" else ""
        gtts: dict[str, _reconcile.GttRow] | None
        try:
            gtts = _reconcile.gtt_index(_main.kite().get_gtts())
        except Exception as exc:  # noqa: BLE001 - no session: the stops are UNVERIFIED, said so
            log.warning("no GTT list from the broker: %s", exc)
            gtts = None
        issues = _reconcile.PgIssueStore(conn, user_id=C.SOLE_USER_ID, schema=schema).open_issues()
        yield Sources(
            stores={"swing": swing_store, "twt": twt_store, "vbt": vbt_store},
            gateways={"swing": swing_desk.swing_gateway, "twt": twt_desk.twt_gateway, "vbt": vbt_desk.vbt_gateway},
            gtts=gtts,
            issues=issues,
        )


def _now() -> dt.datetime:
    return dt.datetime.now(tz=IST)


def build_view() -> dict[str, Any]:
    now = _now()
    try:
        with desk_sources() as src:
            positions = {name: store.open_positions() for name, store in src.stores.items()}
            rows = build_rows(positions, gtts=src.gtts, issues=src.issues, now=now)
            return {
                "rows": rows,
                "unresolved": sum(1 for r in rows if not r.resolved),
                "gtt_checked": src.gtts is not None,
                "issues": src.issues,
                "rules": EXIT_RULES,
                "now": now,
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001 - the page reports the failure, it does not 500
        log.warning("lifecycle view unavailable: %s", exc)
        return {"rows": [], "unresolved": 0, "gtt_checked": False, "issues": [], "rules": EXIT_RULES, "now": now, "error": str(exc)}


def _templates() -> Any:  # noqa: ANN401 - Jinja2Templates, owned by app.main
    from . import main as _main  # noqa: PLC0415

    return _main.templates


@router.get("/lifecycle", response_class=HTMLResponse)
def lifecycle_page(request: Request) -> Any:  # noqa: ANN401 - a TemplateResponse
    return _templates().TemplateResponse(request, "lifecycle.html", {"v": build_view()})


@router.get("/lifecycle/data")
def lifecycle_data() -> Any:  # noqa: ANN401 - JSON
    from .twt_desk import _jsonable  # noqa: PLC0415 - the desk's JSON coercion, one copy

    view = build_view()
    return _jsonable({**view, "rows": [r.__dict__ for r in view["rows"]]})


@router.post("/lifecycle/adopt")
async def lifecycle_adopt(  # noqa: PLR0913 - the form's fields
    sleeve: str = Form(...),
    symbol: str = Form(...),
    quantity: int = Form(...),
    avg_cost: Decimal = Form(...),
    confirm: str = Form(""),
    gtt_id: str = Form(""),
    setup: str = Form(""),
    note: str = Form(""),
) -> Any:  # noqa: ANN401 - a redirect
    adoption = Adoption(
        sleeve=sleeve.strip().lower(),
        symbol=symbol.strip().upper(),
        quantity=int(quantity),
        avg_cost=avg_cost,
        gtt_id=gtt_id.strip() or None,
        setup=setup.strip().upper() or None,
        note=note.strip() or None,
    )
    with desk_sources() as src:
        store = src.stores.get(adoption.sleeve)
        if store is None:
            raise HTTPException(400, f"unknown sleeve {adoption.sleeve!r}")
        outcome = await adopt(
            store, src.gateways[adoption.sleeve](), adoption, confirm=confirm, now=_now(), gtts=src.gtts, arm=ARM[adoption.sleeve]
        )
    log.info("adoption: %s", outcome)
    return RedirectResponse("/lifecycle", status_code=303)
