"""Invoice numbers are gapless and unique under concurrent payment creation — Prompt 13.

    "Invoice numbers are gapless and unique under concurrent payment creation."

This is the one acceptance criterion that cannot be asserted through the shared-session HTTP
fixture: it is *about* two transactions racing, so each caller needs its own connection and its
own commit. The tests below therefore drive `decile_api.invoices` directly against the seeded
database, with real concurrent sessions.

What "gapless" means here, precisely
------------------------------------
Rule 46(b) of the CGST Rules wants a consecutive serial number. `allocate_invoice_number` takes it
with `UPDATE invoice_counter ... RETURNING`, which holds a row lock until the transaction ends, so
concurrent allocations serialise and the set of numbers issued is the contiguous range. A
transaction that allocates and then *rolls back* still leaves a hole — that is inherent, not a
shortcut, which is why allocation happens inside the same transaction that inserts the payment.
`test_a_rollback_leaves_the_counter_where_it_was` pins the one thing that can be guaranteed:
nothing is consumed by a transaction that never committed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Final

import billing_helpers as helpers
import pytest
from screener_helpers import requires_db
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from decile_api.invoices import (
    BillingPeriod,
    InvoiceRequest,
    allocate_invoice_number,
    invoice_series,
    issue_invoice,
    today_ist,
)
from decile_api.settings import Settings
from decile_core.models import AppUser, InvoiceCounter, Payment, Plan
from decile_providers.archive import LocalRawArchive

pytestmark = [pytest.mark.db, requires_db]

#: Enough callers to make the race real without making the suite slow.
CONCURRENCY: Final = 12

ISSUED_ON: Final = dt.date(2026, 8, 21)


@pytest.fixture
def settings(seeded_url: str, tmp_path: Path) -> Settings:
    return helpers.billing_settings(seeded_url, tmp_path / "invoices")


@pytest.fixture
def archive(tmp_path: Path) -> LocalRawArchive:
    return LocalRawArchive(tmp_path / "invoices")


async def _reset_counter(url: str) -> None:
    """Start every test from an empty series, so the assertions are about *this* test's numbers."""
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await session.execute(delete(Payment))
            await session.execute(delete(InvoiceCounter))
    finally:
        await engine.dispose()


@pytest.fixture
async def clean_counter(seeded_url: str) -> str:
    await _reset_counter(seeded_url)
    return seeded_url


async def _seed_user(url: str, email: str) -> int:
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            user = AppUser(
                public_id=email.split("@", maxsplit=1)[0][:12].ljust(12, "0"),
                email=email,
                name="Concurrency Tester",
            )
            session.add(user)
            await session.flush()
            return int(user.id)
    finally:
        await engine.dispose()


def _running(number: str) -> int:
    return int(number.rsplit("/", maxsplit=1)[1])


