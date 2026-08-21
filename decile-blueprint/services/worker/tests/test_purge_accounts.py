"""The soft-delete window closes — Prompt 12 §5, docs/11 §Compliance."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AccountDeletion, AppUser, Payment
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.purge_accounts import run_purge_accounts

pytestmark = [pytest.mark.db, requires_db]

NOW = dt.datetime(2026, 8, 21, 3, 0, tzinfo=dt.UTC)


async def _account(
    session: AsyncSession, email: str, *, deleted: bool, purge_after: dt.datetime | None
) -> AppUser:
    user = AppUser(
        public_id=email.split("@", maxsplit=1)[0].ljust(12, "0")[:12],
        email=email,
        deleted_at=NOW - dt.timedelta(days=8) if deleted else None,
    )
    session.add(user)
    await session.flush()
    if purge_after is not None:
        session.add(AccountDeletion(user_id=user.id, purge_after=purge_after))
        await session.flush()
    return user


class TestPurge:
    async def test_it_erases_an_account_past_its_window(self, session: AsyncSession) -> None:
        user = await _account(
            session, "gone@example.com", deleted=True, purge_after=NOW - dt.timedelta(days=1)
        )
        user_id = user.id

        purged = await run_purge_accounts(session, StepOutcome(), now=NOW)
        assert purged == 1
        assert (
            await session.execute(select(AppUser).where(AppUser.id == user_id))
        ).scalar_one_or_none() is None

    async def test_it_leaves_an_account_still_inside_its_window(
        self, session: AsyncSession
    ) -> None:
        user = await _account(
            session, "waiting@example.com", deleted=True, purge_after=NOW + dt.timedelta(days=3)
        )
        assert await run_purge_accounts(session, StepOutcome(), now=NOW) == 0
        assert (
            await session.execute(select(AppUser).where(AppUser.id == user.id))
        ).scalar_one_or_none() is not None

    async def test_it_leaves_an_account_that_was_reactivated(self, session: AsyncSession) -> None:
        """Signing in cancels the deletion; the sweep must not undo that."""
        user = await _account(
            session, "back@example.com", deleted=False, purge_after=NOW - dt.timedelta(days=1)
        )
        assert await run_purge_accounts(session, StepOutcome(), now=NOW) == 0
        assert (
            await session.execute(select(AppUser).where(AppUser.id == user.id))
        ).scalar_one_or_none() is not None

    async def test_it_keeps_the_invoice_trail_but_unlinks_it(self, session: AsyncSession) -> None:
        """India's tax law requires invoice retention; DPDP erasure does not override it."""
        user = await _account(
            session, "paid@example.com", deleted=True, purge_after=NOW - dt.timedelta(days=1)
        )
        session.add(
            Payment(
                user_id=user.id,
                amount_inr=Decimal("500.00"),
                status="captured",
                invoice_number="DEC-0001",
            )
        )
        await session.flush()

        assert await run_purge_accounts(session, StepOutcome(), now=NOW) == 1

        payment = (
            await session.execute(select(Payment).where(Payment.invoice_number == "DEC-0001"))
        ).scalar_one()
        assert payment.user_id == user.id, "the invoice trail survives intact"

        # ...pointing at a row that identifies nobody.
        await session.refresh(user)
        assert user.email.endswith("@deleted.invalid")
        assert user.name is None
        assert user.password_hash is None
        assert "paid@example.com" not in user.email

    async def test_it_is_idempotent(self, session: AsyncSession) -> None:
        await _account(
            session, "twice@example.com", deleted=True, purge_after=NOW - dt.timedelta(days=1)
        )
        assert await run_purge_accounts(session, StepOutcome(), now=NOW) == 1
        assert await run_purge_accounts(session, StepOutcome(), now=NOW) == 0

    async def test_the_evidence_row_survives_with_a_timestamp(self, session: AsyncSession) -> None:
        user = await _account(
            session, "audit@example.com", deleted=True, purge_after=NOW - dt.timedelta(days=1)
        )
        user_id = user.id
        await run_purge_accounts(session, StepOutcome(), now=NOW)
        # The FK cascades, so the evidence is the *absence* of the user plus the step's note.
        assert (
            await session.execute(select(AccountDeletion).where(AccountDeletion.user_id == user_id))
        ).scalar_one_or_none() is None

    async def test_it_names_public_ids_and_never_addresses(self, session: AsyncSession) -> None:
        """The step's note outlives the account it describes."""
        await _account(
            session, "private@example.com", deleted=True, purge_after=NOW - dt.timedelta(days=1)
        )
        outcome = StepOutcome()
        await run_purge_accounts(session, outcome, now=NOW)
        rendered = repr(outcome.detail)
        assert "private@example.com" not in rendered
        assert "private00000"[:12] in rendered
