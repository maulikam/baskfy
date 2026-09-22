"""The risk ledger: per-sleeve limits in R, the per-trade breach, the book's ₹ limits (``04`` §9).

Per sleeve the limits are in **R** (PACK.13) because R exists in paper-one-lot mode too, so the
ledger is exercised before any capital exists; the book's limits are in ₹ and switch on only once
some sleeve has capital. A pause is a refusal, not a warning (§9.4): ``plan_refusal`` is what
``plan.build`` asks.

Everything here takes its dates as arguments (law 1). Weekly pauses run to the ISO week's last
trading day, which the caller's trading calendar names; monthly pauses to the calendar month's
last day.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.config import OptionsCeilings, RiskConfig, Sleeve

_HUNDRED = Decimal(100)


class PauseReason(StrEnum):
    DAILY_R = "DAILY_R"
    WEEKLY_R = "WEEKLY_R"
    MONTHLY_R = "MONTHLY_R"
    BOOK_DAILY_INR = "BOOK_DAILY_INR"
    BOOK_MONTHLY_INR = "BOOK_MONTHLY_INR"


@dataclass(frozen=True, slots=True)
class RealisedTrade:
    """One closed journal row as the ledger reads it: when, how much, and in R."""

    sleeve: Sleeve
    trade_date: dt.date
    net_pnl_inr: Decimal
    r_multiple: Decimal


@dataclass(frozen=True, slots=True)
class Pause:
    """``paused_until`` is inclusive. ``close_now`` asks the desk to close the sleeve's (or the
    book's) open position at once."""

    paused_until: dt.date
    reasons: tuple[PauseReason, ...]
    close_now: bool


def _week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    monday = day - dt.timedelta(days=day.isoweekday() - 1)
    return monday, monday + dt.timedelta(days=6)


def last_trading_day_of_week(day: dt.date, trading_days: Iterable[dt.date]) -> dt.date:
    """The ISO week's last trading day on or after ``day``; ``day`` itself if the calendar has
    none later that week."""
    _, sunday = _week_bounds(day)
    inside = [d for d in trading_days if day <= d <= sunday]
    return max(inside) if inside else day


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=_calendar.monthrange(day.year, day.month)[1])


def evaluate_sleeve(  # noqa: PLR0913 - every input 04 §9.1 names, by keyword
    *,
    sleeve: Sleeve,
    today: dt.date,
    history: Sequence[RealisedTrade],
    today_marked_inr: Decimal,
    r_today_inr: Decimal,
    trading_days: Iterable[dt.date],
    config: RiskConfig,
) -> Pause | None:
    """``04`` §9.1 for one sleeve, at a close or at 09:00.

    * today's realised + marked net ₹ ÷ today's R ≤ -``daily_loss_r`` → close the position and
      pause for the day (``DAILY_R``);
    * the ISO week's realised R ≤ -``weekly_loss_r`` → paused to the week's last trading day;
    * the calendar month's realised R ≤ -``monthly_loss_r`` → paused to month-end.

    Realised R is the sum of each row's own ``r_multiple`` (each trade is measured against the R
    it was taken at). Several limits at once: every reason is reported and the latest date wins.
    Rows of another sleeve in ``history`` are a caller bug and raise — the ledger never pools.
    """
    if any(row.sleeve is not sleeve for row in history):
        raise ValueError("evaluate_sleeve reads one sleeve's rows; never pool sleeves")
    if r_today_inr <= 0:
        raise ValueError("R must be positive")
    found: list[tuple[PauseReason, dt.date]] = []
    realised_today = sum((r.net_pnl_inr for r in history if r.trade_date == today), Decimal(0))
    if (realised_today + today_marked_inr) / r_today_inr <= -config.daily_loss_r:
        found.append((PauseReason.DAILY_R, today))
    monday, sunday = _week_bounds(today)
    week_r = sum((r.r_multiple for r in history if monday <= r.trade_date <= sunday), Decimal(0))
    if week_r <= -config.weekly_loss_r:
        found.append((PauseReason.WEEKLY_R, last_trading_day_of_week(today, trading_days)))
    month_r = sum(
        (
            r.r_multiple
            for r in history
            if (r.trade_date.year, r.trade_date.month) == (today.year, today.month)
            and r.trade_date <= today
        ),
        Decimal(0),
    )
    if month_r <= -config.monthly_loss_r:
        found.append((PauseReason.MONTHLY_R, month_end(today)))
    if not found:
        return None
    return Pause(
        paused_until=max(until for _, until in found),
        reasons=tuple(reason for reason, _ in found),
        close_now=any(reason is PauseReason.DAILY_R for reason, _ in found),
    )


