"""``op_book_config`` / ``op_sleeve_config`` — reading, changing, and refusing (OP2, M4.1).

The M4.1 boundary applied to the options book (``docs/options/03`` §7, ``02`` "Ceilings"), in the
shape of ``twt_settings`` so a reader of one recognises the other. Three kinds of number:

**Settings** belong to the person: the book's account, margin pool and the two loss limits (0 =
derived, ``04`` §9.3), and per sleeve group the capital, risk %, max lots, whether paper runs, and
the hard exit. **Strategy thresholds are not settings** — they are ``baskfy_core.options.config``
fields, changed by a ``DECISIONS-OP`` entry (condor PACK.7 carried as PACK.2).

**Ceilings** belong to the server (``BASKFY_OPTIONS_*_MAX``, ``BASKFY_OPTIONS_HARD_EXIT_LATEST``):
never editable, never accepted in a payload, and a request that crosses one is refused **with the
ceiling and its env var named**. The per-trade ₹ ceiling is checked on the row *as it would be
after the patch* (capital times risk %), because either field can push it over.

**System-owned fields** — ``paused_until`` / ``paused_reason`` — are the risk ledger's (OP11). The
patch models have no field for them: ``extra="forbid"`` answers "no such field" rather than
accepting and ignoring a request to lift a pause.

**The underlying is NIFTY** (``02`` Track C §5, PACK.12). A patch naming any other underlying — on
either model — is refused with ``underlying-not-allowed`` (422) before anything else is read, and
``op_book_config`` carries ``CHECK (underlying = 'NIFTY')`` as the database's own refusal.

Nothing here reaches a broker, sizes a position, or sets a capital on anyone's behalf.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.problems import setting_above_ceiling, underlying_not_allowed
from baskfy_api.settings import Settings
from baskfy_core.models import OpBookConfig, OpConfigAudit, OpSleeveConfig
from baskfy_core.options.config import OptionsCeilings, SleeveGroup

#: ``02`` Track C §5: the only underlying in v1.
ALLOWED_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY",)

#: Settings field -> (``OptionsCeilings`` attribute, the env var that sets it).
BOOK_CEILINGS: Final[Mapping[str, tuple[str, str]]] = {
    "daily_loss_limit_inr": ("book_daily_loss_inr_max", "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX"),
    "monthly_pause_inr": ("book_monthly_loss_inr_max", "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX"),
}
SLEEVE_CEILINGS: Final[Mapping[str, tuple[str, str]]] = {
    "risk_per_trade_pct": ("risk_pct_max", "BASKFY_OPTIONS_RISK_PCT_MAX"),
    "max_lots": ("max_lots_max", "BASKFY_OPTIONS_MAX_LOTS_MAX"),
    "hard_exit_time": ("hard_exit_latest", "BASKFY_OPTIONS_HARD_EXIT_LATEST"),
}
#: The derived per-trade ₹ risk (``sleeve_capital_inr * risk_per_trade_pct / 100``).
RISK_INR_ENV: Final = "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX"

#: Fields only a job may change (the risk ledger, OP11). Named so the boundary test can assert
#: neither patch model has them.
SYSTEM_OWNED_FIELDS: Final[tuple[str, ...]] = ("paused_until", "paused_reason")


def ceilings_from_settings(settings: Settings) -> OptionsCeilings:
    """``02`` "Ceilings" in force, as the pure core takes them."""
    return OptionsCeilings(
        risk_per_trade_inr_max=settings.options_risk_per_trade_inr_max,
        risk_pct_max=settings.options_risk_pct_max,
        max_lots_max=settings.options_max_lots_max,
        book_daily_loss_inr_max=settings.options_book_daily_loss_inr_max,
        book_monthly_loss_inr_max=settings.options_book_monthly_loss_inr_max,
        hard_exit_latest=settings.options_hard_exit_latest,
    )


def refuse_other_underlying(raw: object) -> None:
    """422 when a payload names any underlying but NIFTY, whichever config it targets."""
    if isinstance(raw, Mapping) and "underlying" in raw:
        value = raw["underlying"]
        if str(value).upper().strip() not in ALLOWED_UNDERLYINGS:
            raise underlying_not_allowed(value=value, allowed=ALLOWED_UNDERLYINGS)


class OptionsBookPatch(BaseModel):
    """A partial update of ``op_book_config``. Engine bounds here (400); ceilings later (422)."""

    model_config = ConfigDict(extra="forbid")

    underlying: str | None = None
    account_inr: Decimal | None = Field(default=None, ge=0)
    margin_pool_inr: Decimal | None = Field(default=None, ge=0)
    daily_loss_limit_inr: Decimal | None = Field(default=None, ge=0)
    monthly_pause_inr: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _nifty_only(cls, data: object) -> object:
        refuse_other_underlying(data)
        return data

    def changes(self) -> dict[str, object]:
        out = dict(self.model_dump(exclude_unset=True, exclude_none=True))
        if "underlying" in out:
            out["underlying"] = str(out["underlying"]).upper().strip()
        return out


class OptionsSleevePatch(BaseModel):
    """A partial update of one ``op_sleeve_config`` row (a sleeve group)."""

    model_config = ConfigDict(extra="forbid")

    sleeve_capital_inr: Decimal | None = Field(default=None, ge=0)
    risk_per_trade_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_lots: int | None = Field(default=None, gt=0)
    paper_enabled: bool | None = None
    hard_exit_time: dt.time | None = None

    @model_validator(mode="before")
    @classmethod
    def _nifty_only(cls, data: object) -> object:
        # A sleeve row has no underlying of its own (it inherits the book's), so even "NIFTY" is
        # an unknown field here; anything else is refused as the scope, not as the JSON.
        refuse_other_underlying(data)
        return data

    def changes(self) -> dict[str, object]:
        return dict(self.model_dump(exclude_unset=True, exclude_none=True))


def _check_ceiling(
    field: str, value: object, ceilings: OptionsCeilings, table: Mapping[str, tuple[str, str]]
) -> None:
    if field not in table:
        return
    attr, env_var = table[field]
    ceiling = getattr(ceilings, attr)
    over = (
        value > ceiling
        if isinstance(value, dt.time)
        else Decimal(str(value)) > Decimal(str(ceiling))
    )
    if over:
        raise setting_above_ceiling(field=field, value=value, ceiling=ceiling, env_var=env_var)


def check_book(changes: Mapping[str, object], ceilings: OptionsCeilings) -> None:
    for field, value in changes.items():
        _check_ceiling(field, value, ceilings, BOOK_CEILINGS)


def check_sleeve(
    changes: Mapping[str, object], ceilings: OptionsCeilings, *, current: OpSleeveConfig | None
) -> None:
    """Every sleeve ceiling, then the derived ₹ risk on the row as it would stand afterwards."""
    for field, value in changes.items():
        _check_ceiling(field, value, ceilings, SLEEVE_CEILINGS)
    capital = changes.get("sleeve_capital_inr", current.sleeve_capital_inr if current else 0)
    pct = changes.get("risk_per_trade_pct", current.risk_per_trade_pct if current else 0)
    risk_inr = Decimal(str(capital)) * Decimal(str(pct)) / Decimal(100)
    if risk_inr > ceilings.risk_per_trade_inr_max:
        raise setting_above_ceiling(
            field="risk_per_trade_inr",
            value=risk_inr.quantize(Decimal("0.01")),
            ceiling=ceilings.risk_per_trade_inr_max,
            env_var=RISK_INR_ENV,
        )


class OptionsConfigNotSeeded(LookupError):
    """No row for this user; ``python -m baskfy_worker.options_cli seed`` owns creation."""


async def read_book(session: AsyncSession, user_id: int) -> OpBookConfig:
    row = await session.get(OpBookConfig, user_id)
    if row is None:
        raise OptionsConfigNotSeeded(f"no op_book_config row for user {user_id}")
    return row


async def read_sleeve(session: AsyncSession, user_id: int, sleeve: SleeveGroup) -> OpSleeveConfig:
    row = await session.get(OpSleeveConfig, (user_id, sleeve.value))
    if row is None:
        raise OptionsConfigNotSeeded(f"no op_sleeve_config row for user {user_id} {sleeve}")
    return row


def _audit(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    scope: str,
    row: OpBookConfig | OpSleeveConfig,
    changes: Mapping[str, object],
    changed_by: str,
    now: dt.datetime,
    note: str | None,
) -> None:
    for field, value in changes.items():
        before = getattr(row, field)
        if str(before) == str(value):
            continue
        setattr(row, field, value)
        session.add(
            OpConfigAudit(
                user_id=user_id,
                scope=scope,
                key=field,
                old_value=None if before is None else str(before),
                new_value=str(value),
                changed_at=now,
                changed_by=changed_by,
                note=note,
            )
        )
    row.updated_by = changed_by


async def apply_book_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    patch: OptionsBookPatch,
    ceilings: OptionsCeilings,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> OpBookConfig:
    """Bounds first, in one pass, before anything is written; then the row and its audit."""
    row = await read_book(session, user_id)
    changes = patch.changes()
    check_book(changes, ceilings)
    _audit(
        session,
        user_id=user_id,
        scope="BOOK",
        row=row,
        changes=changes,
        changed_by=changed_by,
        now=now,
        note=note,
    )
    await session.flush()
    await session.refresh(row)
    return row


async def apply_sleeve_patch(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    sleeve: SleeveGroup,
    patch: OptionsSleevePatch,
    ceilings: OptionsCeilings,
    changed_by: str,
    now: dt.datetime,
    note: str | None = None,
) -> OpSleeveConfig:
    """Bounds first (including the derived ₹ risk), then the row and its audit."""
    row = await read_sleeve(session, user_id, sleeve)
    changes = patch.changes()
    check_sleeve(changes, ceilings, current=row)
    _audit(
        session,
        user_id=user_id,
        scope=sleeve.value,
        row=row,
        changes=changes,
        changed_by=changed_by,
        now=now,
        note=note,
    )
    await session.flush()
    await session.refresh(row)
    return row


async def audit_trail(
    session: AsyncSession, *, user_id: int, limit: int = 50
) -> Sequence[OpConfigAudit]:
    """Newest first — what the settings page shows under the form (OP5)."""
    result = await session.execute(
        select(OpConfigAudit)
        .where(OpConfigAudit.user_id == user_id)
        .order_by(OpConfigAudit.changed_at.desc(), OpConfigAudit.id.desc())
        .limit(limit)
    )
    return list(result.scalars())
