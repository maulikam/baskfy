"""``vb_config`` — reading it, changing it, and refusing a change that crosses the ceiling.

The M4.1 boundary applied to the volume-breakout sleeve (``docs/03`` §3f, ``docs/vbt/03`` §1).
The module is deliberately the swing book's ``swing_settings`` with a different, shorter list,
because a reader who knows one should recognise the other; where the two differ, the difference
is the strategy's rather than the pattern's.

Three kinds of number, and the module exists to keep them apart:

**Settings** live in ``vb_config`` and belong to the person: the sleeve's capital, how many
positions, the per-position cap, and the stop percentage. Four, and no more — everything else in
``docs/vbt/04`` is a ``baskfy_core.vbt.config`` default, because a threshold that can be changed
in a form gets changed after a bad week (DECISIONS-VB PACK.5).

**Ceilings** live in the environment (``BASKFY_VBT_*_MAX``) and belong to the server. They are
the largest value a setting may take, are never editable, are never accepted in a payload, and a
request that crosses one is refused **with the ceiling named** — a refusal that says only "too
large" invites the caller to bisect their way to the limit.

**And two fields are neither.** ``dry_run_sessions`` is ``docs/vbt/02`` §3.1's gate and
``first_live_sessions_left`` is §3.5's countdown. Both are the evening job's memory of what has
actually happened, and :class:`VbtConfigPatch` has no field for either: a person who could set
the DRY_RUN counter to 20 has deleted the gate.

Nothing here reaches a broker, and nothing here can size a position. It stores numbers that
:mod:`baskfy_core.vbt.sizing` later reads.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import setting_above_ceiling
from baskfy_api.settings import Settings
from baskfy_core.models import VbConfig, VbConfigAudit
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG

#: Fields a person may change, in the order the settings form shows them.
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "sleeve_capital_inr",
    "max_open_positions",
    "max_position_pct",
    "stop_pct",
)

#: Fields only a job may change, and the job that owns each. A ``PATCH`` naming one of these is
#: refused by :class:`VbtConfigPatch` before it reaches the database — ``extra="forbid"`` —
#: rather than silently dropped, because silently dropping a field the caller sent is how a
#: settings page comes to believe it saved something it did not.
SYSTEM_OWNED_FIELDS: Final[Mapping[str, str]] = {
    #: `02` §3.1's gate. Twenty of these must have closed before the execution flag may flip.
    "dry_run_sessions": "vbt-evening",
    #: `04` §5.4's countdown, moved once when a LIVE session closes, never by a request.
    "first_live_sessions_left": "vbt-evening",
}

#: Which environment variable sets each ceiling. Module-level rather than a class attribute
#: because a frozen dataclass with a dict attribute is a mutable default, and because the map is
#: a property of the deployment's vocabulary rather than of any one instance.
CEILING_ENV: Final[Mapping[str, str]] = {
    "max_open_positions": "BASKFY_VBT_MAX_OPEN_POSITIONS_MAX",
    "max_position_pct": "BASKFY_VBT_MAX_POSITION_PCT_MAX",
    "stop_pct": "BASKFY_VBT_STOP_PCT_MAX",
}

#: The strategy's own slot count. A setting above it would ask the plan for eleven positions and
#: get ten (``04`` §9.1 takes the smaller), which is a form that lies rather than a wider book.
STRATEGY_MAX_SLOTS: Final[int] = DEFAULT_VBT_CONFIG.sizing.max_slots


@dataclass(frozen=True, slots=True)
class VbtCeilings:
    """The three server-side maxima, carried with the variable that sets each.

    Together, so the refusal, the settings form and the OpenAPI description all name the same
    thing without any of them hard-coding a string.
    """

    max_open_positions: int
    max_position_pct: Decimal
    stop_pct: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> VbtCeilings:
        return cls(
            max_open_positions=settings.vbt_max_open_positions_max,
            max_position_pct=settings.vbt_max_position_pct_max,
            stop_pct=settings.vbt_stop_pct_max,
        )

    def check(self, field: str, value: Decimal | int) -> None:
        """Raise :class:`baskfy_api.problems.Problem` (422) when ``value`` crosses the ceiling.

        Only the three bounded fields are checked; anything else is a no-op, so a caller may pass
        a whole patch through without first deciding which fields have ceilings.
        """
        ceiling = getattr(self, field, None)
        if ceiling is None:
            return
        if Decimal(str(value)) > Decimal(str(ceiling)):
            raise setting_above_ceiling(
                field=field, value=value, ceiling=ceiling, env_var=CEILING_ENV[field]
            )


class VbtConfigPatch(BaseModel):
    """A partial update. Every field optional; unset means "leave it alone".

    ``extra="forbid"`` is the load-bearing line. Without it, ``PATCH {"dry_run_sessions": 20}``
    would be accepted, ignored and answered ``200`` — the caller believing they had satisfied the
    gate by asking for it. With it, they are told the field does not exist here.

    The bounds on each field are the *engine's* limits, not the server's ceilings: a zero stop or
    a negative capital is nonsense at any ceiling, and nonsense is a 400. The ceilings are
    checked afterwards, in :func:`apply_patch`, and answer 422 — a different question with a
    different answer.
    """

    model_config = ConfigDict(extra="forbid")

    sleeve_capital_inr: Decimal | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)
    stop_pct: Decimal | None = Field(default=None, gt=0, le=100)

    def changes(self) -> dict[str, Decimal | int]:
        """The fields the caller actually set, as column values."""
        return dict(self.model_dump(exclude_unset=True, exclude_none=True))


class VbtConfigView(BaseModel):
    """What a reader gets: the settings, the ceilings, and the read-only system fields."""

    model_config = ConfigDict(from_attributes=True)

    sleeve_capital_inr: Decimal
    max_open_positions: int
    max_position_pct: Decimal
    stop_pct: Decimal
    #: Read-only. `02` §3.1's gate, shown as "7 of 20" and never as a control.
    dry_run_sessions: int
    first_live_sessions_left: int
    updated_at: dt.datetime
    updated_by: str | None
    #: The server's maxima, echoed so the form can render "max 15 — set by the server".
    ceilings: dict[str, str]
    #: Displayed as "Execution: disabled on this server" (``docs/vbt/05`` §2). Not a control,
    #: and deliberately not something the form can post back.
    execution_enabled: bool
    #: How many DRY_RUN sessions `02` §3.1 asks for. On the page beside the counter, so the
    #: sentence reads "7 of 20" without the page inventing the 20.
    dry_run_sessions_required: int


class VbtConfigNotSeeded(LookupError):
    """No ``vb_config`` row for this user.

    A read path does **not** create one. A request handler that seeds is a request handler that
    writes on a GET, and the row it writes carries defaults nobody chose. ``make seed`` owns
    creation (``baskfy-seed vbt``).
    """


async def read_config(session: AsyncSession, user_id: int) -> VbConfig:
    row = (
        await session.execute(select(VbConfig).where(VbConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise VbtConfigNotSeeded(
            f"no vb_config row for user {user_id}; run `make seed` (or `baskfy-seed vbt`)"
        )
    return row


def to_view(
    row: VbConfig,
    *,
    ceilings: VbtCeilings,
    execution_enabled: bool,
    dry_run_sessions_required: int,
) -> VbtConfigView:
    return VbtConfigView(
        sleeve_capital_inr=row.sleeve_capital_inr,
        max_open_positions=row.max_open_positions,
        max_position_pct=row.max_position_pct,
        stop_pct=row.stop_pct,
        dry_run_sessions=row.dry_run_sessions,
        first_live_sessions_left=row.first_live_sessions_left,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        ceilings={
            "max_open_positions": str(ceilings.max_open_positions),
            "max_position_pct": str(ceilings.max_position_pct),
            "stop_pct": str(ceilings.stop_pct),
        },
        execution_enabled=execution_enabled,
        dry_run_sessions_required=dry_run_sessions_required,
    )


async def apply_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    patch: VbtConfigPatch,
    ceilings: VbtCeilings,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> VbConfig:
    """Validate against the ceilings, write the row, and audit every field that moved.

    **Ceilings first, in one pass, before anything is written.** A patch that raises two values
    and crosses a ceiling on the second must change neither — otherwise a rejected save leaves
    the settings half-applied, and the person's next read shows a state they never asked for.

    The audit rows go in the same transaction as the change; a settings write whose audit failed
    is a settings write that did not happen.
    """
    row = await read_config(session, user_id)
    changes = patch.changes()
    for field, value in changes.items():
        ceilings.check(field, value)

    for field, value in changes.items():
        before = getattr(row, field)
        if str(before) == str(value):
            continue
        setattr(row, field, value)
        session.add(
            VbConfigAudit(
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
    # ``updated_at`` carries ``onupdate=func.now()``, so the flush expires it to pick up what the
    # database generated. Reading it afterwards would be a lazy load from synchronous code —
    # ``MissingGreenlet`` at serialisation time — so it is fetched here, where there is a
    # coroutine to await in.
    await session.refresh(row)
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
) -> VbConfig:
    """Write one of the fields a **job** owns (:data:`SYSTEM_OWNED_FIELDS`), with its audit.

    Separate from :func:`apply_patch` rather than a flag on it, so the only way to move the
    DRY_RUN counter or the first-live countdown is to call a function whose name says a job is
    doing it. ``changed_by`` is required for the same reason: "who counted that session" has to
    have an answer, and ``vbt-evening`` is a perfectly good one.
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
            VbConfigAudit(
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
        await session.refresh(row)
    return row


async def audit_trail(
    session: AsyncSession, *, user_id: int, limit: int = 50
) -> Sequence[VbConfigAudit]:
    """The most recent changes, newest first — what the settings page shows under the form."""
    result = await session.execute(
        select(VbConfigAudit)
        .where(VbConfigAudit.user_id == user_id)
        .order_by(VbConfigAudit.changed_at.desc(), VbConfigAudit.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
