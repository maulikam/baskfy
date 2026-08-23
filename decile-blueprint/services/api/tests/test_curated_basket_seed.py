"""SC1's sole-operator settings and curated-product reference seed contract."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from baskfy_api.seed import (
    CURATED_MANAGER_ROWS,
    _sole_user_email,
    seed_curated_product_reference,
)
from baskfy_api.settings import Settings, get_settings
from baskfy_core.models import (
    ApiKey,
    AppUser,
    AuthToken,
    AuthVerificationToken,
    CuratedManager,
    Payment,
    RefreshToken,
    Subscription,
)

ENV_VAR = "BASKFY_TEST_DATABASE_URL"
API_DIR = Path(__file__).resolve().parents[1]


def test_sole_user_setting_has_safe_defaults_and_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BASKFY_SOLE_USER_ID", raising=False)
    monkeypatch.delenv("BASKFY_SOLE_BROKER_ACCOUNT_ID", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.sole_user_id == "baskfy-sole-user"
    assert defaults.sole_broker_account_id == "operator-kite"

    monkeypatch.setenv("BASKFY_SOLE_USER_ID", "maulik-private-operator")
    monkeypatch.setenv("BASKFY_SOLE_BROKER_ACCOUNT_ID", "maulik-kite-primary")
    overridden = Settings(_env_file=None)
    assert overridden.sole_user_id == "maulik-private-operator"
    assert overridden.sole_broker_account_id == "maulik-kite-primary"

    stripped = Settings(
        _env_file=None,
        sole_user_id="  maulik-private-operator  ",
        sole_broker_account_id="  maulik-kite-primary  ",
    )
    assert stripped.sole_user_id == "maulik-private-operator"
    assert stripped.sole_broker_account_id == "maulik-kite-primary"
    with pytest.raises(ValueError, match="operator identifiers must not be blank"):
        Settings(_env_file=None, sole_user_id="   ")


def test_sole_user_setting_derives_a_stable_non_routable_unique_email() -> None:
    first = _sole_user_email("baskfy-sole-user")
    assert first == _sole_user_email("baskfy-sole-user")
    assert first != _sole_user_email("another-sole-user")
    assert first.endswith("@users.invalid")
    assert "baskfy-sole-user" not in first
    local_part, _, domain = first.partition("@")
    assert domain == "users.invalid"
    assert len(local_part.encode("ascii")) == 53
    assert len(local_part.encode("ascii")) <= 64
    assert len(first.encode("ascii")) <= 254


@pytest.fixture
def migrated(clean_database: object) -> None:
    """Apply the full schema to this leaf's disposable database."""
    del clean_database
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": os.environ[ENV_VAR],
            "DRY_RUN": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )


db_test = pytest.mark.skipif(
    os.environ.get(ENV_VAR) is None,
    reason=f"{ENV_VAR} is not set; run the disposable PostgreSQL database",
)


