"""The desk's clock for a swing session: the 10:45 cutoff and the 15:15 GTT sweep (SW11).

STANDING-ANSWERS A8: "Cancel any open remainder at 10:45. 15:15 sweep: no filled qty without
a GTT (`SWING_POSITION_NAKED`), re-arm if so." SW10.5 built both as functions on
``app.swing_execute`` and left them as a route (`POST /swing/cutoff`) and a hook
(`eod_gtt_sweep`) — "the sweeps need the desk's gateway, so it is a launchd/cron POST or a
desk timer, SW11's". This is the desk timer.

WHY IT IS THIS PROCESS
----------------------
Both chores are order-shaped — a cancel, a GTT — so by the second law they run through the
desk's gateway, and the gateway lives in the desk. A Beat entry in the data plant cannot do
either; it can only *check* afterwards (``baskfy_worker.tasks.swing_ops``, which raises the
alerts when this clock did not do its job). The monitor process (`app.swing_monitor`) is
already awake for the session and already knows the day, so it becomes the clock once the
strategy has stopped: `run_after_close` runs the cutoff at once and sleeps until the sweep.

WHAT IT HOLDS, AND WHEN
-----------------------
`app.swing_monitor` never hands the strategy a gateway (SW6.4). This module builds one — the
swing book's own, `app.swing_desk.swing_gateway` — only when a chore starts, after the
strategy is gone, and drops it when the chore is done. Nothing here names a placing verb; the
work is `swing_execute.cutoff_open_orders` and `swing_execute.eod_gtt_sweep`, exactly what
the page's buttons post to.

Every chore is recorded on the day's `sw_session.notes` and counted in the desk's metrics
(`desk_swing_sweeps_total`), and every failure is logged and swallowed *here* — a sweep that
raised would end the process before the 15:15 sweep it exists to reach.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG

from . import telemetry as _tel

log = logging.getLogger("swing_clock")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _count(sweep: str, outcome: str) -> None:
    """`desk_swing_sweeps_total`, guarded at the call site: a sink that raises through the
    helper is logged, and the chore goes on (SW11, B8)."""
    try:
        _tel.count("swing_sweeps", sweep=sweep, outcome=outcome)
    except Exception:  # noqa: BLE001
        log.debug("swing sweep metric unavailable", exc_info=True)


def _capture(exc: BaseException, **context: object) -> None:
    try:
        _tel.capture(exc, **context)
    except Exception:  # noqa: BLE001
        log.debug("swing error capture unavailable", exc_info=True)


@dataclass(frozen=True)
class SweepReport:
    """What the 15:15 sweep found and did."""

    naked_before: int
    armed: int
    still_naked: int
    outcomes: tuple[Any, ...]


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def sweep_time(day: dt.date) -> dt.datetime:
    hour, minute = DEFAULT_SWING_CONFIG.opening_range.gtt_sweep_at
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


async def run_cutoff(
    store: Any, gw: Any, *, orders: Any, now: dt.datetime
) -> Any:  # noqa: ANN401 - a CutoffReport
    """The 10:45 chore through `swing_execute.cutoff_open_orders`; counted, never raising."""
    from . import swing_execute  # noqa: PLC0415 - the sibling module, at call time

    try:
        report = await swing_execute.cutoff_open_orders(store, gw, orders=orders, now=now)
    except Exception as exc:  # noqa: BLE001 - the clock must reach 15:15 whatever 10:45 did
        log.error("10:45 cutoff failed: %s", exc)
        _count("cutoff", "failed")
        _capture(exc, sweep="cutoff")
        return None
    _count("cutoff", "ok")
    if report.cancel_failed:
        _count("cutoff", "cancel_refused")
    try:
        store.note_session(
            now.astimezone(IST).date(),
            f"cutoff {now.astimezone(IST):%H:%M}: {report.reconciled} reconciled, "
            f"{report.cancelled} cancelled, {report.cancel_failed} refused, "
            f"{report.slots_freed} slots freed",
        )
    except Exception as exc:  # noqa: BLE001 - the note is the record, not the work
        log.warning("could not note the cutoff on the session: %s", exc)
    return report


async def run_gtt_sweep(
    store: Any, gw: Any, *, now: dt.datetime, price_of: Callable[[str], Decimal | None]
) -> SweepReport:
    """The 15:15 chore (A8): every open position with shares and no GTT is re-armed through
    `swing_execute.eod_gtt_sweep`, at the last price the desk can read for it."""
    from . import swing_execute  # noqa: PLC0415

    naked = [p for p in store.open_positions()
             if p.get("gtt_id") is None and int(p.get("quantity_open") or 0) > 0]
    prices: dict[str, Decimal] = {}
    for pos in naked:
        try:
            price = price_of(str(pos["symbol"]))
        except Exception as exc:  # noqa: BLE001 - no price → the re-arm says BLOCKED, honestly
            log.warning("no last price for %s: %s", pos["symbol"], exc)
            price = None
        if price is not None:
            prices[str(pos["symbol"])] = price
    try:
        outcomes = await swing_execute.eod_gtt_sweep(store, gw, now=now, prices=prices)
    except Exception as exc:  # noqa: BLE001 - logged and counted; the alert reads the book
        log.error("15:15 GTT sweep failed: %s", exc)
        _count("gtt", "failed")
        _capture(exc, sweep="gtt")
        outcomes = []
    armed = sum(1 for o in outcomes if o.status in ("SIMULATED", "FILLED"))
    still = len([p for p in store.open_positions()
                 if p.get("gtt_id") is None and int(p.get("quantity_open") or 0) > 0])
    _count("gtt", "ok" if still == 0 else "still_naked")
    try:
        store.note_session(
            now.astimezone(IST).date(),
            f"gtt-sweep {now.astimezone(IST):%H:%M}: {len(naked)} naked, {armed} armed, "
            f"{still} still naked",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not note the sweep on the session: %s", exc)
    if still:
        log.error("15:15 sweep: %d position(s) still without a GTT — SWING_GTT_MISSING_AT_1515",
                  still)
    else:
        log.info("15:15 sweep: %d naked, %d armed, none left", len(naked), armed)
    return SweepReport(len(naked), armed, still, tuple(outcomes))


def run_after_close(  # noqa: PLR0913 - the clock's seams, for the tests
    *,
    day: dt.date,
    now: Callable[[], dt.datetime] = _now,
    sleep: Callable[[float], None] = time.sleep,
    open_store: Callable[[], Any] | None = None,
    gateway: Callable[[], Any] | None = None,
    orders: Callable[[], Any] | None = None,
    price_of: Callable[[str], Decimal | None] | None = None,
) -> int:
    """After the monitor: the cutoff now, then sleep to `gtt_sweep_at` and sweep. Exit 0.

    The seams default to the desk's own (`app.swing_desk`): the sole user's Postgres store,
    the swing gateway, the Kite order book and `Kite.ltp`. Each is built when its chore starts,
    inside the chore's own `with`, and nothing is held between them.
    """
    from . import swing_desk  # noqa: PLC0415 - the page module; at call time, like the store

    stores = open_store or swing_desk.open_store
    build_gw = gateway or swing_desk.swing_gateway
    order_source = orders or swing_desk.order_source
    prices = price_of or swing_desk.last_price

    def chore(name: str) -> Callable[[Callable[[Any, Any], Any]], None]:
        # A store or a gateway that cannot be built (no Kite session, no database) is logged
        # and counted, never raised: the 10:45 failure must not cost the 15:15 sweep.
        def run(body: Callable[[Any, Any], Any]) -> None:
            try:
                with stores() as store:
                    asyncio.run(body(store, build_gw()))
            except Exception as exc:  # noqa: BLE001
                log.error("clock: the %s chore could not run: %s", name, exc)
                _count(name, "unavailable")
                _capture(exc, sweep=name)

        return run

    moment = now()
    chore("cutoff")(
        lambda store, gw: run_cutoff(store, gw, orders=order_source(), now=moment)
    )
    target = sweep_time(day)
    wait = (target - now()).total_seconds()
    if wait > 0:
        log.info("clock: sleeping %.0f s until the %s GTT sweep", wait, target.time())
        sleep(wait)
    chore("gtt")(lambda store, gw: run_gtt_sweep(store, gw, now=now(), price_of=prices))
    return 0
