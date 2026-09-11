"""TW6 — the three-weeks-tight sleeve's execute logic: one confirmed line becomes an order.

``app/twt_desk.py`` owns the routes and the Postgres store; this module owns what happens
between "Confirm" and the book. It is written against the :class:`TwtStore` protocol so the same
code runs over the desk's Postgres on a weekday morning and over an in-memory dict in a test,
and it is **handed** its gateway rather than building one, so a test can prove the whole path
with a broker client that explodes on contact.

THE RULES IT ENFORCES, AND WHERE THEY COME FROM
-----------------------------------------------
* **Never auto-execute** (non-negotiable 1; ``docs/twt/02`` Track C §3). Nothing here runs
  without ``confirm="true"``, a ``plan_id`` issued in the last thirty minutes and a line still
  ``PROPOSED``; the refusals are 400 / 404 / 410 / 409. **There is no
  ``BASKFY_TWT_AUTO_EXECUTE`` and there will not be one** — non-negotiable 1's named exception
  belongs to the swing sleeve, by Maulik's own hand, and this sleeve's ratchet, which would
  dearly like to fire on ten lines a night for months, is a plan line a person confirms.
* **Every buy gets a GTT stop the same session** (non-negotiable 4; ``04`` §7.1, §7.4). The arm
  is in the *same request* as the fill that created it, which is what makes it one failure
  rather than two — and on this sleeve it is also the fill-day rule, because ``STOP_DAY0`` is
  modelled in the live book by nothing else.
* **A stop never falls** (``04`` §7.2). A ``RAISE_GTT_STOP`` at or below the resting trigger is
  ``BLOCKED``, and so is one at or above the last price, which would fire the moment it is
  armed. This is the third of the three places that say so; the first is the ``max`` inside the
  ratchet's arithmetic and the second is ``exit_lines``.
* **CNC only** (non-negotiable 5). :func:`twt_gates` hands the gateway ``intraday_enabled=False``
  and ``options_enabled=False`` unconditionally, whatever the weekly desk is allowed to do.
* **Everything through the gateway**, with ``client_id = plan_id:symbol`` for an order and
  ``plan_id:symbol:kind`` for a GTT leg (non-negotiable 6, ``04`` §10.4), so a re-posted plan
  cannot double-send before the gateway's own idempotency map is consulted.
* **It never sells what it did not buy** (``02`` Track C §5). Every position this module touches
  is a ``tw_position`` row; the broker's holdings are not consulted and cannot be.
* **Track B**: with ``BASKFY_TWT_EXECUTION_ENABLED=false`` the *same* path runs through the
  gateway's dry-run branch and records ``simulated=true`` whatever ``DRY_RUN`` says. There is no
  second branch for paper trading.

THE TWO HALVES OF THE RATCHET, AND WHY THE SECOND ONE IS NAMED
---------------------------------------------------------------
``RAISE_GTT_STOP`` is delete-and-replace — the swing book's path, reused rather than rewritten.
Both halves fail differently:

* **The cancel fails.** The old trigger is still resting, so the new one is **not** placed: two
  triggers sell the position twice. That is a stop one session behind, which is a nuisance.
* **The cancel succeeds and the arm fails.** The line is **NAKED**. The intent (the raised stop)
  is recorded so the next re-arm rests it at the right level, ``gtt_id`` is nulled so the page
  and the sweep can both see it, and the outcome is ``BLOCKED`` naming the position. Never
  swallowed: this is the one that costs money.

WHAT IS NOT SHARED WITH TW5, AND WHY
------------------------------------
``04`` §6.4's countdown is spent here, on a fill, and :func:`baskfy_api.twt_sleeve.
count_first_live_entry` is where that rule is written — but it takes a SQLAlchemy ``AsyncSession``
and this process has no async Postgres driver at all. The *sizing* half is shared for real
(:func:`baskfy_core.twt.sizing.first_live_multiplier`, imported); the *spending* half is
:meth:`TwtStore.count_first_live_entry`, which reproduces TW5.3's three refusals against the
same two columns. The same argument ``vbt_desk`` makes about the rescan constants, and
DECISIONS-TW **TW6.3** records it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from baskfy_execution.gtt import (
    DRY_RUN_GTT_DELETE,
    GTT_DELETED_STATUSES,
    GTT_PLACED,
    GTT_PLACED_STATUSES,
    StopBand,
)
from baskfy_execution.tenancy import TenantIds
from fastapi import HTTPException

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TICK_INR
from baskfy_core.twt.exits import initial_stop
from baskfy_core.twt.plan import LineKind
from baskfy_core.twt.sizing import SizeRefusal, first_live_multiplier, size_entry
from baskfy_core.twt.sleeve import MarkSource, OpenPositionValue, sleeve_value

from . import config as C
from .core import gateway as _gateway_module
from .core.gateway import OrderGateway, ProductGates

log = logging.getLogger("twt.execute")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

BUY_AT_OPEN = LineKind.BUY_AT_OPEN.value
ARM_GTT = LineKind.ARM_GTT.value
RAISE_GTT_STOP = LineKind.RAISE_GTT_STOP.value
SELL_AT_OPEN = LineKind.SELL_AT_OPEN.value

#: The kinds a confirm may act on — **three, and ``SELL_AT_OPEN`` is not one of them.**
#:
#: The kind exists in the schema so a person can be given a line for a ``MANUAL`` exit without a
#: migration (``03`` §7), and ``04`` §10.2 says no TWT rule ever emits one. A route that could
#: execute it would be an end-of-day sell rule this strategy does not have — the GTT is the exit
#: (``01`` §5) — so the refusal is here as well as in the planner, and TW10's assertion has one
#: fewer way to be wrong. DECISIONS-TW **TW6.4**.
EXECUTABLE_KINDS: Final[frozenset[str]] = frozenset({BUY_AT_OPEN, ARM_GTT, RAISE_GTT_STOP})
assert SELL_AT_OPEN not in EXECUTABLE_KINDS  # noqa: S101 - import-time contract

#: What a ``place()`` answers when the order reached the broker and when it did not.
PLACED_STATUSES: Final[frozenset[str]] = frozenset({"PLACED", "DUPLICATE"})
DRY_RUN_STATUSES: Final[frozenset[str]] = frozenset({"DRY_RUN"})

#: Kite's order statuses, as the postback and the order book report them.
ORDER_COMPLETE: Final = "COMPLETE"

#: This sleeve's own journal file, beside the weekly book's, the swing book's and VBT-1's. Four
#: books, four journals: an operator reading one should not have to filter out the other three.
TWT_JOURNAL_NAME: Final = "twt_orders_journal.jsonl"

#: A simulated GTT id, as the gateway mints it (``DRY-<client_id>``). There is nothing at the
#: exchange under it, so it is never handed to ``delete_gtt``.
SIMULATED_GTT_PREFIX: Final = "DRY-"

#: ``tw_order.state`` values that have spoken for one of ``04`` §6.3's three entries.
ENTERED_ORDER_STATES: Final[tuple[str, ...]] = ("CONFIRMED", "SENT", "PARTIAL", "FILLED")

_ZERO = Decimal(0)


# --- the gates, the band and the gateway ---------------------------------------------------


def twt_gates() -> ProductGates:
    """An order is real only when the desk is out of ``DRY_RUN`` **and** the flag is on.

    Two switches, deliberately independent — going live and turning this sleeve on are separate
    decisions, and either can be reversed without the other. Intraday and options are false
    unconditionally rather than read from config: this sleeve is CNC-only (Track C §1,
    non-negotiable 5) whatever the weekly desk is allowed to do.
    """
    return ProductGates(
        dry_run=C.DRY_RUN or not C.TWT_EXECUTION_ENABLED,
        intraday_enabled=False,
        options_enabled=False,
    )


def twt_stop_band() -> StopBand:
    """``04`` §10.7 — 0.5-30 % below the last price (DECISIONS-TW TW0.8).

    The desk's own 8-12 % is the *weekly* book's, the swing sleeve's is 0.5-10 % and VBT-1's is
    0.5-15 %. A TWT stop sits 20 % under the fill and ratchets to 20 % under the highest high
    since: a band that refused this sleeve's own stop would make non-negotiable 4 unsatisfiable.
    Read off the engine's config so the band and the arithmetic that produces the trigger cannot
    be two different numbers.
    """
    exits = DEFAULT_TWT_CONFIG.exits
    return StopBand(min_pct=float(exits.gtt_band_min_pct), max_pct=float(exits.gtt_band_max_pct))


def twt_limit_fraction() -> float:
    """``04`` §10.6 — where this sleeve's GTT rests its own LIMIT leg, as a fraction [0.97].

    A GTT fires a LIMIT order and Maulik uses market stops, so the limit sits 3 % under the
    trigger and fills on the way down the way a market stop would. The weekly book keeps the
    gateway's own 0.995 and never sees this number; the keyword is additive.
    """
    return float(DEFAULT_TWT_CONFIG.exits.gtt_limit_fraction)


def twt_journal_path() -> str:
    """``twt_orders_journal.jsonl`` beside the desk's journal — read off the shim at call time,
    so a test that isolates one isolates all four."""
    return os.path.join(os.path.dirname(_gateway_module.JOURNAL), TWT_JOURNAL_NAME)


def build_twt_gateway(kc: Any, risk: Any) -> OrderGateway:  # noqa: ANN401 - the desk's clients
    """The desk's gateway, bound to **this** sleeve's gates, band and journal.

    A separate instance from the other three books', for the reason ``build_swing_gateway``
    gives: the idempotency maps are per instance and keyed on ``client_id``, the four books mint
    ids from different plan namespaces, and a shared instance would let one book's gates callable
    be swapped for another's by whichever caller built it first.
    """
    return OrderGateway(
        kc,
        risk,
        gates=twt_gates,
        journal_path=twt_journal_path(),
        stop_band=twt_stop_band(),
    )


def is_simulated() -> bool:
    """Whether a confirm right now would be journalled ``simulated=true``."""
    return bool(twt_gates().dry_run)


# --- the store -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SleeveMoney:
    """``04`` §9 as the confirm needs it, re-read under the lock rather than taken from the page.

    ``04`` §10.5: a line's size is a preview and the confirm is the gate, so every number the
    re-size reads is read again here — never carried on the form.
    """

    equity_inr: Decimal
    cash_available_inr: Decimal
    open_exposure_inr: Decimal
    first_live_entries_left: int
    max_open_positions: int
    max_position_pct: Decimal
    stop_pct: Decimal
    trail_pct: Decimal


class TwtStore(Protocol):
    """One user's ``tw_`` rows. All prices ``Decimal``."""

    def plan(self, plan_id: str) -> dict | None:
        """keys: id, plan_id, session_date, source, built_at, expires_at, gate"""
        ...

    def line(self, line_id: int) -> dict | None:
        """keys: id, plan_pk, plan_id, kind, instrument_id, symbol, quantity, stop_price,
        value_inr, high_since, previous_trigger, note, state, client_id, order_id, position_id,
        session_date, expires_at"""
        ...

    def set_line(
        self,
        line_id: int,
        *,
        state: str,
        journal_ref: str | None = None,
        order_id: int | None = None,
        position_id: int | None = None,
        note: str | None = None,
    ) -> None: ...

    def open_position_for(self, instrument_id: int) -> dict | None: ...

    def position(self, position_id: int) -> dict | None: ...

    def open_positions(self) -> list[dict]: ...

    def create_position(self, fields: dict) -> int: ...

    def update_position(self, position_id: int, fields: dict) -> None: ...

    def add_fill(self, fields: dict) -> int: ...

    def order(self, order_id: int) -> dict | None: ...

    def order_by_broker_id(self, broker_order_id: str) -> dict | None: ...

    def create_order(self, fields: dict) -> int: ...

    def update_order(self, order_id: int, fields: dict) -> None: ...

    def entries_taken(self, day: dt.date) -> int:
        """``04`` §6.3 — this session's buy orders already CONFIRMED, SENT, PARTIAL or FILLED."""
        ...

    def bump_session(  # noqa: PLR0913 - the session row is its counters
        self,
        day: dt.date,
        *,
        mode: str,
        confirms: int = 0,
        fills: int = 0,
        ratchets: int = 0,
        exits: int = 0,
    ) -> None: ...

    def set_naked_count(self, day: dt.date, *, mode: str, naked: int) -> None: ...

    def lock_session_for_update(self, day: dt.date) -> AbstractContextManager[None]:
        """``04`` §10.5 — the day's ``tw_session`` row locked for the whole of a confirm: the
        re-read of the book, the re-size, the gateway call and the writes."""
        ...

    def sleeve_money(self, as_of: dt.date) -> SleeveMoney: ...

    def adj_factor(self, instrument_id: int, as_of: dt.date) -> Decimal: ...

    def count_first_live_entry(
        self, position_id: int, *, session_date: dt.date, now: dt.datetime, live: bool
    ) -> bool:
        """``04`` §6.4, TW5.3 — spend one of the first ten live entries, on a **fill**."""
        ...

    def expire_plans(self, now: dt.datetime) -> int: ...

    def zero_capital(self, *, now: dt.datetime, changed_by: str) -> Decimal | None: ...