@pytest.mark.db
@db_test
@pytest.mark.asyncio
async def test_curated_product_seed_is_idempotent_and_preserves_operator_data(
    engine: AsyncEngine, migrated: None
) -> None:
    del migrated
    sole_user_id = get_settings().sole_user_id
    async with async_sessionmaker(engine)() as session, session.begin():
        first_counts = await seed_curated_product_reference(session)
    assert first_counts == {"app_user": 1, "cb_manager": 2}

    async with async_sessionmaker(engine)() as session:
        first_user = (
            await session.execute(select(AppUser).where(AppUser.public_id == sole_user_id))
        ).scalar_one()
        first_managers = (
            (await session.execute(select(CuratedManager).order_by(CuratedManager.slug)))
            .scalars()
            .all()
        )
        user_snapshot = (
            first_user.id,
            first_user.public_id,
            first_user.email,
            first_user.password_hash,
            first_user.name,
            first_user.email_verified_at,
            first_user.created_at,
            first_user.deleted_at,
            first_user.is_staff,
        )
        manager_snapshot = [
            (
                row.id,
                row.slug,
                row.name,
                row.kind,
                row.sebi_reg_no,
                row.bio,
                tuple(row.strategies),
                row.disclosures_md,
                row.created_at,
            )
            for row in first_managers
        ]

    async with async_sessionmaker(engine)() as session, session.begin():
        second_counts = await seed_curated_product_reference(session)
    assert second_counts == first_counts

    async with async_sessionmaker(engine)() as session:
        second_user = (
            await session.execute(select(AppUser).where(AppUser.public_id == sole_user_id))
        ).scalar_one()
        second_managers = (
            (await session.execute(select(CuratedManager).order_by(CuratedManager.slug)))
            .scalars()
            .all()
        )
        assert (
            second_user.id,
            second_user.public_id,
            second_user.email,
            second_user.password_hash,
            second_user.name,
            second_user.email_verified_at,
            second_user.created_at,
            second_user.deleted_at,
            second_user.is_staff,
        ) == user_snapshot
        assert [
            (
                row.id,
                row.slug,
                row.name,
                row.kind,
                row.sebi_reg_no,
                row.bio,
                tuple(row.strategies),
                row.disclosures_md,
                row.created_at,
            )
            for row in second_managers
        ] == manager_snapshot

    async with async_sessionmaker(engine)() as session, session.begin():
        operator = (
            await session.execute(select(AppUser).where(AppUser.public_id == sole_user_id))
        ).scalar_one()
        operator.email = "operator-maintained@example.invalid"
        operator.name = "Operator-maintained name"
        operator.password_hash = "operator-maintained-credential"
        stale_manager = (
            await session.execute(
                select(CuratedManager).where(CuratedManager.slug == "baskfy-engine")
            )
        ).scalar_one()
        stale_manager.name = "Stale manager copy"
        stale_manager.kind = "HUMAN"
        stale_manager.sebi_reg_no = "UNVERIFIED-CLAIM"
        stale_manager.bio = "Stale manager bio"
        stale_manager.strategies = ["stale"]
        stale_manager.disclosures_md = "Stale disclosure"

    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_curated_product_reference(session)

    expected_by_slug = {str(row["slug"]): row for row in CURATED_MANAGER_ROWS}
    async with async_sessionmaker(engine)() as session:
        operator = (
            await session.execute(select(AppUser).where(AppUser.public_id == sole_user_id))
        ).scalar_one()
        assert (
            operator.email,
            operator.name,
            operator.password_hash,
        ) == (
            "operator-maintained@example.invalid",
            "Operator-maintained name",
            "operator-maintained-credential",
        )
        managers = (await session.execute(select(CuratedManager))).scalars().all()
        assert len(managers) == 2
        for manager in managers:
            expected = expected_by_slug[manager.slug]
            assert manager.name == expected["name"]
            assert manager.kind == expected["kind"]
            assert manager.sebi_reg_no == expected["sebi_reg_no"]
            assert manager.bio == expected["bio"]
            assert manager.strategies == expected["strategies"]
            assert manager.disclosures_md == expected["disclosures_md"]


@pytest.mark.db
@db_test
@pytest.mark.asyncio
async def test_managers_are_exactly_the_documented_first_party_profiles(
    engine: AsyncEngine, migrated: None
) -> None:
    del migrated
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_curated_product_reference(session)

    async with async_sessionmaker(engine)() as session:
        managers = (
            (await session.execute(select(CuratedManager).order_by(CuratedManager.slug)))
            .scalars()
            .all()
        )

    assert [(row.slug, row.kind) for row in managers] == [
        ("baskfy-engine", "ENGINE"),
        ("maulik", "HUMAN"),
    ]
    for manager in managers:
        assert manager.sebi_reg_no is None
        assert manager.bio
        assert manager.strategies
        assert manager.disclosures_md
        assert "SEBI-registered" not in manager.bio
        assert "SEBI registered" not in manager.bio


@pytest.mark.db
@db_test
@pytest.mark.asyncio
async def test_seed_creates_no_credentials_tokens_subscriptions_or_payments(
    engine: AsyncEngine, migrated: None
) -> None:
    del migrated
    sole_user_id = get_settings().sole_user_id
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_curated_product_reference(session)

    async with async_sessionmaker(engine)() as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.public_id == sole_user_id))
        ).scalar_one()
        assert user.email == _sole_user_email(sole_user_id)
        assert user.password_hash is None
        assert user.email_verified_at is None
        counts = []
        for model in (
            AuthVerificationToken,
            AuthToken,
            RefreshToken,
            ApiKey,
            Subscription,
            Payment,
        ):
            counts.append(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
    assert counts == [0, 0, 0, 0, 0, 0]
