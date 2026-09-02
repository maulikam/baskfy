"""``sw_config`` — reading it, changing it, and refusing a change that crosses the ceiling.

The M4.1 boundary, applied to the swing book (``docs/03`` §3f, ``docs/swing/03`` §1). There are
two kinds of number here and the whole module exists to keep them apart:

**Settings** live in ``sw_config`` and belong to the person. Sleeve capital, risk per trade, the
position cap, how many positions, the opening-range window, the stop mode, the liquidity floors.
A person changes these; every change is audited.

**Ceilings** live in the environment (``BASKFY_SWING_*_MAX``) and belong to the server. They are
the largest value a setting may take. They are never returned as *editable*, never accepted in a
payload, and a request that exceeds one is refused with the ceiling named — because a refusal
that says only "too large" invites the caller to bisect their way to the limit.

**And two fields are neither.** ``exposure_level`` (the ladder rung) and
``first_live_sessions_left`` (the "start small" countdown of ``docs/swing/02`` §3.5) are the
system's memory of how the book has actually been doing. They are written by the EOD job and the
execute route through :func:`record_system_change`, and :class:`SwingConfigPatch` has no field
for either: a person who could set the rung to 3 after three losses has deleted the ladder.

Nothing in this module reaches a broker, and nothing in it can size a position. It stores
numbers that :mod:`baskfy_core.swing.sizing` later reads.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import setting_above_ceiling
from baskfy_api.settings import Settings
from baskfy_core.models import SwConfig, SwConfigAudit
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_core.swing.stops import StopMode

#: The opening-range windows ``docs/swing/04`` §7.1 admits. Read off the engine's own config so
#: the form and the monitor cannot disagree about what a valid window is.
OR_WINDOWS: Final[tuple[int, ...]] = DEFAULT_SWING_CONFIG.opening_range.windows_minutes

#: Fields a person may change, in the order the settings form shows them.
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "sleeve_capital_inr",
    "risk_per_trade_pct",
    "max_position_pct",
    "max_open_positions",
    "or_window_minutes",
    "stop_mode",
    "adr_min_pct",
    "turnover_min_inr",
    "price_min",
)

#: Fields only a job may change, and the job that owns each. A ``PATCH`` naming one of these is
#: refused by :class:`SwingConfigPatch` before it reaches the database — ``extra="forbid"`` —
#: rather than being silently dropped, because silently dropping a field the caller sent is how a
#: settings page comes to believe it saved something it did not.
SYSTEM_OWNED_FIELDS: Final[Mapping[str, str]] = {
    "exposure_level": "swing-eod",
    "first_live_sessions_left": "/swing/execute",
}


#: Which environment variable sets each ceiling. Module-level rather than a class attribute
#: because a frozen dataclass with a dict attribute is a mutable default, and because the map is
#: a property of the deployment's vocabulary rather than of any one instance.
CEILING_ENV: Final[Mapping[str, str]] = {
    "risk_per_trade_pct": "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
    "max_position_pct": "BASKFY_SWING_MAX_POSITION_PCT_MAX",
    "max_open_positions": "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX",
}


@dataclass(frozen=True, slots=True)
class SwingCeilings:
    """The three server-side maxima, with the variable that sets each.

    Carried together with their environment-variable names so the refusal, the settings form and
    the OpenAPI description all name the same thing without any of them hard-coding a string.
    """

    risk_per_trade_pct: Decimal
    max_position_pct: Decimal
    max_open_positions: int

    @classmethod
    def from_settings(cls, settings: Settings) -> SwingCeilings:
        return cls(
            risk_per_trade_pct=settings.swing_risk_per_trade_pct_max,
            max_position_pct=settings.swing_max_position_pct_max,
            max_open_positions=settings.swing_max_open_positions_max,
        )

    def check(self, field: str, value: Decimal | int) -> None:
        """Raise :class:`baskfy_api.problems.Problem` (422) when ``value`` crosses the ceiling.

        Only the three bounded fields are checked; anything else is a no-op, so a caller may pass
        the whole patch through without first deciding which fields have ceilings.
        """
        ceiling = getattr(self, field, None)
        if ceiling is None:
            return
        if Decimal(str(value)) > Decimal(str(ceiling)):
            raise setting_above_ceiling(
                field=field, value=value, ceiling=ceiling, env_var=CEILING_ENV[field]
            )


class SwingConfigPatch(BaseModel):
    """A partial update. Every field optional; unset means "leave it alone".

    ``extra="forbid"`` is the load-bearing line. Without it, ``PATCH {"exposure_level": 3}``
    would be accepted, ignored and answered ``200`` — the caller believing they had climbed the
    ladder by asking. With it, they are told the field does not exist here.

    The bounds on each field are the *engine's* limits, not the server's ceilings: a negative
    risk or a 0-minute opening range is nonsense at any ceiling, and nonsense is a 400. The
    ceilings are checked afterwards, in :func:`apply_patch`, and answer 422 — a different
    question with a different answer.
    """

    model_config = ConfigDict(extra="forbid")

    sleeve_capital_inr: Decimal | None = Field(default=None, ge=0)
    risk_per_trade_pct: Decimal | None = Field(default=None, gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_open_positions: int | None = Field(default=None, gt=0)
    or_window_minutes: Literal[1, 5, 60] | None = None
    stop_mode: StopMode | None = None
    adr_min_pct: Decimal | None = Field(default=None, ge=0)
    turnover_min_inr: Decimal | None = Field(default=None, ge=0)
    price_min: Decimal | None = Field(default=None, ge=0)

    def changes(self) -> dict[str, Decimal | int | str]:
        """The fields the caller actually set, as column values."""
        raw = self.model_dump(exclude_unset=True, exclude_none=True)
        return {
            key: (value.value if isinstance(value, StopMode) else value)
            for key, value in raw.items()
        }


class SwingConfigView(BaseModel):
    """What a reader gets: the settings, the ceilings, and the two read-only system fields."""

    model_config = ConfigDict(from_attributes=True)

    sleeve_capital_inr: Decimal
    risk_per_trade_pct: Decimal
    max_position_pct: Decimal
    max_open_positions: int
    or_window_minutes: int
    stop_mode: str
    adr_min_pct: Decimal
    turnover_min_inr: Decimal
    price_min: Decimal
    #: Read-only. Shown so a person can see the rung; never a control.
    exposure_level: int
    first_live_sessions_left: int
    updated_at: dt.datetime
    updated_by: str | None
    #: The server's maxima, echoed so the form can render "max 1.0% — set by the server".
    ceilings: dict[str, str]
    #: Displayed as "Execution: disabled on this server" (``docs/swing/05`` §2). Not a control,
    #: and deliberately not something the form can post back.
    execution_enabled: bool


class SwingConfigNotSeeded(LookupError):
    """No ``sw_config`` row for this user.

    A read path does **not** create one. ``curated_seed.resolve_sole_user_id`` learned this the
    hard way: a request handler that seeds is a request handler that writes on a GET, and the
    row it writes carries defaults nobody chose. ``make seed`` owns creation.
    """


async def read_config(session: AsyncSession, user_id: int) -> SwConfig:
    row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise SwingConfigNotSeeded(
            f"no sw_config row for user {user_id}; run `make seed` (or `baskfy-seed swing`)"
        )
    return row


def to_view(row: SwConfig, *, ceilings: SwingCeilings, execution_enabled: bool) -> SwingConfigView:
    return SwingConfigView(
        sleeve_capital_inr=row.sleeve_capital_inr,
        risk_per_trade_pct=row.risk_per_trade_pct,
        max_position_pct=row.max_position_pct,
        max_open_positions=row.max_open_positions,
        or_window_minutes=row.or_window_minutes,
        stop_mode=row.stop_mode,
        adr_min_pct=row.adr_min_pct,
        turnover_min_inr=row.turnover_min_inr,
        price_min=row.price_min,
        exposure_level=row.exposure_level,
        first_live_sessions_left=row.first_live_sessions_left,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        ceilings={
            "risk_per_trade_pct": str(ceilings.risk_per_trade_pct),
            "max_position_pct": str(ceilings.max_position_pct),
            "max_open_positions": str(ceilings.max_open_positions),
        },
        execution_enabled=execution_enabled,
    )


async def apply_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    patch: SwingConfigPatch,
    ceilings: SwingCeilings,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> SwConfig:
    """Validate against the ceilings, write the row, and audit every field that moved.

    **Ceilings first, in one pass, before anything is written.** A patch that raises two values
    and crosses a ceiling on the second must change neither — otherwise a rejected save leaves
    the settings half-applied, and the person's next read shows a state they never asked for.

    The audit rows go in the same transaction as the change (:class:`SwConfigAudit`); a settings
    write whose audit failed is a settings write that did not happen.
    """
    row = await read_config(session, user_id)
    changes = patch.changes()
    for field, value in changes.items():
        if isinstance(value, Decimal | int) and not isinstance(value, bool):
            ceilings.check(field, value)

    for field, value in changes.items():
        before = getattr(row, field)
        if str(before) == str(value):
            continue
        setattr(row, field, value)
        session.add(
            SwConfigAudit(
                user_id=user_id,
                key=field,
                old_value=None if before is None else str(before),
                new_value=str(value),
                changed_at=now,
                changed_by=changed_by,
                note=note,
            )
        )
    row.updated_by = changed_by
    await session.flush()
    return row


async def record_system_change(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    field: str,
    value: int,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> SwConfig:
    """Write one of the two fields a **job** owns (:data:`SYSTEM_OWNED_FIELDS`), with its audit.

    Separate from :func:`apply_patch` rather than a flag on it, so that the only way to move the
    exposure rung or the first-live countdown is to call a function whose name says a job is
    doing it. A ``changed_by`` is required for the same reason: "who raised the rung" has to have
    an answer, and ``swing-eod`` is a perfectly good one.
    """
    if field not in SYSTEM_OWNED_FIELDS:
        raise ValueError(
            f"{field!r} is not a system-owned field; the editable ones go through apply_patch "
            f"({', '.join(SYSTEM_OWNED_FIELDS)})"
        )
    row = await read_config(session, user_id)
    before = getattr(row, field)
    if before != value:
        setattr(row, field, value)
        session.add(
            SwConfigAudit(
                user_id=user_id,
                key=field,
                old_value=str(before),
                new_value=str(value),
                changed_at=now,
                changed_by=changed_by,
                note=note,
            )
        )
        row.updated_by = changed_by
        await session.flush()
    return row


async def audit_trail(
    session: AsyncSession, *, user_id: int, limit: int = 50
) -> Sequence[SwConfigAudit]:
    """The most recent changes, newest first — what the settings page shows under the form."""
    result = await session.execute(
        select(SwConfigAudit)
        .where(SwConfigAudit.user_id == user_id)
        .order_by(SwConfigAudit.changed_at.desc(), SwConfigAudit.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