# --- the outcome ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecOutcome:
    """What one confirmed line came to.

    ``SIMULATED`` — the gateway's dry-run branch ran end to end and the book was written with
    ``simulated=true``. ``SENT`` — a real order was accepted and is not yet filled. ``FILLED`` —
    the action is complete at the broker; for an ``ARM_GTT`` or a ``RAISE_GTT_STOP`` it means the
    trigger is resting. ``BLOCKED`` — refused here or by the gateway, with its words in
    ``reason``, and nothing was sent. ``REJECTED`` — the broker refused.
    """

    status: str
    reason: str
    order: dict | None
    gtt: dict | None
    order_row_id: int | None
    position_id: int | None
    simulated: bool
    #: True when the position named in ``reason`` has shares open and no resting stop. The one
    #: state the method forbids, surfaced as a field so a caller does not have to read prose.
    naked: bool = False

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _blocked(
    reason: str, *, simulated: bool, position_id: int | None = None, naked: bool = False
) -> ExecOutcome:
    return ExecOutcome(
        status="BLOCKED",
        reason=reason,
        order=None,
        gtt=None,
        order_row_id=None,
        position_id=position_id,
        simulated=simulated,
        naked=naked,
    )


def _price(value: Any) -> Decimal:  # noqa: ANN401 - a store value
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _blocked_reason(result: dict, *, what: str) -> str:
    """The gateway's own words for a refusal, in one sentence."""
    status = result.get("status", "?")
    if status == "DUPLICATE":
        ref = result.get("order_id", result.get("gtt_id"))
        return f"{what} already sent for this plan ({status}: {ref})"
    error = result.get("error") or status
    return f"{what} refused by the gateway ({status}): {error}"


