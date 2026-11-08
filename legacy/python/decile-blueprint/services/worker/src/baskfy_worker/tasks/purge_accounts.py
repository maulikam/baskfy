"""Erase accounts whose soft-delete window has passed — Prompt 12 §5, docs/11 §Compliance.

    "Account deletion and data export endpoints (DPDP compliance), with a **7-day soft-delete
     window**."

`DELETE /me` deactivates immediately and records ``account_deletion.purge_after``. This is the
other half: a daily sweep that turns a deactivation into an erasure once the window has closed.
Without it the "soft" delete is the only delete, and DPDP asks for erasure.

Two outcomes, decided by whether the account ever paid
------------------------------------------------------
**No payments — the row is deleted.** Everything the account owns cascades from ``app_user``:
screens, portfolios, backtests, consent records, auth tokens, refresh tokens, and the
``account_deletion`` row itself.

**Payments exist — the row is anonymised in place.** India's tax law requires invoice records to
be retained, and DPDP's erasure right does not override a statutory retention obligation (docs/11
§"Compliance & legal (India)" requires GST-compliant invoices with a GSTIN and place of supply).
``payment.user_id`` is NOT NULL in docs/04, so the invoice cannot simply be detached — instead the
*person* is removed from the row that identifies them: the address becomes an opaque tombstone,
the name and the password hash go, and every child row that is not an invoice is deleted. What is
left identifies nobody, which is what erasure of personal data means.

Either way the evidence of the erasure is this step's ``pipeline_run_step`` note, which carries
public ids and never an address. `docs/12a` §9.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AccountDeletion,
    AppUser,
    AuthToken,
    ConsentRecord,
    Payment,
    Portfolio,
    RefreshToken,
    Screen,
)
from baskfy_worker.steps import StepOutcome

log = logging.getLogger(__name__)

#: The address an anonymised row carries. Unique per account (the public id is), never routable,
#: and obviously a tombstone to anyone reading the table.
TOMBSTONE_DOMAIN: Final = "deleted.invalid"

#: Everything keyed to a user that is not an invoice. Deleted explicitly on the anonymise path,
#: because there the `app_user` row survives and nothing cascades.
_OWNED_TABLES: Final = (Screen, Portfolio, ConsentRecord, AuthToken, RefreshToken)


@dataclass(slots=True)
class PurgeResult:
    considered: int = 0
    purged: int = 0
    anonymised: int = 0
    public_ids: list[str] = field(default_factory=list)


async def run_purge_accounts(
    session: AsyncSession, outcome: StepOutcome, now: dt.datetime | None = None
) -> int:
    """Erase every account whose window has closed. Idempotent: a purged row is skipped."""
    moment = now or dt.datetime.now(tz=dt.UTC)
    result = PurgeResult()

    due = (
        (
            await session.execute(
                select(AccountDeletion).where(
                    AccountDeletion.cancelled_at.is_(None),
                    AccountDeletion.purged_at.is_(None),
                    AccountDeletion.purge_after <= moment,
                )
            )
        )
        .scalars()
        .all()
    )
    result.considered = len(due)

    for row in due:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == row.user_id))
        ).scalar_one_or_none()
        if user is None:
            row.purged_at = moment
            continue
        if user.deleted_at is None:
            # Reactivated between the sweep's read and now, or by a path that forgot to cancel.
            # Either way this account is in use; leave it alone and say so.
            log.warning("skipping purge of a reactivated account", extra={"user_id": user.id})
            row.cancelled_at = moment
            continue

        result.public_ids.append(user.public_id)
        has_invoices = (
            await session.execute(
                select(func.count()).select_from(Payment).where(Payment.user_id == user.id)
            )
        ).scalar_one() > 0

        row.purged_at = moment
        await session.flush()

        if has_invoices:
            await _anonymise(session, user)
            result.anonymised += 1
        else:
            await session.execute(delete(AppUser).where(AppUser.id == user.id))
            # The delete cascaded `account_deletion` away; drop the orphaned instance from the
            # identity map so the next flush does not UPDATE a row that no longer exists.
            session.expunge(row)
        result.purged += 1

    await session.flush()
    outcome.rows_in = result.considered
    outcome.rows_out = result.purged
    outcome.note(
        # Public ids, never addresses: this log line outlives the account it names.
        purged_public_ids=result.public_ids or None,
        anonymised=result.anonymised or None,
    )
    return result.purged


async def _anonymise(session: AsyncSession, user: AppUser) -> None:
    """Remove the person from a row the invoice trail still has to point at."""
    for model in _OWNED_TABLES:
        await session.execute(delete(model).where(model.user_id == user.id))
    user.email = f"{user.public_id}@{TOMBSTONE_DOMAIN}"
    user.name = None
    user.password_hash = None
    user.email_verified_at = None
    await session.flush()
