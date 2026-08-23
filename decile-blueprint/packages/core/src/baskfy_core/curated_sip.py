"""SIP reminder calendar math — docs/smallcase/04 §9, SC7 / leaf 1.6.1.

REMINDER mode only. AUTO must never appear on a write path (desk non-negotiable #1;
Track-C / D3 dark). No OrderGateway, no clock, no I/O — caller supplies ``as_of`` and the
trading-day set.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping, Sequence, Set
from decimal import Decimal
from typing import Final, Literal, TypedDict

__all__ = [
    "FORBIDDEN_WRITE_MODES",
    "SIP_DUE",
    "SIP_MODE_REMINDER",
    "SIP_STATUS_ACTIVE",
    "SIP_STATUS_PAUSED",
    "SipDuePayload",
    "SipDuePendingAction",
    "advance_next_fire_date",
    "assert_reminder_mode",
    "evaluate_plan_for_fire",
    "fire_idempotency_key",
    "next_fire_date",
    "next_trading_on_or_after",
    "pause_sip",
    "raise_sip_due",
    "resume_sip",
    "should_fire",
]

SIP_MODE_REMINDER: Final = "REMINDER"
SIP_STATUS_ACTIVE: Final = "ACTIVE"
SIP_STATUS_PAUSED: Final = "PAUSED"
SIP_DUE: Final = "SIP_DUE"

#: Modes that must never be accepted on a create/update/write path (SC7 AC).
FORBIDDEN_WRITE_MODES: Final[frozenset[str]] = frozenset({"AUTO"})

#: day_of_month is capped at 28 so February never needs end-of-month inventiveness.
_DAY_OF_MONTH_MIN: Final = 1
_DAY_OF_MONTH_MAX: Final = 28

#: How far ``next_trading_on_or_after`` may scan when the caller omitted far-future dates.
_SNAP_HORIZON_DAYS: Final = 60


class SipDuePayload(TypedDict):
    plan_id: int
    investment_id: int
    amount: str
    fire_date: str
    fire_key: str
    mode: Literal["REMINDER"]


class SipDuePendingAction(TypedDict):
    """Pure pending-action shape for ``cb_pending_action`` (type=SIP_DUE)."""

    type: Literal["SIP_DUE"]
    user_id: int
    payload: SipDuePayload


def assert_reminder_mode(mode: str) -> None:
    """Refuse any mode other than REMINDER on the SIP write path.

    ``AUTO`` is listed in ``FORBIDDEN_WRITE_MODES`` so a future enum expansion cannot
    silently land on create/update without failing this guard.
    """
    if mode in FORBIDDEN_WRITE_MODES:
        raise ValueError(
            f"SIP write path refuses mode {mode!r}; AUTO stays dark until D3 "
            "(desk non-negotiable #1)"
        )
    if mode != SIP_MODE_REMINDER:
        raise ValueError(f"SIP write path accepts {SIP_MODE_REMINDER!r} only; got {mode!r}")


def _validate_day_of_month(day_of_month: int) -> None:
    if not (_DAY_OF_MONTH_MIN <= day_of_month <= _DAY_OF_MONTH_MAX):
        raise ValueError(
            f"day_of_month must be in {_DAY_OF_MONTH_MIN}..{_DAY_OF_MONTH_MAX}, got {day_of_month}"
        )


def _candidate(year: int, month: int, day_of_month: int) -> dt.date:
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day_of_month, last))


def _add_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:  # noqa: PLR2004 - December rolls into the next year
        return year + 1, 1
    return year, month + 1


def next_trading_on_or_after(
    day: dt.date,
    trading_dates: Set[dt.date],
) -> dt.date:
    """Roll *day* forward to the first date present in *trading_dates*."""
    if not trading_dates:
        raise ValueError("trading_dates cannot be empty")
    cursor = day
    for _ in range(_SNAP_HORIZON_DAYS):
        if cursor in trading_dates:
            return cursor
        cursor += dt.timedelta(days=1)
    raise ValueError(
        f"no trading day on or after {day.isoformat()} within {_SNAP_HORIZON_DAYS} days"
    )


def next_fire_date(
    *,
    day_of_month: int,
    as_of: dt.date,
    trading_dates: Set[dt.date],
    after: dt.date | None = None,
) -> dt.date:
    """Next SIP fire date: nominal ``day_of_month``, snapped to a trading day.

    Holiday / weekend aware — the caller passes the trading-day set (house rule 1: no
    calendar I/O in core). When the nominal date is closed, the fire rolls *forward* to
    the next open session.

    *as_of* is the evaluation date (usually "today" from the Beat job). The returned date
    is always ``>= as_of``. Pass *after* to require a fire strictly after that date
    (used when advancing past a just-fired month).
    """
    _validate_day_of_month(day_of_month)
    if not trading_dates:
        raise ValueError("trading_dates cannot be empty")

    floor = as_of if after is None else max(as_of, after + dt.timedelta(days=1))
    year, month = floor.year, floor.month
    # Bound the search: at most 14 months ahead covers pause/resume edge cases.
    for _ in range(14):
        candidate = _candidate(year, month, day_of_month)
        snapped = next_trading_on_or_after(candidate, trading_dates)
        if snapped >= floor:
            return snapped
        year, month = _add_month(year, month)
    raise ValueError(
        f"could not resolve next_fire_date for day_of_month={day_of_month} "
        f"as_of={as_of.isoformat()}"
    )


def advance_next_fire_date(
    *,
    day_of_month: int,
    fired_on: dt.date,
    trading_dates: Set[dt.date],
) -> dt.date:
    """After a successful fire, schedule the next month's reminder."""
    return next_fire_date(
        day_of_month=day_of_month,
        as_of=fired_on,
        trading_dates=trading_dates,
        after=fired_on,
    )


