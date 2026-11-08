"""TWT auto-execute: the three-weeks-tight sleeve confirms its own MORNING plan at the open.

THE DECISION, AND WHOSE IT IS
-----------------------------
Maulik, in session, 21 Sep 2026, verbatim: *"TWT has no auto-execute flag - this should be
implemented auto execute"*. It is the **second** named exception to the desk's first
non-negotiable ("Never auto-execute"); the first is the swing sleeve's (DECISIONS-SW SW25/SW26).
Until that sentence every file in both trees said "there is no BASKFY_TWT_AUTO_EXECUTE and there
will not be one", and that was the decision until its owner changed it. DECISIONS-TW **TW17** is
the record, including the alternatives and how to reverse it.

The flag is ``BASKFY_TWT_AUTO_EXECUTE``. It **defaults false**, it exists **only in the desk**
(the web API and the worker have no such setting, and the web app still has no execute route),
and flipping it on the box is Maulik's hand alone. An agent may not default it true, widen what
it does, or add a third exception.

THE ONE THING TO UNDERSTAND
---------------------------
This removes the person pressing Confirm and **nothing else**. Every line is sent by the same
:func:`app.twt_execute.execute_line` the ``/twt/execute`` button reaches, with ``confirm="true"``,
the plan's own ``plan_id``, the same :func:`app.twt_desk.twt_gateway` and the same
:func:`app.twt_desk.last_price`. So every refusal still runs, in the same place:

* the plan's thirty-minute expiry (410), a line not ``PROPOSED`` (409), the line's kind (400);
* the ``tw_session`` row lock and the re-size of ``04`` §10.5 under it;
* ``04`` §6.3's three new entries a session (``SESSION_CAP``), ``ALREADY_HELD``, no capital;
* half size for the first ten live entries (``first_live_multiplier``) — a sizing rule inside
  ``_resize``, not a person's judgement, so it is unchanged;
* a halt: ``/twt/halt`` zeroes the capital and expires every plan, and both refuse here;
* ``client_id = plan_id:symbol`` and the gateway's idempotency map, so a re-run cannot
  double-send;
* the gateway's guards (non-negotiable 7), risk, rate limits and journal; CNC only;
* the GTT stop armed in the same request as the fill (non-negotiable 4).

WHAT IT RUNS, AND IN WHAT ORDER
-------------------------------
Only **today's MORNING plan** — ``twt-morning`` builds it at 09:05 and it expires at 09:35 — or,
since LV8 (28 Sep 2026, DECISIONS-TW **TW19**, Maulik's decision), **today's LIVE plan**: the one
the Scan button builds from today's provisional bar during the session, whose entries are "buy
now, at market" and which expires thirty minutes after it was built. This module never builds,
rebuilds or edits a plan: a missing, stale (built on another day), EVENING, or expired plan is
refused loudly and nothing is sent. The entry rule is **at market** (``04`` §5.1 for the open;
TW19 for the live scan), so the runner does nothing before 09:15 IST or after 15:30.

The session supervisor (LV4) calls :func:`drain_now` once a minute while the session is open and
a Kite session exists, which is how a LIVE plan built at 11:40 is confirmed at 11:40 rather than
waiting for a person. Every line still goes through ``execute_line`` — the same plan expiry,
gateway, guards, GTT stop, three-entries cap and first-ten half size as a click.

Lines are taken in **the plan's own order** (``baskfy_core.twt.plan.LINE_ORDER``):
``SELL_AT_OPEN`` (since LV9 — Qullamaggie's partial and MA-trail exit, Maulik's TW20 widening of
this exception to sells), then ``ARM_GTT``, then ``RAISE_GTT_STOP``, then ``BUY_AT_OPEN``,
alphabetical within a kind, which is how ``assemble`` sorted them and how the page shows them.
Risk comes off first on purpose: a sale, a re-arm or a raise protects or realises shares already
held and spends none of the session's three entries, so nothing that happens to a buy can delay
the book's protection. The planner has already capped the buys at three; the confirm-time
``SESSION_CAP`` is the second refusal, not the first.

FAIL SOFT, PER LINE
-------------------
A line that raises is logged, counted and skipped; the next line still goes. The line stays
``PROPOSED`` (or whatever ``execute_line`` wrote before raising), so a person can still confirm it
by hand from the desk page until the plan expires, exactly as before this module existed.

``python -m app.twt_auto`` exits 0 and does nothing when :func:`auto_execute_enabled` is false,
so the compose service and its loop can exist before the flag does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from fastapi import HTTPException

from . import config as C
from . import telemetry as _tel
from .core.guards import UntouchableInstrumentError

log = logging.getLogger("twt.auto")

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The open. ``04`` §5.1: the entry is the next session's open, at market.
MARKET_OPEN: Final = dt.time(9, 15)
#: The close. Past it a MARKET order is not an open-auction entry; the plan's own thirty-minute
#: expiry refuses long before this, and this is the belt beside those braces.
MARKET_CLOSE: Final = dt.time(15, 30)

#: The plan sources this runner acts on. ``EVENING`` is built for a person to read the night
#: before and has expired by morning; the 09:05 rebuild is sized against this morning's book, and
#: a LIVE plan (LV8) against the book at the minute the scan ran.
MORNING: Final = "MORNING"
LIVE: Final = "LIVE"
AUTO_SOURCES: Final[frozenset[str]] = frozenset({MORNING, LIVE})

#: How often the session supervisor's :func:`drain_now` looks for a LIVE plan. The plan lives
#: thirty minutes; a minute's latency spends none of it worth having.
DRAIN_EVERY_SECONDS: Final = 60

#: How long to wait for a Kite session before giving up, and how often to look. Ten minutes is
#: inside the plan's thirty: a login at 09:20 still trades at 09:20, and one at 09:40 does not
#: because the plan has expired by then anyway.
SESSION_WAIT_SECONDS: Final = 600
SESSION_POLL_SECONDS: Final = 30

#: The kinds this runner sends — exactly ``twt_execute.EXECUTABLE_KINDS``, restated so the
#: module can be imported without the execute module's broker-side imports. Asserted equal in
#: ``tests/test_twt_auto.py``.
AUTO_KINDS: Final[frozenset[str]] = frozenset(
    {"SELL_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "BUY_AT_OPEN"}
)


def live_execution() -> bool:
    """Whether a confirm would place a real order: the TWT flag on AND the desk not in DRY_RUN —
    the same truth table as :func:`app.twt_execute.twt_gates`, read at call time."""
    return bool(C.TWT_EXECUTION_ENABLED) and not bool(C.DRY_RUN)


def auto_execute_enabled() -> bool:
    """May the desk confirm TWT's morning plan by itself? All THREE flags, never fewer.

    ``DRY_RUN`` off, ``BASKFY_TWT_EXECUTION_ENABLED`` on, and ``BASKFY_TWT_AUTO_EXECUTE`` on.
    Going live and going unattended are separate decisions and separate switches, so either can
    be reversed without the other — the same shape as the swing sleeve's SW25. Read from the
    config module at call time so the answer cannot be stale in a long-lived process.
    """
    return live_execution() and bool(C.TWT_AUTO_EXECUTE)


# --- what to run --------------------------------------------------------------------------


@dataclass
class RunReport:
    """What one morning's run came to. ``line`` is the one-line summary the log ends with."""

    ran: bool = False
    reason: str = ""
    plan_id: str | None = None
    attempts: list[tuple[str, str, str]] = field(default_factory=list)

    def line(self) -> str:
        if not self.ran:
            return f"TWT AUTO: nothing sent — {self.reason}"
        counts: dict[str, int] = {}
        for _symbol, _kind, status in self.attempts:
            counts[status] = counts.get(status, 0) + 1
        tally = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no executable lines"
        detail = "; ".join(f"{s} {k} {st}" for s, k, st in self.attempts)
        return (
            f"TWT AUTO: plan {self.plan_id}: {len(self.attempts)} line(s) — {tally}"
            + (f" [{detail}]" if detail else "")
        )


