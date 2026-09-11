"""``tw_config`` — reading it, changing it, and refusing a change that crosses a bound.

The M4.1 boundary applied to the three-weeks-tight sleeve (``docs/03`` §3f, ``docs/twt/03`` §1,
``docs/twt/02`` §4). The module is deliberately ``vbt_settings`` and ``swing_settings`` with a
different, slightly longer list, because a reader who knows one should recognise the other; where
this one differs, **the difference is the strategy's rather than the pattern's** — and it differs
in exactly one place, which the next paragraph is about.

Three kinds of number, and the module exists to keep them apart:

**Settings** live in ``tw_config`` and belong to the person: the sleeve's capital, how many
positions, the per-position cap, the stop percentage and the trail percentage. Five, and no more
— everything else in ``docs/twt/04`` is a ``baskfy_core.twt.config`` default, because a threshold
that can be changed in a form gets changed after a bad week (``02`` §4).

**Bounds** live in the environment (``BASKFY_TWT_*``) and belong to the server. They are never
editable, never accepted in a payload, and a request that crosses one is refused **with the bound
named** — a refusal that says only "too large" invites the caller to bisect their way to the
limit.

**And two fields are neither.** ``dry_run_sessions`` is ``02`` §3's information counter and
``first_live_entries_left`` is §3.6's countdown. Both are the evening job's memory of what has
actually happened, and :class:`TwtConfigPatch` has no field for either: a person who could set
the first-live counter could delete the half-size discipline by asking for it.

THE ONE BOUND THAT IS A FLOOR
-----------------------------
``trail_pct`` is bounded **below**, by ``BASKFY_TWT_TRAIL_PCT_MIN`` [18.00], and **not above.**

Every other bounded setting in this repository is capped above, because the risk being managed is
somebody making a position bigger than the book can carry. Here the measured cliff is in the
other direction: the trail is TWT-1's only exit — 137 of the research's 164 exits — and
tightening it from 20 % to 15 % took the CAGR from 20.9 % to 9.6 % and the drawdown from -24.7 %
to -43 %. Widening it is merely unprofitable (30 % → 15.5 % on 73 trades); tightening it is the
failure mode. **The ceiling that matters here is a floor.** DECISIONS-TW **TW0.5**.

It is very easy to implement this backwards, which is why it has its own problem type
(:data:`baskfy_api.problems.ProblemType.SETTING_BELOW_FLOOR`), its own env var suffix (``_MIN``,
not ``_MAX``), and its own named test.

Nothing here reaches a broker, and nothing here can size a position. It stores numbers that
``baskfy_core.twt.sizing`` and ``baskfy_core.twt.exits`` later read. In particular **nothing here
sets ``sleeve_capital_inr``**: the seeder writes 0, the person writes the rest, and the root
``CLAUDE.md`` safety rails say no agent ever does.
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

from baskfy_api.problems import setting_above_ceiling, setting_below_floor
from baskfy_api.settings import Settings
from baskfy_core.models import TwConfig, TwConfigAudit

#: Fields a person may change, in the order the settings form shows them.
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "sleeve_capital_inr",
    "max_open_positions",
    "max_position_pct",
    "stop_pct",
    "trail_pct",
)

#: Fields only a job may change, and the job that owns each. A ``PATCH`` naming one of these is
#: refused by :class:`TwtConfigPatch` before it reaches the database — ``extra="forbid"`` —
#: rather than silently dropped, because silently dropping a field the caller sent is how a
#: settings page comes to believe it saved something it did not.
SYSTEM_OWNED_FIELDS: Final[Mapping[str, str]] = {
    #: `02` §3's information counter: how many DRY_RUN sessions closed with a plan built.
    "dry_run_sessions": "twt-evening",
    #: `04` §6.4's countdown, moved once per **filled** entry by the session that filled it,
    #: never by a request and never by a plan nobody confirmed.
    "first_live_entries_left": "twt-evening",
}

#: Which environment variable sets each **ceiling** (a maximum). Module-level rather than a class
#: attribute because a frozen dataclass with a dict attribute is a mutable default, and because
#: the map is a property of the deployment's vocabulary rather than of any one instance.
CEILING_ENV: Final[Mapping[str, str]] = {
    "max_open_positions": "BASKFY_TWT_MAX_OPEN_POSITIONS_MAX",
    "max_position_pct": "BASKFY_TWT_MAX_POSITION_PCT_MAX",
    "stop_pct": "BASKFY_TWT_STOP_PCT_MAX",
}

#: Which environment variable sets each **floor** (a minimum). One entry, and see the module
#: docstring for why it is not in the map above. Kept as a map rather than a special case in
#: :meth:`TwtCeilings.check` so that a second one, if the measurements ever produce one, is a
#: line here rather than a branch.
FLOOR_ENV: Final[Mapping[str, str]] = {
    "trail_pct": "BASKFY_TWT_TRAIL_PCT_MIN",
}

#: The strategy's own slot count (``04`` §6.1). A setting above it would ask the plan for eleven
#: positions and get ten, which is a form that lies rather than a wider book. Spelled out rather
#: than imported from ``baskfy_core.twt.config``, which TW1 owns; ``test_twt_ceilings.py`` pins
#: it to ``docs/twt/04`` §6.1.
STRATEGY_MAX_SLOTS: Final[int] = 10


@dataclass(frozen=True, slots=True)
class TwtCeilings:
    """The server-side bounds, carried with the variable that sets each.

    Together, so the refusal, the settings form and the OpenAPI description all name the same
    thing without any of them hard-coding a string.

    The class is called ``Ceilings`` for symmetry with its two siblings even though one of its
    four members is a floor; :attr:`trail_pct_min`'s name is where the difference is said out
    loud, and :meth:`check` is where it is enforced.
    """

    max_open_positions: int
    max_position_pct: Decimal
    stop_pct: Decimal
    #: A **minimum**. See the module docstring and DECISIONS-TW TW0.5.
    trail_pct_min: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> TwtCeilings:
        return cls(
            max_open_positions=settings.twt_max_open_positions_max,
            max_position_pct=settings.twt_max_position_pct_max,
            stop_pct=settings.twt_stop_pct_max,
            trail_pct_min=settings.twt_trail_pct_min,
        )

    def bound_for(self, field: str) -> Decimal | int | None:
        """The bound in force for ``field``, whichever direction it runs in, or ``None``.

        Used by :func:`to_view` so the form can render "max 15 — set by the server" and
        "min 18 — set by the server" from one place.
        """
        if field in CEILING_ENV:
            ceiling: Decimal | int = getattr(self, field)
            return ceiling
        if field in FLOOR_ENV:
            return self.trail_pct_min
        return None

    def check(self, field: str, value: Decimal | int) -> None:
        """Raise :class:`baskfy_api.problems.Problem` (422) when ``value`` crosses its bound.

        Only the four bounded fields are checked; anything else is a no-op, so a caller may pass
        a whole patch through without first deciding which fields have bounds.

        **``trail_pct`` is compared the other way round.** A 30 % trail is legal and a 15 % one
        is not, which is the opposite of every other line in this method and is the whole of
        TW0.5.
        """
        requested = Decimal(str(value))
        if field in CEILING_ENV:
            ceiling = Decimal(str(getattr(self, field)))
            if requested > ceiling:
                raise setting_above_ceiling(
                    field=field,
                    value=value,
                    ceiling=getattr(self, field),
                    env_var=CEILING_ENV[field],
                )
            return
        if field in FLOOR_ENV:
            if requested < Decimal(str(self.trail_pct_min)):
                raise setting_below_floor(
                    field=field,
                    value=value,
                    floor=self.trail_pct_min,
                    env_var=FLOOR_ENV[field],
                )
            return


class TwtConfigPatch(BaseModel):
    """A partial update. Every field optional; unset means "leave it alone".

    ``extra="forbid"`` is the load-bearing line. Without it, ``PATCH
    {"first_live_entries_left": 0}`` would be accepted, ignored and answered ``200`` — the caller
    believing they had skipped the half-size discipline by asking for it. With it, they are told
    the field does not exist here.

    The bounds on each field are the *engine's* limits, not the server's: a zero stop or a
    negative capital is nonsense at any bound, and nonsense is a 400. The server's bounds are
    checked afterwards, in :func:`apply_patch`, and answer 422 — a different question with a
    different answer.
    """

    model_config = ConfigDict(extra="forbid")

    sleeve_capital_inr: Decimal | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)
    stop_pct: Decimal | None = Field(default=None, gt=0, le=100)
    trail_pct: Decimal | None = Field(default=None, gt=0, le=100)

    def changes(self) -> dict[str, Decimal | int]:
        """The fields the caller actually set, as column values."""
        return dict(self.model_dump(exclude_unset=True, exclude_none=True))


class TwtConfigView(BaseModel):
    """What a reader gets: the settings, the bounds, and the read-only system fields."""

    model_config = ConfigDict(from_attributes=True)

    sleeve_capital_inr: Decimal
    max_open_positions: int
    max_position_pct: Decimal
    stop_pct: Decimal
    trail_pct: Decimal
    #: Read-only. `02` §3: information, not a gate — there is no paper phase and no session
    #: count to satisfy, and the page says so beside the number.
    dry_run_sessions: int
    first_live_entries_left: int
    updated_at: dt.datetime
    updated_by: str | None
    #: The server's maxima, echoed so the form can render "max 15 — set by the server".
    ceilings: dict[str, str]
    #: The server's minima — one entry, ``trail_pct``. Separate from ``ceilings`` so the form
    #: renders "min 18" rather than "max 18", which is the mistake this whole module is
    #: arranged to prevent.
    floors: dict[str, str]
    #: Displayed as "Execution: disabled on this server". Not a control, and deliberately not
    #: something the form can post back.
    execution_enabled: bool


class TwtConfigNotSeeded(LookupError):
    """No ``tw_config`` row for this user.

    A read path does **not** create one. A request handler that seeds is a request handler that
    writes on a GET, and the row it writes carries defaults nobody chose. ``make seed`` owns
    creation (``baskfy-seed twt``).
    """


async def read_config(session: AsyncSession, user_id: int) -> TwConfig:
    row = (
        await session.execute(select(TwConfig).where(TwConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise TwtConfigNotSeeded(
            f"no tw_config row for user {user_id}; run `make seed` (or `baskfy-seed twt`)"
        )
    return row


def to_view(row: TwConfig, *, ceilings: TwtCeilings, execution_enabled: bool) -> TwtConfigView:
    return TwtConfigView(
        sleeve_capital_inr=row.sleeve_capital_inr,
        max_open_positions=row.max_open_positions,
        max_position_pct=row.max_position_pct,
        stop_pct=row.stop_pct,
        trail_pct=row.trail_pct,
        dry_run_sessions=row.dry_run_sessions,
        first_live_entries_left=row.first_live_entries_left,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        ceilings={field: str(ceilings.bound_for(field)) for field in CEILING_ENV},
        floors={field: str(ceilings.bound_for(field)) for field in FLOOR_ENV},
        execution_enabled=execution_enabled,
    )


async def apply_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    patch: TwtConfigPatch,
    ceilings: TwtCeilings,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> TwConfig:
    """Validate against the bounds, write the row, and audit every field that moved.

    **Bounds first, in one pass, before anything is written.** A patch that raises two values and
    crosses a bound on the second must change neither — otherwise a rejected save leaves the
    settings half-applied, and the person's next read shows a state they never asked for.

    The audit rows go in the same transaction as the change; a settings write whose audit failed
    is a settings write that did not happen. On this sleeve the audit is not decoration: a line
    is held for months, and "what was ``trail_pct`` on the morning that stop was armed" is a
    question ``updated_at`` cannot answer.
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
            TwConfigAudit(
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
) -> TwConfig:
    """Write one of the fields a **job** owns (:data:`SYSTEM_OWNED_FIELDS`), with its audit.

    Separate from :func:`apply_patch` rather than a flag on it, so the only way to move the
    DRY_RUN counter or the first-live countdown is to call a function whose name says a job is
    doing it. ``changed_by`` is required for the same reason: "who counted that entry" has to
    have an answer, and ``twt-evening`` is a perfectly good one.
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
            TwConfigAudit(
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
) -> Sequence[TwConfigAudit]:
    """The most recent changes, newest first — what the settings page shows under the form."""
    result = await session.execute(
        select(TwConfigAudit)
        .where(TwConfigAudit.user_id == user_id)
        .order_by(TwConfigAudit.changed_at.desc(), TwConfigAudit.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
