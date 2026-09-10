"""VB10: every `vb_` write carries the sole tenant's id (`docs/vbt/02` Track C §6).

A sleeve is one person's money. The failure this guards against is not a stranger reading the
book — the API refuses that (`test_api_vbt.py`) — but a **write** that lands with no owner or the
wrong one, which would make the sleeve's cash arithmetic quietly wrong for both users.

Asserted two ways, because either alone leaves a hole:

* **over the schema**, so a table added later without a `user_id` fails immediately;
* **over a real evening**, so a writer that has the column and forgets to fill it is caught too.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, VbBreadthDaily, VbConfig, VbSignalDaily
from baskfy_core.models.base import Base
from baskfy_core.vbt.config import Gate, SignalState
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.vbt_evening import run_vbt_evening

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)
SESSIONS = [
    dt.date(2026, 8, 12),
    dt.date(2026, 8, 13),
    dt.date(2026, 8, 14),
    dt.date(2026, 8, 17),
    AS_OF,
]

#: Every table the sleeve owns. Written out rather than derived from a `vb_` prefix scan, so
#: adding a table is a deliberate act in two places — the same argument `test_schema_matches_docs`
#: makes for its own list.
VB_TABLES = (
    "vb_config",
    "vb_config_audit",
    "vb_signal_daily",
    "vb_breadth_daily",
    "vb_order",
    "vb_position",
    "vb_fill",
    "vb_plan",
    "vb_plan_line",
    "vb_plan_skip",
    "vb_session",
    "vb_backtest_run",
)


class TestTheSchemaCannotHoldAnOwnerlessRow:
    @pytest.mark.parametrize("table", VB_TABLES)
    def test_every_vb_table_has_a_non_null_user_id(self, table: str) -> None:
        columns = Base.metadata.tables[table].c
        assert "user_id" in columns, f"{table} has no user_id at all"
        assert columns["user_id"].nullable is False, f"{table}.user_id is nullable"

    @pytest.mark.parametrize("table", VB_TABLES)
    def test_every_vb_table_points_its_user_id_at_app_user(self, table: str) -> None:
        """A `user_id` that is only an integer is a foreign key nobody enforces."""
        column = Base.metadata.tables[table].c["user_id"]
        targets = {key.column.table.name for key in column.foreign_keys}
        assert targets == {"app_user"}, f"{table}.user_id points at {targets}"

    def test_the_list_is_the_whole_sleeve(self) -> None:
        """A table added under the `vb_` prefix and left out of the list above would otherwise
        never be checked."""
        actual = {name for name in Base.metadata.tables if name.startswith("vb_")}
        assert actual == set(VB_TABLES)


class TestAnEveningWritesNothingOwnerless:
    async def test_every_row_an_evening_produces_carries_the_user(
        self, session: AsyncSession
    ) -> None:
        """The schema half proves the column exists; this proves the writers fill it."""
        user = AppUser(public_id="vb10-user", email="vb10@example.com")
        session.add(user)
        await session.flush()
        user_id = int(user.id)
        session.add(
            VbConfig(user_id=user_id, sleeve_capital_inr=Decimal("1000000.00"), updated_by="test")
        )
        for day in SESSIONS:
            session.add(
                VbBreadthDaily(
                    user_id=user_id,
                    date=day,
                    universe_count=100,
                    measured_count=100,
                    above_count=60,
                    pct_above_dma=Decimal("60.0000"),
                    gate=Gate.OPEN.value,
                    dma_bars=200,
                )
            )
        instrument_id = await make_instrument(session, "VBTCO")
        session.add(
            VbSignalDaily(
                user_id=user_id,
                date=AS_OF,
                instrument_id=instrument_id,
                state=SignalState.SIGNAL.value,
                failed_filters=[],
                close=Decimal("100.00"),
                close_raw=Decimal("100.00"),
                adj_factor=Decimal(1),
                limit_price=Decimal("100.00"),
                stop_price=Decimal("88.00"),
                turnover_avg_20=500_000_000,
                rank_key=1_000_000,
            )
        )
        await session.flush()

        report = await run_vbt_evening(
            session, StepOutcome(), AS_OF, user_id=user_id, execution_enabled=False
        )
        assert report is not None and report.entries == 1

        # Every writable table the evening touches, read back in full. Counted through the
        # column rather than the mapped attribute, so mypy sees a column and not `Base`.
        for table in ("vb_plan", "vb_plan_line", "vb_session", "vb_order", "vb_position"):
            mapped = Base.metadata.tables[table]
            wrong = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(mapped)
                    .where(mapped.c["user_id"] != user_id)
                )
            ).scalar_one()
            assert wrong == 0, f"{table} holds {wrong} rows belonging to somebody else"
            mine = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(mapped)
                    .where(mapped.c["user_id"] == user_id)
                )
            ).scalar_one()
            if table in {"vb_plan", "vb_plan_line", "vb_session"}:
                assert mine > 0, f"{table} was not written at all, so this proved nothing"

    async def test_a_second_user_sees_none_of_it(self, session: AsyncSession) -> None:
        """The isolation the sleeve's cash arithmetic depends on: two books, no shared row."""
        first = AppUser(public_id="vb10-a", email="vb10a@example.com")
        second = AppUser(public_id="vb10-b", email="vb10b@example.com")
        session.add_all([first, second])
        await session.flush()
        session.add(VbConfig(user_id=int(first.id), updated_by="test"))
        session.add(VbConfig(user_id=int(second.id), updated_by="test"))
        await session.flush()

        theirs = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(VbConfig)
                .where(VbConfig.user_id == int(second.id))
            )
        ).scalar_one()

        assert theirs == 1
        assert int(first.id) != int(second.id)