def is_simulated_gtt(gtt_id: Any) -> bool:  # noqa: ANN401 - a store value
    return str(gtt_id).startswith(SIMULATED_GTT_PREFIX)


def _broker_gtt_id(gtt_id: Any) -> int | None:  # noqa: ANN401 - a store value
    """The integer trigger id the exchange knows, or ``None`` for a simulated or absent one."""
    if gtt_id is None or is_simulated_gtt(gtt_id):
        return None
    try:
        return int(gtt_id)
    except (TypeError, ValueError):
        return None


def _tenant() -> TenantIds:
    return TenantIds(user_id=int(C.SOLE_USER_ID), broker_account_id=int(C.SOLE_BROKER_ACCOUNT_ID))


def _aware(stamp: dt.datetime) -> dt.datetime:
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=IST)


def session_day(now: dt.datetime) -> dt.date:
    return _aware(now).astimezone(IST).date()


def _tick() -> Decimal:
    return Decimal(TICK_INR)


def resting_trigger(position: dict) -> Decimal:
    """The stop in force — the **greater** of ``stop_price`` and the resting ``gtt_trigger``.

    The two can differ for a session while a raise is in flight, and the safe reading of "a stop
    never falls" is the higher of them (TW4.8, restated here because this is the surface that
    refuses a fall).
    """
    stop = _price(position["stop_price"])
    trigger = position.get("gtt_trigger")
    return stop if trigger is None else max(stop, _price(trigger))


# --- the gateway calls ---------------------------------------------------------------------


async def _arm(  # noqa: PLR0913 - a stop is its instrument, its size and its two prices
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    symbol: str,
    qty: int,
    trigger: Decimal,
    last_price: Decimal,
    client_id: str,
) -> dict:
    """One GTT through the gateway. ``Decimal`` in, float across the boundary.

    ``limit_fraction`` is this sleeve's own (``04`` §10.6): the resting LIMIT sits 3 % under the
    trigger so it fills on the way down like the market stop Maulik uses. The weekly book's GTTs
    keep the gateway's default and are byte-for-byte as before.
    """
    result = await gateway.place_gtt_stop(
        symbol=symbol,
        qty=int(qty),
        trigger=float(trigger),
        last_price=float(last_price),
        exchange="NSE",
        client_id=client_id,
        tenant=_tenant(),
        plan_tenant=_tenant(),
        limit_fraction=twt_limit_fraction(),
    )
    result.setdefault("client_id", client_id)
    return result


async def _cancel(gateway: Any, *, gtt_id: Any, symbol: str, client_id: str) -> dict | None:  # noqa: ANN401
    """Pull a resting trigger. ``None`` when there was nothing resting.

    A simulated trigger is recorded as cancelled without a gateway call, because nothing exists
    at the exchange under a ``DRY-`` handle and ``delete_gtt`` would (rightly) refuse it.
    """
    if gtt_id is None:
        return None
    broker_id = _broker_gtt_id(gtt_id)
    if broker_id is None:
        return {
            "symbol": symbol,
            "gtt_id": gtt_id,
            "status": DRY_RUN_GTT_DELETE,
            "simulated": True,
        }
    return await gateway.delete_gtt(
        gtt_id=broker_id,
        symbol=symbol,
        exchange="NSE",
        client_id=client_id,
        tenant=_tenant(),
        plan_tenant=_tenant(),
    )


def _gtt_fields(gtt: dict, *, trigger: Decimal, now: dt.datetime) -> dict:
    """The position's three GTT columns from a placed (or simulated) trigger."""
    gtt_id = gtt.get("gtt_id")
    if gtt.get("status") != GTT_PLACED or gtt_id is None:
        gtt_id = f"{SIMULATED_GTT_PREFIX}{gtt.get('client_id') or gtt.get('symbol')}"
    return {
        "gtt_id": str(gtt_id),
        "gtt_trigger": trigger,
        "gtt_armed_at": _aware(now),
    }


# --- validation ----------------------------------------------------------------------------


def _validate(store: TwtStore, plan_id: str, line_id: int, confirm: str, now: dt.datetime) -> dict:
    """The refusals, in the order a caller hits them. Raises ``HTTPException``.

    Order matters: a missing ``confirm`` is answered before anything is read, so a form posted by
    accident costs no database work and no broker read.
    """
    if str(confirm).lower() != "true":
        raise HTTPException(400, "confirm=true is required — nothing fires without it")
    plan = store.plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"no plan {plan_id}")
    expires = plan["expires_at"]
    # `>=`, not `>`: **at** the instant it expires, it is expired. That is the safe direction,
    # and it is what makes `/twt/halt`'s second behaviour exact — the halt stamps every live
    # plan's `expires_at` with the moment of the halt, and a confirm in the same microsecond
    # must not slip through the gap a strict `>` would leave.
    if expires is not None and _aware(now) >= _aware(expires):
        raise HTTPException(
            410,
            f"plan {plan_id} expired at {expires.isoformat()}; rebuild it and confirm again "
            f"— a plan is good for thirty minutes and no way round that is a feature",
        )
    line = store.line(line_id)
    if line is None or str(line["plan_id"]) != str(plan_id):
        raise HTTPException(404, f"no line {line_id} on plan {plan_id}")
    if line["kind"] not in EXECUTABLE_KINDS:
        raise HTTPException(
            400,
            f"a {line['kind']} line cannot be executed: this sleeve has no end-of-day sell "
            f"rule and the GTT is the exit",
        )
    if line["state"] != "PROPOSED":
        raise HTTPException(
            409,
            f"line {line_id} is already {line['state']} — a line is confirmed once, and "
            f"client_id {line.get('client_id')} would be the gateway's second refusal",
        )
    return line