class TestAllocationUnderConcurrency:
    async def test_twelve_racing_allocations_are_unique_and_contiguous(
        self, clean_counter: str, settings: Settings
    ) -> None:
        """The acceptance criterion. Each caller has its own connection and its own commit."""
        engine = create_async_engine(clean_counter)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def allocate() -> str:
            async with maker() as session, session.begin():
                return await allocate_invoice_number(session, settings, on=ISSUED_ON)

        try:
            numbers = await asyncio.gather(*(allocate() for _ in range(CONCURRENCY)))
        finally:
            await engine.dispose()

        assert len(set(numbers)) == CONCURRENCY, f"duplicate invoice numbers: {numbers}"
        assert sorted(_running(number) for number in numbers) == list(range(1, CONCURRENCY + 1))

    async def test_the_series_is_the_financial_year(
        self, clean_counter: str, settings: Settings
    ) -> None:
        """Rule 46(b): the number is unique within a financial year."""
        engine = create_async_engine(clean_counter)
        try:
            async with async_sessionmaker(engine)() as session, session.begin():
                number = await allocate_invoice_number(session, settings, on=ISSUED_ON)
        finally:
            await engine.dispose()
        assert number == "DCL/2026-27/000001"
        assert invoice_series(ISSUED_ON) == "2026-27"

    async def test_two_financial_years_count_independently(
        self, clean_counter: str, settings: Settings
    ) -> None:
        engine = create_async_engine(clean_counter)
        try:
            async with async_sessionmaker(engine)() as session, session.begin():
                first = await allocate_invoice_number(session, settings, on=dt.date(2027, 3, 31))
                second = await allocate_invoice_number(session, settings, on=dt.date(2027, 4, 1))
        finally:
            await engine.dispose()
        assert first == "DCL/2026-27/000001"
        assert second == "DCL/2027-28/000001"

    async def test_a_rollback_leaves_the_counter_where_it_was(
        self, clean_counter: str, settings: Settings
    ) -> None:
        """Nothing is consumed by a transaction that never committed."""
        engine = create_async_engine(clean_counter)
        maker = async_sessionmaker(engine)
        try:
            async with maker() as session:
                await allocate_invoice_number(session, settings, on=ISSUED_ON)
                await session.rollback()
            async with maker() as session, session.begin():
                after = await allocate_invoice_number(session, settings, on=ISSUED_ON)
        finally:
            await engine.dispose()
        assert after == "DCL/2026-27/000001"

    async def test_the_counter_row_survives_a_concurrent_first_use(
        self, clean_counter: str, settings: Settings
    ) -> None:
        """Two first-ever payments of a financial year must not collide on the counter's own PK."""
        engine = create_async_engine(clean_counter)
        maker = async_sessionmaker(engine)

        async def allocate() -> str:
            async with maker() as session, session.begin():
                return await allocate_invoice_number(session, settings, on=dt.date(2029, 6, 1))

        try:
            numbers = await asyncio.gather(allocate(), allocate(), allocate())
            async with maker() as session:
                rows = (
                    (
                        await session.execute(
                            select(InvoiceCounter).where(InvoiceCounter.series == "2029-30")
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()
        assert len(set(numbers)) == 3
        assert len(rows) == 1


class TestIssuingWholeInvoicesConcurrently:
    """The same race, through the function a webhook actually calls."""

    async def test_twelve_concurrent_payments_produce_twelve_contiguous_invoices(
        self, clean_counter: str, settings: Settings, archive: LocalRawArchive
    ) -> None:
        user_id = await _seed_user(clean_counter, "concurrent@example.com")
        engine = create_async_engine(clean_counter)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def pay(index: int) -> str:
            async with maker() as session, session.begin():
                user = (
                    await session.execute(select(AppUser).where(AppUser.id == user_id))
                ).scalar_one()
                plan = (
                    await session.execute(select(Plan).where(Plan.code == "monthly"))
                ).scalar_one()
                payment = Payment(
                    user_id=user.id,
                    amount_inr=Decimal("500.00"),
                    status="captured",
                    razorpay_payment_id=f"pay_CONCURRENT{index:02d}",
                )
                session.add(payment)
                await session.flush()
                facts = await issue_invoice(
                    session,
                    settings,
                    InvoiceRequest(
                        payment=payment,
                        user=user,
                        plan=plan,
                        archive=archive,
                        period=BillingPeriod(),
                        on=ISSUED_ON,
                    ),
                )
                return facts.invoice_number

        try:
            numbers = await asyncio.gather(*(pay(index) for index in range(CONCURRENCY)))
            async with maker() as session:
                stored = [
                    row
                    for row in (
                        (
                            await session.execute(
                                select(Payment.invoice_number).where(
                                    Payment.invoice_number.is_not(None)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if row is not None
                ]
        finally:
            await engine.dispose()

        assert len(set(numbers)) == CONCURRENCY
        assert sorted(_running(number) for number in numbers) == list(range(1, CONCURRENCY + 1))
        assert sorted(stored) == sorted(numbers)

    async def test_the_database_refuses_a_duplicate_number(self, clean_counter: str) -> None:
        """docs/04 declares `payment.invoice_number` UNIQUE; the constraint is the last line."""
        engine = create_async_engine(clean_counter)
        user_id = await _seed_user(clean_counter, "duplicate@example.com")
        maker = async_sessionmaker(engine)
        try:
            async with maker() as session, session.begin():
                for suffix in ("a", "b"):
                    session.add(
                        Payment(
                            user_id=user_id,
                            amount_inr=Decimal("500.00"),
                            status="captured",
                            razorpay_payment_id=f"pay_DUP{suffix}",
                            invoice_number="DCL/2026-27/000001",
                        )
                    )
                with pytest.raises(IntegrityError, match=r"uq_payment_invoice_number"):
                    await session.flush()
        finally:
            await engine.dispose()

    async def test_every_pdf_was_actually_written(
        self, clean_counter: str, settings: Settings, archive: LocalRawArchive, tmp_path: Path
    ) -> None:
        """An invoice number whose document does not exist would be worse than no number."""
        user_id = await _seed_user(clean_counter, "written@example.com")
        engine = create_async_engine(clean_counter)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session, session.begin():
                user = (
                    await session.execute(select(AppUser).where(AppUser.id == user_id))
                ).scalar_one()
                plan = (
                    await session.execute(select(Plan).where(Plan.code == "monthly"))
                ).scalar_one()
                payment = Payment(
                    user_id=user.id,
                    amount_inr=Decimal("500.00"),
                    status="captured",
                    razorpay_payment_id="pay_WRITTEN001",
                )
                session.add(payment)
                await session.flush()
                await issue_invoice(
                    session,
                    settings,
                    InvoiceRequest(
                        payment=payment, user=user, plan=plan, archive=archive, on=ISSUED_ON
                    ),
                )
                key = payment.invoice_pdf_key
        finally:
            await engine.dispose()

        assert key is not None
        assert archive.exists(key)
        assert archive.get(key).startswith(b"%PDF-")
        assert (tmp_path / "invoices" / key).is_file()


class TestTheIssueDate:
    def test_it_is_the_indian_calendar_date(self) -> None:
        """An invoice raised at 03:00 IST on 1 April belongs to the new financial year; UTC would
        put it in the old one."""
        just_after_midnight_ist = dt.datetime(2027, 3, 31, 20, 0, tzinfo=dt.UTC)
        assert today_ist(just_after_midnight_ist) == dt.date(2027, 4, 1)
        assert invoice_series(today_ist(just_after_midnight_ist)) == "2027-28"
