"""``fo_book_config`` / ``fo_sleeve_config`` — reading, changing, refusing (FO5).

``options_settings``'s boundary, applied to the FO book (``docs/fno/03`` §6, ``02`` Track B
"Ceilings"), so a reader of one recognises the other.

**Settings** belong to the person: per sleeve group (``F1``, ``F2``, ``F3``) the capital, risk %,
max lots, max open positions and whether paper runs; for the book the monthly loss pause (0 = not
set, FO4.9). ``03`` §6 names capital and risk % as the config ("Config is per-sleeve capital, risk
%, max concurrent positions, and the book's loss pause. Every write goes through settings_audit")
and ``05`` §4 puts them on "the settings form, audited" (FO5.1). They are money-free in the sense
that matters: a write here cannot build, size into, confirm or place an order; it changes what a
*future* plan would size to, inside the ceilings, and every FO execution flag stays false.

**Ceilings** belong to the server (``BASKFY_FNO_*_MAX``): never editable, never in a payload, and a
request that crosses one is refused with the ceiling and its env var named. The per-trade ₹
ceiling is checked on the row as it would stand after the patch (capital x risk %).

**Strategy thresholds are not settings** (``baskfy_core.fno.config``, changed by a DECISIONS-FO
entry). **System-owned fields** — ``paused_until`` / ``paused_reason`` — are the loss ledger's
(FO10): the patch models have no field for them (``extra="forbid"``).

Every change writes one ``fo_config_audit`` row per field that moved.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import setting_above_ceiling
from baskfy_api.settings import Settings
from baskfy_core.fno.config import DEFAULT_FNO_CONFIG, FnoCeilings, FoSleeveGroup
from baskfy_core.models import FoBookConfig, FoConfigAudit, FoSleeveConfig

#: Settings field -> (``FnoCeilings`` attribute, the env var that sets it).
BOOK_CEILINGS: Final[Mapping[str, tuple[str, str]]] = {
    "monthly_pause_inr": ("book_monthly_loss_inr_max", "BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX"),
}
SLEEVE_CEILINGS: Final[Mapping[str, tuple[str, str]]] = {
    "risk_per_trade_pct": ("risk_pct_max", "BASKFY_FNO_RISK_PCT_MAX"),
    "max_open_positions": ("max_open_positions_max", "BASKFY_FNO_MAX_OPEN_POSITIONS_MAX"),
}
RISK_INR_ENV: Final = "BASKFY_FNO_RISK_PER_TRADE_INR_MAX"

#: ``04`` §3 / §10 engine bounds that are not env ceilings, per group.
MAX_LOTS_CEILING: Final = DEFAULT_FNO_CONFIG.common.max_lots_ceiling
#: F1: one structure per underlying (``f1_max_open_per_underlying`` = 1), so at most one per
#: underlying in ``f1_underlyings``.
F1_MAX_OPEN: Final = len(DEFAULT_FNO_CONFIG.f1.underlyings) * (
    DEFAULT_FNO_CONFIG.f1.max_open_per_underlying
)
#: F2: ``f2_max_open``'s bound, 1-10 (``04`` §10).
F2_MAX_OPEN: Final = 10
#: F3: one spread per underlying (``f3_max_open_per_underlying`` = 1; ``04`` §11).
F3_MAX_OPEN: Final = len(DEFAULT_FNO_CONFIG.f3.underlyings) * (
    DEFAULT_FNO_CONFIG.f3.max_open_per_underlying
)
_GROUP_MAX_OPEN: Final[Mapping[FoSleeveGroup, int]] = {
    FoSleeveGroup.F1: F1_MAX_OPEN,
    FoSleeveGroup.F2: F2_MAX_OPEN,
    FoSleeveGroup.F3: F3_MAX_OPEN,
}

SYSTEM_OWNED_FIELDS: Final[tuple[str, ...]] = ("paused_until", "paused_reason")


def ceilings_from_settings(settings: Settings) -> FnoCeilings:
    """``02`` "Ceilings" in force, as the pure core takes them."""
    return FnoCeilings(
        risk_per_trade_inr_max=settings.fno_risk_per_trade_inr_max,
        risk_pct_max=settings.fno_risk_pct_max,
        max_open_positions_max=settings.fno_max_open_positions_max,
        max_per_underlying_max=settings.fno_max_per_underlying_max,
        book_monthly_loss_inr_max=settings.fno_book_monthly_loss_inr_max,
    )


class FnoBookPatch(BaseModel):
    """A partial update of ``fo_book_config``."""

    model_config = ConfigDict(extra="forbid")

    monthly_pause_inr: Decimal | None = Field(default=None, ge=0)

    def changes(self) -> dict[str, object]:
        return dict(self.model_dump(exclude_unset=True, exclude_none=True))


class FnoSleevePatch(BaseModel):
    """A partial update of one ``fo_sleeve_config`` row (a sleeve group)."""

    model_config = ConfigDict(extra="forbid")

    capital_inr: Decimal | None = Field(default=None, ge=0)
    risk_per_trade_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_lots: int | None = Field(default=None, gt=0, le=MAX_LOTS_CEILING)
    max_open_positions: int | None = Field(default=None, gt=0)
    paper_enabled: bool | None = None

    def changes(self) -> dict[str, object]:
        return dict(self.model_dump(exclude_unset=True, exclude_none=True))


class FnoConfigNotSeeded(LookupError):
    """No row for this user; ``python -m baskfy_worker.fno_cli seed`` owns creation."""


class FnoSettingOutOfBounds(ValueError):
    """A value inside the ceilings but outside ``04``'s own bound for that sleeve."""

    def __init__(self, field: str, value: object, bound: object) -> None:
        super().__init__(f"{field} = {value} is above its bound {bound} (docs/fno/04)")
        self.field = field
        self.value = value
        self.bound = bound