def trade_breached(marked_loss_inr: Decimal, risk_budget_inr: Decimal, config: RiskConfig) -> bool:
    """``04`` §9.2: a marked loss beyond ``risk_budget * budget_breach_frac`` closes the position
    (``STOP``) whatever the sleeve's own rule says. ``marked_loss_inr`` is a positive loss."""
    return marked_loss_inr > risk_budget_inr * config.budget_breach_frac


@dataclass(frozen=True, slots=True)
class BookLimits:
    daily_loss_inr: Decimal
    monthly_pause_inr: Decimal


def book_limits(
    *,
    sleeve_capitals_inr: Iterable[Decimal],
    daily_loss_limit_inr: Decimal,
    monthly_pause_inr: Decimal,
    config: RiskConfig,
    ceilings: OptionsCeilings,
) -> BookLimits | None:
    """``04`` §9.3 — ``None`` until some sleeve has capital; a 0 setting is derived (1.5 % and
    5 % of Σ sleeve capital); each is capped by its env ceiling."""
    total = sum(sleeve_capitals_inr, Decimal(0))
    if total <= 0:
        return None
    daily = (
        daily_loss_limit_inr
        if daily_loss_limit_inr > 0
        else total * config.book_daily_loss_pct / _HUNDRED
    )
    monthly = (
        monthly_pause_inr
        if monthly_pause_inr > 0
        else total * config.book_monthly_loss_pct / _HUNDRED
    )
    return BookLimits(
        daily_loss_inr=min(daily, ceilings.book_daily_loss_inr_max),
        monthly_pause_inr=min(monthly, ceilings.book_monthly_loss_inr_max),
    )


def evaluate_book(
    *,
    today: dt.date,
    today_net_inr: Decimal,
    month_realised_inr: Decimal,
    limits: BookLimits | None,
) -> Pause | None:
    """A daily breach closes every open ``op_position`` and pauses the book for the day; a monthly
    breach pauses it to month-end (``04`` §9.3). Inputs are net ₹ across every sleeve."""
    if limits is None:
        return None
    found: list[tuple[PauseReason, dt.date]] = []
    if today_net_inr <= -limits.daily_loss_inr:
        found.append((PauseReason.BOOK_DAILY_INR, today))
    if month_realised_inr <= -limits.monthly_pause_inr:
        found.append((PauseReason.BOOK_MONTHLY_INR, month_end(today)))
    if not found:
        return None
    return Pause(
        paused_until=max(until for _, until in found),
        reasons=tuple(reason for reason, _ in found),
        close_now=any(reason is PauseReason.BOOK_DAILY_INR for reason, _ in found),
    )


def is_paused(paused_until: dt.date | None, today: dt.date) -> bool:
    return paused_until is not None and today <= paused_until


REJECTED_PAUSED = "REJECTED_PAUSED"


def plan_refusal(
    sleeve_paused_until: dt.date | None, book_paused_until: dt.date | None, today: dt.date
) -> str | None:
    """``04`` §9.4: ``REJECTED_PAUSED`` while the sleeve or the book is paused, else ``None``."""
    if is_paused(sleeve_paused_until, today) or is_paused(book_paused_until, today):
        return REJECTED_PAUSED
    return None