# --- BUY_AT_OPEN ---------------------------------------------------------------------------


def _resize(  # noqa: PLR0913 - the re-size is every cap `04` §6.2 applies, in order
    line: dict,
    money: SleeveMoney,
    *,
    entry_price: Decimal,
    turnover_avg_inr: Decimal | None,
    gates: ProductGates,
) -> tuple[int, Decimal, Decimal, str]:
    """``04`` §10.5 — re-derive the line's size under the lock. Returns qty, value, stop, note.

    A line that fits goes as planned. One that only the exposure ceiling refuses is **shrunk to
    the ceiling's headroom**, and never grown past what the page showed: a confirm that sent more
    than the page said would be a different order from the one a person agreed to. One the rules
    cannot line at all raises :class:`_Refused`, and the caller marks the row ``REJECTED``.
    """
    config = DEFAULT_TWT_CONFIG
    exits = dataclasses.replace(
        config.exits, stop_pct=money.stop_pct, trail_pct=money.trail_pct
    )
    sizing = dataclasses.replace(config.sizing, max_position_pct=money.max_position_pct)
    stop = initial_stop(entry_price, exits, _tick())
    if stop >= entry_price:
        raise _Refused("STOP_NOT_BELOW_ENTRY: the stop is not below the entry")
    multiplier = first_live_multiplier(
        entries_left=money.first_live_entries_left,
        execution_enabled=not gates.dry_run,
        config=sizing,
    )
    sized = size_entry(
        equity=money.equity_inr,
        cash_available=money.cash_available_inr,
        entry_price=entry_price,
        stop_price=stop,
        turnover_avg_inr=turnover_avg_inr,
        config=sizing,
        slot_multiplier=multiplier,
    )
    if not sized.placed:
        refusal = sized.refusal or SizeRefusal.BELOW_MIN_TRADE_VALUE
        raise _Refused(f"{refusal.value}: the rules cannot line this name at the confirm")
    quantity = min(int(sized.quantity), int(line["quantity"]))
    note = "" if quantity == int(line["quantity"]) else "re-sized down at the confirm"
    headroom = money.equity_inr - money.open_exposure_inr
    if entry_price * quantity > headroom:
        quantity = int(headroom // entry_price) if entry_price > _ZERO else 0
        note = "shrunk to the exposure ceiling's headroom"
    if quantity <= 0:
        raise _Refused("EXPOSURE_FULL: the sleeve's own cash is fully committed")
    value = (entry_price * quantity).quantize(Decimal("0.01"))
    if value < sizing.min_trade_value_inr:
        raise _Refused("BELOW_MIN_TRADE_VALUE: what is left is below the minimum trade")
    return quantity, value, stop, note


class _Refused(Exception):
    """A rule refused the line at the confirm. Carries the skip code that leads the reason."""


async def _buy_at_open(  # noqa: PLR0913 - a confirm is its line, its money and its gateway
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
    last_price: Decimal | None,
) -> ExecOutcome:
    """``04`` §5.1 — the next session's **open, at market**, and a ``tw_order`` that records it.

    Three refusals before anything is read from the broker, then the re-size of ``04`` §10.5,
    then one ``place`` through the gateway. **The GTT is armed in the same request as the fill**,
    which is non-negotiable 4 and, on this sleeve, the fill-day rule.
    """
    day = plan["session_date"]
    cap = DEFAULT_TWT_CONFIG.sizing.max_new_entries_per_session
    taken = store.entries_taken(day)
    if taken >= cap:
        reason = f"SESSION_CAP: {taken} entries already taken this session; {cap} is the cap"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    if store.open_position_for(int(line["instrument_id"])) is not None:
        reason = "ALREADY_HELD: the sleeve holds this name; the book never averages down"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    money = store.sleeve_money(day)
    if money.equity_inr <= _ZERO:
        reason = "NO_SLEEVE_CAPITAL: the sleeve has no capital set"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    # A MARKET order has no price of its own. The live quote is the honest reference and the
    # desk supplies it per confirm; the plan's own preview (the signal session's close, which
    # is what the page showed) is the fallback, named in the note so a fill against a stale
    # reference is legible rather than invisible.
    preview = _price(line["value_inr"]) / int(line["quantity"]) if line["quantity"] else _ZERO
    reference = last_price if last_price is not None else preview
    if reference <= _ZERO:
        reason = "no price to size or value this market buy against"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)

    try:
        quantity, value, stop, note = _resize(
            line,
            money,
            entry_price=reference,
            turnover_avg_inr=line.get("turnover_avg_inr"),
            gates=gates,
        )
    except _Refused as refused:
        store.set_line(line["id"], state="REJECTED", note=str(refused))
        return _blocked(str(refused), simulated=gates.dry_run)

    result = await gateway.place(
        symbol=line["symbol"],
        qty=quantity,
        side="BUY",
        product="CNC",
        order_type="MARKET",
        client_id=line["client_id"],
        reference_price=float(reference),
        gross_exposure=float(value),
        tenant=_tenant(),
        plan_tenant=_tenant(),
    )
    status = str(result.get("status") or "")
    if status not in PLACED_STATUSES | DRY_RUN_STATUSES:
        reason = str(result.get("error") or f"the gateway answered {status}")
        store.set_line(line["id"], state="REJECTED", note=reason)
        return ExecOutcome(
            "BLOCKED" if status.endswith("BLOCKED") or status == "DUPLICATE" else "REJECTED",
            reason,
            result,
            None,
            None,
            None,
            gates.dry_run,
        )

    order_row = store.create_order(
        {
            "instrument_id": int(line["instrument_id"]),
            "signal_date": day,
            "side": "BUY",
            "quantity": quantity,
            "stop_price": stop,
            "state": "SENT",
            "broker_order_id": result.get("order_id"),
            "client_id": line["client_id"],
            "simulated": gates.dry_run,
        }
    )
    store.set_line(
        line["id"],
        state="SENT",
        journal_ref=str(result.get("order_id") or ""),
        order_id=order_row,
        note=note or None,
    )
    store.bump_session(day, mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1)
    if status in DRY_RUN_STATUSES:
        # Track B: the dry-run branch is a complete fill at the reference, so the rehearsal
        # exercises the position, the fill row **and the stop** rather than stopping at the
        # order. A rehearsal that stopped before the GTT would rehearse the wrong thing.
        return await _apply_fill(
            store,
            gateway,
            line=line,
            plan=plan,
            order_row=order_row,
            quantity=quantity,
            fill_price=reference,
            gates=gates,
            now=now,
        )
    return ExecOutcome("SENT", "", result, None, order_row, None, gates.dry_run)