def _check_ceiling(
    field: str, value: object, ceilings: FnoCeilings, table: Mapping[str, tuple[str, str]]
) -> None:
    if field not in table:
        return
    attr, env_var = table[field]
    ceiling = getattr(ceilings, attr)
    if Decimal(str(value)) > Decimal(str(ceiling)):
        raise setting_above_ceiling(field=field, value=value, ceiling=ceiling, env_var=env_var)


def check_book(changes: Mapping[str, object], ceilings: FnoCeilings) -> None:
    for field, value in changes.items():
        _check_ceiling(field, value, ceilings, BOOK_CEILINGS)


def check_sleeve(
    group: FoSleeveGroup,
    changes: Mapping[str, object],
    ceilings: FnoCeilings,
    *,
    current: FoSleeveConfig,
) -> None:
    """Every sleeve ceiling, the group's own bound on open positions, then the derived ₹ risk on
    the row as it would stand afterwards."""
    for field, value in changes.items():
        _check_ceiling(field, value, ceilings, SLEEVE_CEILINGS)
    open_bound = _GROUP_MAX_OPEN[group]
    wanted_open = changes.get("max_open_positions")
    if wanted_open is not None and int(str(wanted_open)) > open_bound:
        raise FnoSettingOutOfBounds("max_open_positions", wanted_open, open_bound)
    capital = Decimal(str(changes.get("capital_inr", current.capital_inr)))
    pct = Decimal(str(changes.get("risk_per_trade_pct", current.risk_per_trade_pct)))
    risk_inr = capital * pct / Decimal(100)
    if risk_inr > ceilings.risk_per_trade_inr_max:
        raise setting_above_ceiling(
            field="risk_per_trade_inr",
            value=risk_inr.quantize(Decimal("0.01")),
            ceiling=ceilings.risk_per_trade_inr_max,
            env_var=RISK_INR_ENV,
        )


async def read_book(session: AsyncSession, user_id: int) -> FoBookConfig:
    row = await session.get(FoBookConfig, user_id)
    if row is None:
        raise FnoConfigNotSeeded(f"no fo_book_config row for user {user_id}")
    return row


async def read_sleeve(session: AsyncSession, user_id: int, group: FoSleeveGroup) -> FoSleeveConfig:
    row = await session.get(FoSleeveConfig, (user_id, group.value))
    if row is None:
        raise FnoConfigNotSeeded(f"no fo_sleeve_config row for user {user_id} {group}")
    return row


def _audit(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    scope: str,
    row: FoBookConfig | FoSleeveConfig,
    changes: Mapping[str, object],
    changed_by: str,
    now: dt.datetime,
) -> None:
    for field, value in changes.items():
        before = getattr(row, field)
        if str(before) == str(value):
            continue
        setattr(row, field, value)
        session.add(
            FoConfigAudit(
                user_id=user_id,
                scope=scope,
                key=field,
                old_value=None if before is None else str(before),
                new_value=str(value),
                changed_at=now,
                changed_by=changed_by,
                note="FO5 settings PATCH",
            )
        )
    row.updated_by = changed_by


async def apply_book_patch(
    session: AsyncSession,
    *,
    user_id: int,
    patch: FnoBookPatch,
    changed_by: str,
    now: dt.datetime,
) -> None:
    row = await read_book(session, user_id)
    _audit(
        session,
        user_id=user_id,
        scope="BOOK",
        row=row,
        changes=patch.changes(),
        changed_by=changed_by,
        now=now,
    )
    await session.flush()


async def apply_sleeve_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    group: FoSleeveGroup,
    patch: FnoSleevePatch,
    changed_by: str,
    now: dt.datetime,
) -> None:
    row = await read_sleeve(session, user_id, group)
    _audit(
        session,
        user_id=user_id,
        scope=group.value,
        row=row,
        changes=patch.changes(),
        changed_by=changed_by,
        now=now,
    )
    await session.flush()


async def audit_trail(
    session: AsyncSession, *, user_id: int, limit: int = 50
) -> Sequence[FoConfigAudit]:
    """Newest first."""
    result = await session.execute(
        select(FoConfigAudit)
        .where(FoConfigAudit.user_id == user_id)
        .order_by(FoConfigAudit.changed_at.desc(), FoConfigAudit.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
