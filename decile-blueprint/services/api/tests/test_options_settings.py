"""OP2: the options settings boundary (M4.1) — ceilings, NIFTY only, the flags' defaults.

``06`` OP2 AC asserted here: ``OptionsSettings`` validates ``op_*_config`` against the six
ceilings of ``docs/options/02`` with an audit row per change; a refused patch writes nothing;
**adding BANKNIFTY to any config is a 422**; the nine flags and six ceilings are system-only,
mirrored in the API and the worker, shipped false in both ``.env.example`` files, and the stale
``BASKFY_CONDOR_*`` block is gone (OP0.7). No auto-execute name exists for any options sleeve.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.options_settings import (
    SYSTEM_OWNED_FIELDS,
    OptionsBookPatch,
    OptionsSleevePatch,
    apply_book_patch,
    apply_sleeve_patch,
    audit_trail,
    ceilings_from_settings,
    check_book,
    check_sleeve,
    read_sleeve,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.settings import Settings
from baskfy_core.models import AppUser
from baskfy_core.options.config import (
    DEFAULT_CEILINGS,
    CalendarConfig,
    OptionsCeilings,
    OptionsConfig,
    SleeveGroup,
)
from baskfy_core.options.gating import CEILING_ENV, EXECUTION_FLAG_ENV, OPERATIONAL_FLAG_ENV
from baskfy_worker.seeds.options_event_days import seed_options
from baskfy_worker.settings import WorkerSettings

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[1]
REPO_ROOT: Final = API_DIR.parents[1]
MONOREPO_ROOT: Final = REPO_ROOT.parent
ENV_EXAMPLES: Final = (MONOREPO_ROOT / ".env.example", REPO_ROOT / ".env.example")

CEILINGS: Final = OptionsCeilings()
NOW: Final = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)


def _shipped(model: type[Settings] | type[WorkerSettings], name: str) -> object:
    return model.model_fields[name].default


# --- the ceilings, pure ----------------------------------------------------------------------


class TestTheCeilings:
    def test_the_api_defaults_are_the_documents_and_the_cores(self) -> None:
        assert ceilings_from_settings(Settings.model_construct()) == DEFAULT_CEILINGS

    @pytest.mark.parametrize(
        ("field", "value", "env"),
        [
            ("risk_per_trade_pct", Decimal("1.01"), "BASKFY_OPTIONS_RISK_PCT_MAX"),
            ("max_lots", 11, "BASKFY_OPTIONS_MAX_LOTS_MAX"),
            ("hard_exit_time", dt.time(15, 1), "BASKFY_OPTIONS_HARD_EXIT_LATEST"),
        ],
    )
    def test_a_sleeve_value_above_its_ceiling_is_a_422_naming_the_env_var(
        self, field: str, value: object, env: str
    ) -> None:
        with pytest.raises(Problem) as caught:
            check_sleeve({field: value}, CEILINGS, current=None)
        assert caught.value.status == 422
        assert caught.value.type is ProblemType.SETTING_ABOVE_CEILING
        assert caught.value.extra["env_var"] == env

    @pytest.mark.parametrize(
        ("field", "value", "env"),
        [
            ("daily_loss_limit_inr", Decimal("30000.01"), "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX"),
            ("monthly_pause_inr", Decimal("75001"), "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX"),
        ],
    )
    def test_a_book_limit_above_its_ceiling_is_a_422(
        self, field: str, value: Decimal, env: str
    ) -> None:
        with pytest.raises(Problem) as caught:
            check_book({field: value}, CEILINGS)
        assert caught.value.extra["env_var"] == env

    def test_values_at_the_ceiling_are_allowed(self) -> None:
        check_sleeve(
            {"risk_per_trade_pct": Decimal("1.0"), "max_lots": 10, "hard_exit_time": dt.time(15)},
            CEILINGS,
            current=None,
        )
        check_book({"daily_loss_limit_inr": Decimal("30000")}, CEILINGS)

    def test_the_derived_rupee_risk_is_bounded_on_the_row_as_it_would_stand(self) -> None:
        """₹30 lakh at 1 % is ₹30,000 > ₹25,000: either field alone can push it over."""
        with pytest.raises(Problem) as caught:
            check_sleeve(
                {"sleeve_capital_inr": Decimal("3000000"), "risk_per_trade_pct": Decimal("1.0")},
                CEILINGS,
                current=None,
            )
        assert caught.value.extra["env_var"] == "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX"
        check_sleeve(
            {"sleeve_capital_inr": Decimal("2500000"), "risk_per_trade_pct": Decimal("1.0")},
            CEILINGS,
            current=None,
        )


# --- NIFTY only ------------------------------------------------------------------------------


class TestNiftyOnly:
    @pytest.mark.parametrize("underlying", ["BANKNIFTY", "banknifty", "FINNIFTY", "SENSEX"])
    def test_banknifty_on_the_book_config_is_a_422(self, underlying: str) -> None:
        with pytest.raises(Problem) as caught:
            OptionsBookPatch.model_validate({"underlying": underlying})
        assert caught.value.status == 422
        assert caught.value.type is ProblemType.UNDERLYING_NOT_ALLOWED
        assert caught.value.extra["allowed"] == ["NIFTY"]

    def test_banknifty_on_a_sleeve_config_is_a_422_too(self) -> None:
        with pytest.raises(Problem) as caught:
            OptionsSleevePatch.model_validate({"underlying": "BANKNIFTY", "max_lots": 1})
        assert caught.value.status == 422

    def test_banknifty_in_the_pure_config_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            OptionsConfig(calendar=CalendarConfig(underlying="BANKNIFTY"))

    def test_nifty_is_accepted_on_the_book(self) -> None:
        assert OptionsBookPatch.model_validate({"underlying": "nifty"}).changes() == {
            "underlying": "NIFTY"
        }


class TestTheSystemOwnedFields:
    @pytest.mark.parametrize("field", SYSTEM_OWNED_FIELDS)
    def test_a_patch_cannot_lift_a_pause(self, field: str) -> None:
        for model in (OptionsBookPatch, OptionsSleevePatch):
            with pytest.raises(ValidationError):
                model.model_validate({field: None})

    @pytest.mark.parametrize("ceiling", sorted(CEILING_ENV))
    def test_no_ceiling_is_a_patch_field(self, ceiling: str) -> None:
        for model in (OptionsBookPatch, OptionsSleevePatch):
            assert ceiling not in model.model_fields


# --- the flags: system-only, mirrored, shipped false ------------------------------------------


_API_FLAGS: Final = (
    "options_o1m_execution_enabled",
    "options_o1w_execution_enabled",
    "options_o2_execution_enabled",
    "options_o3_execution_enabled",
    "options_monitor_enabled",
    "options_collect_enabled",
    "options_scan_enabled",
)
_CEILING_FIELDS: Final = tuple(f"options_{name}" for name in CEILING_ENV)


class TestTheFlags:
    @pytest.mark.parametrize("name", _API_FLAGS)
    def test_every_flag_defaults_false_in_the_api_and_the_worker(self, name: str) -> None:
        assert _shipped(Settings, name) is False
        assert _shipped(WorkerSettings, name) is False

    @pytest.mark.parametrize("name", _CEILING_FIELDS)
    def test_the_api_and_worker_ceilings_agree(self, name: str) -> None:
        assert _shipped(Settings, name) == _shipped(WorkerSettings, name)

    def test_no_auto_execute_setting_exists_for_options(self) -> None:
        for model in (Settings, WorkerSettings):
            assert not [n for n in model.model_fields if n.startswith("options_") and "auto" in n]

    @pytest.mark.parametrize("path", ENV_EXAMPLES, ids=lambda p: p.parent.name)
    def test_the_env_examples_ship_every_flag_false_and_every_ceiling(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for name in (*EXECUTION_FLAG_ENV.values(), *OPERATIONAL_FLAG_ENV):
            assert re.search(rf"^{name}=false\s", text, re.MULTILINE), f"{name} not false"
        for name in CEILING_ENV.values():
            assert re.search(rf"^{name}=\S+", text, re.MULTILINE), f"{name} missing"
        assert not re.search(r"^BASKFY_OPTIONS_\w*AUTO\w*=", text, re.MULTILINE)
        assert not re.search(r"^BASKFY_CONDOR_", text, re.MULTILINE), "OP0.7: stale block"


# --- the database half: refused writes nothing, accepted writes are audited ------------------


def _database_url() -> str | None:
    return os.environ.get(ENV_VAR)


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set")
)


@pytest.fixture(scope="module")
def op_url() -> str:
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return url


@asynccontextmanager
async def _seeded(url: str, tag: str) -> AsyncIterator[tuple[AsyncSession, int]]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        user = AppUser(public_id=f"op2s-{tag}-{uuid.uuid4().hex[:8]}", email=f"{tag}@x.test")
        session.add(user)
        await session.flush()
        await seed_options(session, int(user.id))
        yield session, int(user.id)
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


@requires_db
class TestTheWrites:
    async def test_a_patch_crossing_a_ceiling_changes_nothing(self, op_url: str) -> None:
        async with _seeded(op_url, "atomic") as (session, user_id):
            with pytest.raises(Problem):
                await apply_sleeve_patch(
                    session,
                    user_id=user_id,
                    sleeve=SleeveGroup.O2,
                    patch=OptionsSleevePatch.model_validate(
                        {"max_lots": 1, "hard_exit_time": "15:30"}
                    ),
                    ceilings=CEILINGS,
                    changed_by="test",
                    now=NOW,
                )
            session.expire_all()
            row = await read_sleeve(session, user_id, SleeveGroup.O2)
            assert row.max_lots == 2
            assert await audit_trail(session, user_id=user_id) == []

    async def test_an_accepted_patch_is_audited_per_field(self, op_url: str) -> None:
        async with _seeded(op_url, "audit") as (session, user_id):
            await apply_sleeve_patch(
                session,
                user_id=user_id,
                sleeve=SleeveGroup.O1M,
                patch=OptionsSleevePatch.model_validate({"max_lots": 2, "paper_enabled": True}),
                ceilings=CEILINGS,
                changed_by="test",
                now=NOW,
            )
            trail = await audit_trail(session, user_id=user_id)
            # paper_enabled was already true: only the field that moved is audited.
            assert [(a.scope, a.key, a.old_value, a.new_value) for a in trail] == [
                ("O1M", "max_lots", "3", "2")
            ]

    async def test_banknifty_never_reaches_the_book_row(self, op_url: str) -> None:
        async with _seeded(op_url, "bn") as (session, user_id):
            with pytest.raises(Problem) as caught:
                await apply_book_patch(
                    session,
                    user_id=user_id,
                    patch=OptionsBookPatch.model_validate({"underlying": "BANKNIFTY"}),
                    ceilings=CEILINGS,
                    changed_by="test",
                    now=NOW,
                )
            assert caught.value.status == 422
            underlying = (
                await session.execute(
                    sa.text("SELECT underlying FROM op_book_config WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
            assert underlying == "NIFTY"