async def _apply_fill(  # noqa: PLR0913 - a fill is its order, its price and its size
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    line: dict | None,
    plan: dict | None,
    order_row: int,
    quantity: int,
    fill_price: Decimal,
    gates: ProductGates,
    now: dt.datetime,
) -> ExecOutcome:
    """A filled entry becomes a ``tw_position``, a ``tw_fill`` **and a resting GTT**.

    The three writes and the arm are one request, deliberately. ``04`` §7.4: a fill without a
    same-session GTT has no fill-day protection at all, and the backtest's ``STOP_DAY0`` — a
    name whose entry session's own low is 20 % under its open — is modelled in the live book by
    this arm and by nothing else.

    ``04`` §7.1's stop is computed from the **fill**, not from the plan's preview and not from
    the cost-inclusive book entry (``04`` §5.3).
    """
    order = store.order(order_row)
    assert order is not None
    day = plan["session_date"] if plan is not None else session_day(now)
    symbol = str(line["symbol"]) if line is not None else str(order["symbol"])
    instrument_id = int(order["instrument_id"])
    money = store.sleeve_money(day)
    exits = dataclasses.replace(
        DEFAULT_TWT_CONFIG.exits, stop_pct=money.stop_pct, trail_pct=money.trail_pct
    )
    stop = initial_stop(fill_price, exits, _tick())
    position_id = store.create_position(
        {
            "instrument_id": instrument_id,
            "order_id": order_row,
            "signal_date": order["signal_date"],
            "entry_date": day,
            "entry_avg": fill_price,
            "entry_adj_factor": store.adj_factor(instrument_id, day),
            "quantity_entered": quantity,
            "quantity_open": quantity,
            "initial_stop": stop,
            "stop_price": stop,
            "high_since": fill_price,
            "high_since_date": day,
            "state": "OPEN",
            "simulated": gates.dry_run,
        }
    )
    store.add_fill(
        {
            "position_id": position_id,
            "order_id": order_row,
            "side": "BUY",
            "quantity": quantity,
            "price": fill_price,
            "filled_at": _aware(now),
            "simulated": gates.dry_run,
        }
    )
    store.update_order(
        order_row,
        {
            "state": "FILLED",
            "filled_quantity": quantity,
            "avg_fill_price": fill_price,
            "position_id": position_id,
        },
    )
    if line is not None:
        store.set_line(line["id"], state="FILLED", position_id=position_id)
    store.bump_session(day, mode="DRY_RUN" if gates.dry_run else "LIVE", fills=1)
    # `04` §6.4, TW5.3: the countdown moves once per FILLED entry, by the session that filled
    # it — never on a proposed line, never on a simulated fill, never twice for the same one.
    store.count_first_live_entry(
        position_id, session_date=day, now=_aware(now), live=not gates.dry_run
    )
    gtt = await _arm(
        gateway,
        symbol=symbol,
        qty=quantity,
        trigger=stop,
        last_price=fill_price,
        client_id=f"{line['client_id']}:{ARM_GTT}"
        if line is not None
        else f"fill:{order_row}:{symbol}:{ARM_GTT}",
    )
    if str(gtt.get("status")) in GTT_PLACED_STATUSES:
        store.update_position(position_id, _gtt_fields(gtt, trigger=stop, now=now))
        return ExecOutcome(
            "SIMULATED" if gates.dry_run else "FILLED",
            "",
            None,
            gtt,
            order_row,
            position_id,
            gates.dry_run,
        )
    # The shares are held and the stop is not resting. The honest record is a NAKED position,
    # and it is said out loud rather than logged: the sweep will find it, and so should a person.
    log.error(
        "%s: filled and the GTT was not armed — position %s is NAKED (TWT_POSITION_NAKED)",
        symbol,
        position_id,
    )
    return ExecOutcome(
        "BLOCKED",
        f"{symbol}: the buy filled and the stop was not armed — "
        f"{_blocked_reason(gtt, what='the GTT')}; position {position_id} is NAKED, re-arm it",
        None,
        gtt,
        order_row,
        position_id,
        gates.dry_run,
        naked=True,
    )


# --- ARM_GTT -------------------------------------------------------------------------------


async def _arm_gtt_line(  # noqa: PLR0913 - a re-arm is its line, its book and its gateway
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
    last_price: Decimal | None,
) -> ExecOutcome:
    """``04`` §10.2's ``ARM_GTT``: a filled position that somehow has no resting stop."""
    position = store.open_position_for(int(line["instrument_id"]))
    if position is None:
        reason = f"{line['symbol']} is not in this sleeve's book"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    if position.get("gtt_id") is not None:
        reason = (
            f"{line['symbol']}: a GTT is already resting ({position['gtt_id']}); arming a "
            f"second one would sell twice what is held when they fire"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=int(position["id"]))
    stop = _price(line["stop_price"]) if line.get("stop_price") is not None else _price(
        position["stop_price"]
    )
    reference = last_price if last_price is not None else _price(position["entry_avg"])
    if stop >= reference:
        reason = (
            f"{line['symbol']}: the stop {stop} is at or above the "
            f"{'last price' if last_price is not None else 'entry'} {reference} — it would "
            f"fire the moment it is armed"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=int(position["id"]))
    gtt = await _arm(
        gateway,
        symbol=line["symbol"],
        qty=int(position["quantity_open"]),
        trigger=stop,
        last_price=reference,
        client_id=line["client_id"],
    )
    if str(gtt.get("status")) not in GTT_PLACED_STATUSES:
        reason = _blocked_reason(gtt, what="the GTT")
        store.set_line(line["id"], state="REJECTED", note=reason)
        return ExecOutcome(
            "BLOCKED",
            f"{line['symbol']}: {reason}; position {position['id']} is NAKED, re-arm it",
            None,
            gtt,
            None,
            int(position["id"]),
            gates.dry_run,
            naked=True,
        )
    store.update_position(int(position["id"]), _gtt_fields(gtt, trigger=stop, now=now))
    store.set_line(line["id"], state="FILLED", position_id=int(position["id"]))
    store.bump_session(
        plan["session_date"], mode="DRY_RUN" if gates.dry_run else "LIVE", confirms=1
    )
    return ExecOutcome(
        "SIMULATED" if gates.dry_run else "FILLED",
        "",
        None,
        gtt,
        None,
        int(position["id"]),
        gates.dry_run,
    )


# --- RAISE_GTT_STOP: the ratchet -----------------------------------------------------------