def _aware(stamp: dt.datetime) -> dt.datetime:
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=dt.UTC)


def morning_plan(store: Any, now: dt.datetime) -> tuple[dict | None, str]:  # noqa: ANN401
    """Today's MORNING or LIVE plan, or ``None`` and the reason it will not be used.

    Never a rebuild. The newest plan at or before today is read exactly as the page reads it,
    and refused unless it is a ``MORNING`` or ``LIVE`` plan, built today (IST), and not yet
    expired. The name is historical: until LV8 only the 09:05 plan qualified.
    """
    today = now.astimezone(IST).date()
    plan = store.todays_plan(today)
    if plan is None:
        return None, "NO_PLAN: no TWT plan exists — did twt-morning run at 09:05, or a live scan?"
    if str(plan.get("source")) not in AUTO_SOURCES:
        return None, (
            f"NOT_MORNING: the newest plan {plan['plan_id']} is {plan.get('source')}, and only "
            f"the 09:05 MORNING rebuild or a LIVE scan's plan is sized against today's book"
        )
    built = plan.get("built_at")
    if built is None or _aware(built).astimezone(IST).date() != today:
        return None, (
            f"STALE_PLAN: plan {plan['plan_id']} was not built today ({built}); this runner "
            f"never builds a plan of its own"
        )
    expires = plan.get("expires_at")
    if expires is not None and _aware(now) >= _aware(expires):
        return None, (
            f"EXPIRED: plan {plan['plan_id']} expired at {expires.isoformat()}; nothing is sent "
            f"against an expired plan, by a person or by this"
        )
    return plan, ""