def fire_idempotency_key(plan_id: int, year: int, month: int) -> str:
    """Idempotent fire key per (plan, calendar year-month) — SC7 AC.

    Re-running Beat in the same month must not raise a second SIP_DUE for the same plan.
    """
    if plan_id < 1:
        raise ValueError(f"plan_id must be positive, got {plan_id}")
    if not (1 <= month <= 12):  # noqa: PLR2004 - calendar months
        raise ValueError(f"month must be 1..12, got {month}")
    return f"{plan_id}:{year:04d}-{month:02d}"


def pause_sip(status: str) -> Literal["PAUSED"]:
    """ACTIVE → PAUSED. Idempotent if already paused."""
    if status == SIP_STATUS_PAUSED:
        return SIP_STATUS_PAUSED
    if status != SIP_STATUS_ACTIVE:
        raise ValueError(f"cannot pause SIP from status {status!r}")
    return SIP_STATUS_PAUSED


def resume_sip(
    status: str,
    *,
    day_of_month: int,
    as_of: dt.date,
    trading_dates: Set[dt.date],
) -> tuple[Literal["ACTIVE"], dt.date]:
    """PAUSED → ACTIVE and recompute ``next_fire_date`` from *as_of*."""
    if status == SIP_STATUS_ACTIVE:
        # Idempotent resume: still refresh next_fire so a long pause does not leave a stale date.
        return SIP_STATUS_ACTIVE, next_fire_date(
            day_of_month=day_of_month,
            as_of=as_of,
            trading_dates=trading_dates,
        )
    if status != SIP_STATUS_PAUSED:
        raise ValueError(f"cannot resume SIP from status {status!r}")
    return SIP_STATUS_ACTIVE, next_fire_date(
        day_of_month=day_of_month,
        as_of=as_of,
        trading_dates=trading_dates,
    )


def raise_sip_due(  # noqa: PLR0913 - one arg per pending-action field
    *,
    plan_id: int,
    investment_id: int,
    user_id: int,
    amount: Decimal,
    fire_date: dt.date,
    mode: str = SIP_MODE_REMINDER,
    already_fired_keys: Sequence[str] | Set[str] | None = None,
) -> SipDuePendingAction | None:
    """Build a SIP_DUE pending-action dict, or ``None`` when the fire key already exists.

    Pure values in / values out. The service layer persists the row and the bell item;
    this module never places an order.
    """
    assert_reminder_mode(mode)
    if amount <= 0:
        raise ValueError(f"SIP amount must be positive, got {amount}")
    key = fire_idempotency_key(plan_id, fire_date.year, fire_date.month)
    prior = set(already_fired_keys) if already_fired_keys is not None else set()
    if key in prior:
        return None
    payload: SipDuePayload = {
        "plan_id": plan_id,
        "investment_id": investment_id,
        "amount": format(amount, "f"),
        "fire_date": fire_date.isoformat(),
        "fire_key": key,
        "mode": SIP_MODE_REMINDER,
    }
    return {
        "type": SIP_DUE,
        "user_id": user_id,
        "payload": payload,
    }


def should_fire(
    *,
    status: str,
    next_fire: dt.date,
    as_of: dt.date,
) -> bool:
    """Whether an ACTIVE plan is due on *as_of* (``next_fire_date <= as_of``)."""
    return status == SIP_STATUS_ACTIVE and next_fire <= as_of


def evaluate_plan_for_fire(
    plan: Mapping[str, object],
    *,
    as_of: dt.date,
    trading_dates: Set[dt.date],
    already_fired_keys: Sequence[str] | Set[str] | None = None,
) -> tuple[SipDuePendingAction | None, dt.date | None]:
    """Pure Beat step for one plan row: maybe raise SIP_DUE and the advanced next_fire.

    *plan* keys: ``id``, ``investment_id``, ``user_id``, ``amount``, ``day_of_month``,
    ``mode``, ``status``, ``next_fire_date``. Returns ``(None, None)`` when not due or
    when the fire key was already consumed.
    """
    status = str(plan["status"])
    next_fire = plan["next_fire_date"]
    if not isinstance(next_fire, dt.date):
        raise TypeError("next_fire_date must be a date")
    if not should_fire(status=status, next_fire=next_fire, as_of=as_of):
        return None, None

    mode = str(plan["mode"])
    assert_reminder_mode(mode)
    amount = plan["amount"]
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    action = raise_sip_due(
        plan_id=int(plan["id"]),
        investment_id=int(plan["investment_id"]),
        user_id=int(plan["user_id"]),
        amount=amount,
        fire_date=next_fire,
        mode=mode,
        already_fired_keys=already_fired_keys,
    )
    if action is None:
        return None, None

    day_of_month = int(plan["day_of_month"])
    advanced = advance_next_fire_date(
        day_of_month=day_of_month,
        fired_on=next_fire,
        trading_dates=trading_dates,
    )
    return action, advanced