async def _raise_gtt_stop(  # noqa: PLR0913, C901 - the ratchet is its guards
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    line: dict,
    plan: dict,
    gates: ProductGates,
    now: dt.datetime,
    last_price: Decimal | None,
) -> ExecOutcome:
    """**The ratchet** (``04`` §7.2): cancel the resting trigger, arm the new one.

    Five refusals before anything is cancelled, and each of them leaves the resting stop exactly
    where it is — which is the safe direction every time:

    1. not a position this sleeve holds;
    2. no new trigger on the line;
    3. **the new trigger is at or below the resting one — a stop never falls.** Equal is refused
       too: cancelling and re-arming the same level is a moment of nakedness for nothing;
    4. nothing open to protect;
    5. no last price, or a trigger at or above it — which would fire the moment it is armed.

    Then the two halves, and the second is the one that can cost money. See the module docstring.
    """
    symbol = str(line["symbol"])
    position = store.open_position_for(int(line["instrument_id"]))
    if position is None:
        reason = f"{symbol} is not in this sleeve's book"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run)
    position_id = int(position["id"])
    if line.get("stop_price") is None:
        reason = f"{symbol}: a RAISE line needs a new trigger"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=position_id)
    new_trigger = _price(line["stop_price"])
    resting = resting_trigger(position)
    if new_trigger <= resting:
        reason = (
            f"{symbol}: the new trigger {new_trigger} is not above the resting stop {resting} "
            f"— a stop never falls"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=position_id)
    open_qty = int(position.get("quantity_open") or 0)
    if open_qty <= 0:
        reason = f"{symbol}: nothing is open to protect"
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=position_id)
    if last_price is None:
        reason = (
            f"{symbol}: no last price to check the new trigger against — a stop armed above "
            f"the market fires at the next tick"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=position_id)
    if new_trigger >= _price(last_price):
        reason = (
            f"{symbol}: the new trigger {new_trigger} is at or above the last price "
            f"{last_price} — that would fire at once"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return _blocked(reason, simulated=gates.dry_run, position_id=position_id)

    client_id = str(line["client_id"])
    cancel = await _cancel(
        gateway, gtt_id=position.get("gtt_id"), symbol=symbol, client_id=f"{client_id}:cancel"
    )
    if cancel is not None and str(cancel.get("status")) not in GTT_DELETED_STATUSES:
        # THE OLD TRIGGER IS STILL RESTING, so the new one is NOT placed: two triggers sell the
        # position twice. The stop stays where it was — one session behind, and protected.
        reason = (
            f"{symbol}: {_blocked_reason(cancel, what='cancelling the old GTT')}; the old stop "
            f"at {resting} is still resting and nothing was placed"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        return ExecOutcome(
            "BLOCKED", reason, None, cancel, None, position_id, gates.dry_run, naked=False
        )

    gtt = await _arm(
        gateway,
        symbol=symbol,
        qty=open_qty,
        trigger=new_trigger,
        last_price=_price(last_price),
        client_id=client_id,
    )
    if str(gtt.get("status")) not in GTT_PLACED_STATUSES:
        # CANCELLED AND NOT RE-ARMED: the line is NAKED. The intent — the raised stop — is
        # recorded so the next re-arm rests it at the right level, and `gtt_id = None` is what
        # makes the page, the sweep and `TWT_POSITION_NAKED` all able to see it.
        store.update_position(
            position_id,
            {
                "stop_price": new_trigger,
                "gtt_id": None,
                "gtt_trigger": None,
                "gtt_armed_at": None,
            },
        )
        reason = (
            f"{symbol}: the old stop was cancelled and the new one was not armed — "
            f"{_blocked_reason(gtt, what='the GTT')}; position {position_id} is NAKED, re-arm it"
        )
        store.set_line(line["id"], state="REJECTED", note=reason)
        log.error(
            "%s: cancelled and not re-armed — position %s is NAKED (TWT_POSITION_NAKED)",
            symbol,
            position_id,
        )
        return ExecOutcome(
            "BLOCKED", reason, None, gtt, None, position_id, gates.dry_run, naked=True
        )

    store.update_position(
        position_id,
        {
            **_gtt_fields(gtt, trigger=new_trigger, now=now),
            "stop_price": new_trigger,
            # The trigger has been spent. Leaving it would offer the same raise again tomorrow
            # against a stop that has already moved, which `exit_lines` would then refuse — a
            # line that cannot be confirmed is a line nobody should be shown.
            "next_trigger": None,
            "next_trigger_for": None,
        },
    )
    store.set_line(line["id"], state="FILLED", position_id=position_id)
    store.bump_session(
        plan["session_date"],
        mode="DRY_RUN" if gates.dry_run else "LIVE",
        confirms=1,
        ratchets=1,
    )
    return ExecOutcome(
        "SIMULATED" if gates.dry_run else "FILLED",
        "",
        None,
        gtt,
        None,
        position_id,
        gates.dry_run,
    )


# --- the entry point -------------------------------------------------------------------------


async def execute_line(  # noqa: PLR0913 - a confirm is its store, its gateway and its request
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    plan_id: str,
    line_id: int,
    confirm: str,
    now: dt.datetime | None = None,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """One confirmed line becomes an order, a stop, a raised stop, or a refusal with its reason.

    The whole of it runs under a row lock on the day's ``tw_session`` (``04`` §10.5): the re-read
    of the book, the re-size, the gateway call and the writes. Two browser tabs confirming a
    fourth entry between them is exactly what the lock exists to stop.
    """
    stamp = now or dt.datetime.now(tz=IST)
    gates = twt_gates()
    line = _validate(store, plan_id, line_id, confirm, stamp)
    plan = store.plan(plan_id)
    assert plan is not None  # `_validate` has already refused a missing one
    with store.lock_session_for_update(plan["session_date"]):
        if line["kind"] == BUY_AT_OPEN:
            return await _buy_at_open(store, gateway, line, plan, gates, stamp, last_price)
        if line["kind"] == ARM_GTT:
            return await _arm_gtt_line(store, gateway, line, plan, gates, stamp, last_price)
        return await _raise_gtt_stop(store, gateway, line, plan, gates, stamp, last_price)


# --- the four chores the runbook needs -------------------------------------------------------


async def rearm_gtt(
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    position_id: int,
    confirm: str,
    now: dt.datetime,
    last_price: Decimal | None = None,
) -> ExecOutcome:
    """Arm a stop for a **naked** position — ``gtt_id`` null with shares open. Nothing else.

    ``05`` §2's per-line Re-arm button, and the sweep's own worker. A position that already
    carries a trigger is ``BLOCKED``: two triggers on one position sell twice what is held when
    they fire. Re-sizing a resting stop is ``RAISE_GTT_STOP``'s job, which cancels first.

    **This is the callable TW7's sweep injects.** It takes a store, a gateway and one position
    id, it places no buy and it can only ever add protection.
    """
    if str(confirm).lower() != "true":
        raise HTTPException(400, "Arming a stop requires explicit confirmation.")
    position = store.position(position_id)
    if position is None:
        raise HTTPException(404, "Unknown position_id.")
    simulated = twt_gates().dry_run
    symbol = str(position["symbol"])
    if position.get("gtt_id") is not None:
        return _blocked(
            f"{symbol}: a GTT is already resting ({position['gtt_id']}); re-arming would "
            f"leave two",
            simulated=simulated,
            position_id=position_id,
        )
    open_qty = int(position.get("quantity_open") or 0)
    if open_qty <= 0 or position.get("state") == "CLOSED":
        return _blocked(
            f"{symbol}: nothing is open to protect", simulated=simulated, position_id=position_id
        )
    trigger = resting_trigger(position)
    reference = last_price if last_price is not None else _price(position["entry_avg"])
    if trigger >= reference:
        return _blocked(
            f"{symbol}: the stop {trigger} is at or above the "
            f"{'last price' if last_price is not None else 'entry'} {reference} — a GTT that "
            f"would fire on the next tick is not protection; decide whether to sell, by hand",
            simulated=simulated,
            position_id=position_id,
        )
    gtt = await _arm(
        gateway,
        symbol=symbol,
        qty=open_qty,
        trigger=trigger,
        last_price=reference,
        client_id=f"rearm:{position_id}:{symbol}:{ARM_GTT}:{session_day(now).isoformat()}",
    )
    if str(gtt.get("status")) not in GTT_PLACED_STATUSES:
        return ExecOutcome(
            "BLOCKED",
            f"{symbol}: {_blocked_reason(gtt, what='the GTT')}; position {position_id} is "
            f"still NAKED",
            None,
            gtt,
            None,
            position_id,
            simulated,
            naked=True,
        )
    store.update_position(position_id, _gtt_fields(gtt, trigger=trigger, now=now))
    return ExecOutcome(
        "SIMULATED" if simulated else "FILLED", "", None, gtt, None, position_id, simulated
    )


def rearm_callable(
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    now: dt.datetime,
    price_for: Callable[[str], Decimal | None] | None = None,
) -> Callable[[int], Awaitable[dict[str, Any]]]:
    """**The seam TW7's sweep injects.** One position id in, one plain dict out.

    ``tools/twt/sweep.py`` declares ``Rearm = Callable[[PositionId], Awaitable[RearmOutcome]]``
    and ships ``unavailable_rearm`` — which refuses loudly — until this exists. It lives in the
    data-plant tree and cannot import this module, so what this returns is a **dict**, not a
    dataclass: ``{"position_id", "armed", "gtt_id", "reason"}``, exactly ``RearmOutcome``'s
    fields, so the caller builds one with ``RearmOutcome(**answer)`` and neither tree has to
    import the other's types.

    ``armed`` is the only field the sweep branches on, and it is true **only** when a trigger is
    actually resting (or was faithfully simulated). A re-arm that could not arm returns
    ``armed=False`` with the gateway's own words, because "the trigger is at or above the last
    price" and "an untouchable instrument" want completely different hands
    (FIRST-LIVE-MORNING §9.2 step 2).

    It places no buy and cancels nothing. The worst it can do is add protection.
    """

    async def rearm(position_id: int) -> dict[str, Any]:
        position = store.position(int(position_id))
        price = None
        if position is not None and price_for is not None:
            price = price_for(str(position["symbol"]))
        outcome = await rearm_gtt(
            store,
            gateway,
            position_id=int(position_id),
            confirm="true",
            now=now,
            last_price=price,
        )
        after = store.position(int(position_id))
        return {
            "position_id": int(position_id),
            "armed": outcome.status in {"FILLED", "SIMULATED"},
            "gtt_id": None if after is None else (
                None if after.get("gtt_id") is None else str(after["gtt_id"])
            ),
            "reason": outcome.reason,
        }

    return rearm


def naked_positions(store: TwtStore) -> list[dict]:
    """Every ``OPEN`` position with shares and no resting ``gtt_id`` (``04`` §7.4, TW7)."""
    return [
        row
        for row in store.open_positions()
        if row.get("gtt_id") is None and int(row.get("quantity_open") or 0) > 0
    ]


@dataclass(frozen=True)
class SweepReport:
    """What the 15:15 chore found and what it could do about it."""

    naked_before: int
    rearmed: int
    naked: int
    outcomes: tuple[ExecOutcome, ...]


async def sweep_naked(
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    *,
    now: dt.datetime,
    prices: dict[str, Decimal] | None = None,
) -> SweepReport:
    """The 15:15 sweep: re-arm everything naked, and say what is still naked afterwards.

    **Idempotent and keyed on the day** (TW7): a second run finds nothing to do and writes the
    same count. It places no buy, cancels nothing and can only add protection — which is why it
    is safe to run by hand at any hour, and why the halt does not disable it.

    What is left over is ``TWT_GTT_MISSING_AT_1515``. This process cannot reach the data plant's
    alert sinks (a different venv), so the name is logged at ``error`` — the swing book's own
    arrangement — and ``tools/twt/sweep.py`` (TW7) raises the real alert from the tree that can.
    """
    day = session_day(now)
    before = naked_positions(store)
    outcomes: list[ExecOutcome] = []
    for position in before:
        price = (prices or {}).get(str(position["symbol"]))
        outcomes.append(
            await rearm_gtt(
                store,
                gateway,
                position_id=int(position["id"]),
                confirm="true",
                now=now,
                last_price=price,
            )
        )
    still = naked_positions(store)
    store.set_naked_count(
        day, mode="DRY_RUN" if twt_gates().dry_run else "LIVE", naked=len(still)
    )
    if still:
        log.error(
            "TWT 15:15 sweep: %d position(s) still without a GTT — TWT_GTT_MISSING_AT_1515: %s",
            len(still),
            ", ".join(str(row["symbol"]) for row in still),
        )
    return SweepReport(
        naked_before=len(before),
        rearmed=len(before) - len(still),
        naked=len(still),
        outcomes=tuple(outcomes),
    )


class GttSource(Protocol):
    """The broker's resting triggers, as a read. Never a write."""

    def list_gtts(self) -> list[dict]: ...


@dataclass(frozen=True)
class ReconcileReport:
    """What ``/twt/reconcile`` attached, and what it could not.

    ``unmatched`` counts resting triggers **in names this sleeve holds** that landed on no
    position — not every trigger at the broker. The account also carries the weekly book's, the
    swing book's and VBT-1's stops, and counting those would make a healthy reconcile look like a
    discrepancy every single time. A non-zero number here is the case worth a second look: a
    trigger in a TWT name that this sleeve cannot account for, which usually means two stops on
    one line.
    """

    attached: int
    unmatched: int
    positions: tuple[int, ...]


def reconcile_gtts(
    store: TwtStore, source: GttSource | None, *, now: dt.datetime
) -> ReconcileReport:
    """Attach a **hand-armed** GTT to the position it protects (FIRST-LIVE-MORNING §9.2 step 3).

    Without this, a line Maulik protects by hand in the Kite app reads naked forever and the
    15:15 sweep keeps shouting about a line that is in fact covered — and worse, the sweep would
    try to arm a *second* trigger on it.

    It is a **read of the broker and a write of our own book**: it places nothing, cancels
    nothing and moves no stop. A trigger it cannot match to a naked position is left alone and
    counted, because the safe direction for a discrepancy is always *more* protection.
    """
    if source is None:
        return ReconcileReport(0, 0, ())
    try:
        resting = source.list_gtts()
    except Exception as exc:  # the reconcile is a convenience; a broker that will not answer
        log.warning("no GTT list from the broker: %s", exc)  # is not a reason to 500
        return ReconcileReport(0, 0, ())
    by_symbol: dict[str, dict] = {}
    for trigger in resting:
        symbol = str(trigger.get("symbol") or "")
        if symbol and trigger.get("status") in (None, "active", "ACTIVE"):
            by_symbol.setdefault(symbol, trigger)
    attached: list[int] = []
    for position in naked_positions(store):
        found = by_symbol.pop(str(position["symbol"]), None)
        if found is None or found.get("gtt_id") is None:
            continue
        level = found.get("trigger")
        store.update_position(
            int(position["id"]),
            {
                "gtt_id": str(found["gtt_id"]),
                "gtt_trigger": _price(level) if level is not None else resting_trigger(position),
                "gtt_armed_at": _aware(now),
            },
        )
        attached.append(int(position["id"]))
    ours = {str(row["symbol"]) for row in store.open_positions()}
    unmatched = sum(1 for symbol in by_symbol if symbol in ours)
    return ReconcileReport(len(attached), unmatched, tuple(attached))


@dataclass(frozen=True)
class HaltReport:
    """What the stop-the-sleeve command did, so the reply itself is the record."""

    halted: bool
    sleeve_capital_inr_before: Decimal | None
    plans_expired: int

    def line(self) -> str:
        """One line, because it is read on a phone."""
        before = "unset" if self.sleeve_capital_inr_before is None else (
            f"₹{self.sleeve_capital_inr_before}"
        )
        return (
            f"TWT halted: sleeve capital {before} -> ₹0 (audited), {self.plans_expired} live "
            f"plan(s) expired; every resting GTT is untouched and the ratchet still works."
        )


HALT_CHANGED_BY: Final = "twt-halt"


def halt_sleeve(store: TwtStore, *, confirm: str, now: dt.datetime) -> HaltReport:
    """**The stop-the-sleeve command** (FIRST-LIVE-MORNING §7). Four behaviours, and one absence.

    1. ``tw_config.sleeve_capital_inr`` goes to **0**, audited into ``tw_config_audit`` with the
       previous value — so it can be restored by reading the audit row rather than by
       remembering. At ₹0 every signal is skipped ``NO_SLEEVE_CAPITAL`` and every confirm of a
       ``BUY_AT_OPEN`` is ``BLOCKED``, because the confirm re-sizes through the same rules the
       plan did (``04`` §9.3, §10.5).
    2. Every unexpired ``tw_plan`` is expired, so no ``plan_id`` in anybody's browser can still
       be confirmed.
    3. **It never touches protection.** No GTT is cancelled, no stop is moved, no position is
       sold, and ``ARM_GTT``, ``RAISE_GTT_STOP``, ``/twt/rearm`` and ``/twt/sweep`` all keep
       working. *A halted sleeve is a sleeve that cannot buy*; everything it already holds keeps
       its stop and keeps ratcheting. **A halt that removed stops would be the most dangerous
       button in the product**, which is why there is not one line in this function that could.
    4. It answers in one line, with the previous capital, so the reply is the record.

    It touches no other sleeve: the weekly book, the swing book and VBT-1 are not mentioned here
    and cannot be reached from here.
    """
    if str(confirm).lower() != "true":
        raise HTTPException(400, "Halting the sleeve requires explicit confirmation.")
    before = store.zero_capital(now=_aware(now), changed_by=HALT_CHANGED_BY)
    expired = store.expire_plans(_aware(now))
    report = HaltReport(halted=True, sleeve_capital_inr_before=before, plans_expired=expired)
    log.error("%s", report.line())
    return report


# --- fills that arrive later ------------------------------------------------------------------


async def on_order_update(
    store: TwtStore,
    gateway: Any,  # noqa: ANN401 - an OrderGateway
    payload: dict,
    *,
    now: dt.datetime,
) -> ExecOutcome | None:
    """A fill reported by the broker — the postback, or a poll of the order book.

    ``None`` when the update is about an order this sleeve does not own, or one that has not
    completed. A completed buy becomes the position, the fill row **and the GTT**, through
    exactly the same :func:`_apply_fill` the dry-run branch uses: the rehearsal and the real
    thing are one code path, which is the only way a rehearsal proves anything.
    """
    broker_id = str(payload.get("order_id") or "")
    if not broker_id:
        return None
    order = store.order_by_broker_id(broker_id)
    if order is None or order.get("position_id") is not None:
        return None
    if str(payload.get("status") or "").upper() != ORDER_COMPLETE:
        return None
    filled = int(payload.get("filled_quantity") or 0)
    price = payload.get("average_price")
    if filled <= 0 or price is None:
        return None
    return await _apply_fill(
        store,
        gateway,
        line=None,
        plan=None,
        order_row=int(order["id"]),
        quantity=filled,
        fill_price=_price(price),
        gates=twt_gates(),
        now=now,
    )


# --- the money the confirm re-reads -----------------------------------------------------------


def sleeve_money_from_rows(
    config: dict, positions: list[dict], marks: dict[int, Decimal], realised: Decimal
) -> SleeveMoney:
    """``04`` §9.1-9.2 over rows the store has already read. Pure, so it is testable alone.

    The arithmetic is :func:`baskfy_core.twt.sleeve.sleeve_value` — imported, not restated, so
    the desk and the evening job cannot each derive a slightly different equity.
    """
    valued = [
        OpenPositionValue(
            instrument_id=int(row["instrument_id"]),
            quantity_open=int(row["quantity_open"]),
            entry_avg=_price(row["entry_avg"]),
            mark=marks.get(int(row["instrument_id"]), _price(row["entry_avg"])),
            mark_source=(
                MarkSource.SESSION_CLOSE
                if int(row["instrument_id"]) in marks
                else MarkSource.ENTRY
            ),
        )
        for row in positions
    ]
    value = sleeve_value(
        capital_inr=_price(config.get("sleeve_capital_inr") or 0),
        realised_inr=realised,
        open_positions=valued,
    ).quantize()
    return SleeveMoney(
        equity_inr=value.equity_inr,
        cash_available_inr=value.cash_available_inr,
        open_exposure_inr=value.open_exposure_inr,
        first_live_entries_left=int(config.get("first_live_entries_left") or 0),
        max_open_positions=int(config.get("max_open_positions") or 10),
        max_position_pct=_price(config.get("max_position_pct") or "12.50"),
        stop_pct=_price(config.get("stop_pct") or "20.00"),
        trail_pct=_price(config.get("trail_pct") or "20.00"),
    )