def executable_lines(store: Any, plan: dict) -> list[dict]:  # noqa: ANN401 - a PgTwtStore
    """The plan's ``PROPOSED`` executable lines, in the plan's own order (stops, then buys)."""
    return [
        line
        for line in store.lines_for(int(plan["id"]))
        if line["kind"] in AUTO_KINDS and line["state"] == "PROPOSED"
    ]


def session_closed(store: Any, day: dt.date) -> bool:  # noqa: ANN401 - a PgTwtStore
    """True only when the trading calendar says ``day`` is **not** a session.

    An exchange holiday that falls on a weekday is the case: ``twt-morning`` builds a plan on it
    anyway. Unknown (no row, no table) is *not* closed — a missing calendar row must not silently
    switch the sleeve off — and is logged. The broker refuses a market order on a closed day in
    any case; this is so the runner does not ask.
    """
    try:
        row = store.conn.execute(
            f"SELECT is_trading_day FROM {store.t('trading_day')} WHERE date = ?", (day,)
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - a calendar we cannot read is "unknown"
        log.warning("TWT AUTO: trading calendar unreadable for %s (%s); treating as open", day, exc)
        return False
    if row is None:
        log.warning("TWT AUTO: no trading_day row for %s; treating as open", day)
        return False
    return not bool(row["is_trading_day"] if hasattr(row, "keys") else row[0])


# --- the drain ----------------------------------------------------------------------------


ExecuteFn = Callable[..., Awaitable[Any]]


async def drain_plan(  # noqa: PLR0913 - the plan's collaborators, named
    store: Any,  # noqa: ANN401 - a PgTwtStore
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    plan: dict,
    *,
    now: Callable[[], dt.datetime],
    price_for: Callable[[str], Decimal | None],
    execute: ExecuteFn | None = None,
) -> list[tuple[str, str, str]]:
    """Send each ``PROPOSED`` line of ``plan`` through ``execute_line``, once, in order.

    Refuses to do anything unless :func:`auto_execute_enabled` says so **at call time** — the
    check sits here as well as in :func:`run_once` so no caller can reach the order path around
    it. Returns ``(symbol, kind, status)`` per attempt.
    """
    if not auto_execute_enabled():
        return []
    if execute is None:
        from . import twt_execute  # noqa: PLC0415 - the broker-side module, only when on

        execute = twt_execute.execute_line
    done: list[tuple[str, str, str]] = []
    for line in executable_lines(store, plan):
        symbol, kind = str(line["symbol"]), str(line["kind"])
        try:
            outcome = await execute(
                store,
                gateway,
                plan_id=str(plan["plan_id"]),
                line_id=int(line["id"]),
                confirm="true",
                now=now(),
                last_price=price_for(symbol),
            )
        except HTTPException as exc:
            status = f"REFUSED_{exc.status_code}"
            log.warning("TWT AUTO %s %s: %s — %s", symbol, kind, status, exc.detail)
        except UntouchableInstrumentError as exc:
            status = "BLOCKED"
            log.warning("TWT AUTO %s %s: BLOCKED by a guard — %s", symbol, kind, exc)
        except Exception as exc:  # noqa: BLE001 - one bad line must not cost the rest
            status = "ERROR"
            log.exception(
                "TWT AUTO %s %s: %s — the line can still be confirmed by hand",
                symbol,
                kind,
                type(exc).__name__,
            )
        else:
            status = str(outcome.status)
            reason = getattr(outcome, "reason", "")
            log.warning("TWT AUTO %s %s: %s%s", symbol, kind, status, f" — {reason}" if reason else "")
        _tel.count("twt_auto_execute", outcome=status)
        done.append((symbol, kind, status))
    return done


# --- one morning --------------------------------------------------------------------------


def _wait_for_session(
    authed: Callable[[], bool],
    *,
    sleep: Callable[[float], None],
    wait_seconds: float,
    poll_seconds: float,
) -> bool:
    waited = 0.0
    while True:
        try:
            if authed():
                return True
        except Exception as exc:  # noqa: BLE001 - a failed check is "not yet"
            log.warning("TWT AUTO: Kite session check failed: %s", exc)
        if waited >= wait_seconds:
            return False
        log.info("TWT AUTO: no Kite session yet; retrying in %.0fs", poll_seconds)
        sleep(poll_seconds)
        waited += poll_seconds


def _desk_authed() -> bool:
    from . import main as _main  # noqa: PLC0415 - the desk's one Kite, at call time

    return bool(_main.kite().is_authed())


def run_once(  # noqa: PLR0913 - every collaborator is a seam a test needs
    *,
    now: Callable[[], dt.datetime] | None = None,
    open_store: Callable[[], Any] | None = None,
    gateway: Callable[[], Any] | None = None,
    price_for: Callable[[str], Decimal | None] | None = None,
    authed: Callable[[], bool] | None = None,
    execute: ExecuteFn | None = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = SESSION_WAIT_SECONDS,
    poll_seconds: float = SESSION_POLL_SECONDS,
) -> RunReport:
    """One morning. Every refusal before the drain is a ``RunReport`` with a reason, never a raise."""
    clock = now or (lambda: dt.datetime.now(tz=IST))
    report = RunReport()

    def _refuse(reason: str, outcome: str) -> RunReport:
        report.reason = reason
        _tel.count("twt_auto_runs", outcome=outcome)
        return report

    if not auto_execute_enabled():
        return _refuse(
            "auto-execute is off (needs DRY_RUN=false, BASKFY_TWT_EXECUTION_ENABLED=true and "
            "BASKFY_TWT_AUTO_EXECUTE=true)",
            "off",
        )
    stamp = clock().astimezone(IST)
    if stamp.weekday() > 4:  # noqa: PLR2004 - Saturday is 5
        return _refuse(f"{stamp:%a} is not a weekday", "not_a_session")
    if stamp.time() < MARKET_OPEN or stamp.time() >= MARKET_CLOSE:
        return _refuse(
            f"{stamp:%H:%M} IST is outside the session; the entry is at market inside it "
            "(04 §5.1, TW19)",
            "outside_session",
        )

    if open_store is None or gateway is None or price_for is None:
        from . import twt_desk  # noqa: PLC0415 - the desk's own collaborators, at call time

        open_store = open_store or twt_desk.open_store
        gateway = gateway or twt_desk.twt_gateway
        price_for = price_for or twt_desk.last_price

    if not _wait_for_session(
        authed or _desk_authed, sleep=sleep, wait_seconds=wait_seconds, poll_seconds=poll_seconds
    ):
        log.error("TWT AUTO: no Kite session after %.0fs; nothing sent", wait_seconds)
        return _refuse(f"no Kite session after {wait_seconds:.0f}s", "no_session")

    with open_store() as store:
        if session_closed(store, stamp.date()):
            return _refuse(f"{stamp.date()} is not a trading session", "not_a_session")
        plan, why = morning_plan(store, clock())
        if plan is None:
            log.error("TWT AUTO: %s", why)
            return _refuse(why, "no_plan")
        report.plan_id = str(plan["plan_id"])
        report.attempts = asyncio.run(
            drain_plan(
                store, gateway(), plan, now=clock, price_for=price_for, execute=execute
            )
        )
    report.ran = True
    _tel.count("twt_auto_runs", outcome="ran")
    return report


def drain_now(now: dt.datetime | None = None) -> RunReport:
    """LV8: one pass for the session supervisor — today's MORNING or LIVE plan, sent now.

    :func:`run_once` with no wait for a Kite session (the supervisor only calls this when it has
    seen one) and no sleep. Every refusal is a ``RunReport`` with its reason; nothing raises to
    the supervisor's tick. With any of the three flags off it does nothing, like everything else
    in this module.
    """
    clock = (lambda: now) if now is not None else None
    return run_once(now=clock, wait_seconds=0, poll_seconds=0, sleep=lambda _s: None)


def main(argv: Iterable[str] | None = None) -> int:  # noqa: ARG001 - no arguments, on purpose
    """``python -m app.twt_auto`` — one morning's run. Exits 0 when the flag is off."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not auto_execute_enabled():
        log.info(
            "TWT auto-execute is off (DRY_RUN=%s, TWT_EXECUTION_ENABLED=%s, TWT_AUTO_EXECUTE=%s); "
            "exiting without reading a plan",
            C.DRY_RUN,
            C.TWT_EXECUTION_ENABLED,
            C.TWT_AUTO_EXECUTE,
        )
        return 0
    _tel.install(metrics_port=0)
    report = run_once()
    log.warning("%s", report.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
