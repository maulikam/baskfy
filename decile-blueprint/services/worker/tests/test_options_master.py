"""OP2: the nightly NFO master, the calendar reading only it, the worker's gates, partitions.

``06`` OP2 AC asserted here:

* **the calendar's next monthly equals the master's**, and a fixture whose last Tuesday moved to
  the Monday (a holiday) **moves it** — a weekday rule would not (``04`` §1.1);
* the nightly task persists NIFTY CE/PE rows **never deleting**, rebuilds ``op_expiry``, and
  **alerts on a lot-size change or a moved expiry**; re-running a night changes nothing;
* ``options_gates()`` in the worker is ``PAPER`` for every combination but all-four-true, per
  sleeve (the 16-row table, read through the worker's own settings and the desk's parse).

Database tests run in rolled-back transactions against ``BASKFY_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import OpContract, OpExpiry
from baskfy_core.options.calendar import expiries, monthly_expiry
from baskfy_core.options.config import Mode, Sleeve, SleeveGroup, group_of
from baskfy_providers.kite import _to_option_contract
from baskfy_providers.records import OptionContractRecord
from baskfy_worker.alerts import AlertName
from baskfy_worker.options import options_ceilings, options_gates
from baskfy_worker.options.master import (
    EmptyMaster,
    load_contracts,
    master_alert,
    refresh_master,
)
from baskfy_worker.options.partitions import ensure_chain_partition, month_bounds, partition_name
from baskfy_worker.settings import WorkerSettings

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
API_DIR: Final = REPO_ROOT / "services" / "api"

#: A literal lot size is allowed in a fixture and nowhere else (``04`` §1.4).
LOT: Final = 65
#: A token base far from any real Kite token, so fixture rows never collide with a real master.
BASE: Final = 9_000_000_000

OCT_TUESDAYS: Final = (dt.date(2026, 10, 6), dt.date(2026, 10, 13), dt.date(2026, 10, 20))
OCT_MONTHLY: Final = dt.date(2026, 10, 27)
OCT_MONTHLY_SHIFTED: Final = dt.date(2026, 10, 26)  # a Monday: the Tuesday is a holiday
NOV_MONTHLY: Final = dt.date(2026, 11, 24)


def _records(
    expiry_dates: tuple[dt.date, ...], *, lot: int = LOT, offset: int = 0
) -> list[OptionContractRecord]:
    out: list[OptionContractRecord] = []
    for i, expiry in enumerate(expiry_dates):
        for j, strike in enumerate((24900, 25000, 25100)):
            for k, kind in enumerate(("CE", "PE")):
                token = BASE + offset + i * 100 + j * 10 + k
                out.append(
                    OptionContractRecord(
                        instrument_token=token,
                        tradingsymbol=f"FIXTURE{token}{kind}",
                        underlying="NIFTY",
                        expiry=expiry,
                        strike=Decimal(strike),
                        option_type="CE" if kind == "CE" else "PE",
                        lot_size=lot,
                        tick_size=Decimal("0.05"),
                    )
                )
    return out


NORMAL: Final = (*OCT_TUESDAYS, OCT_MONTHLY, NOV_MONTHLY)
SHIFTED: Final = (*OCT_TUESDAYS, OCT_MONTHLY_SHIFTED, NOV_MONTHLY)


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
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        # Fixture tokens only: a real master on this database must not bleed into the calendar.
        await session.execute(sa.delete(OpContract).where(OpContract.underlying == "NIFTY"))
        await session.execute(sa.delete(OpExpiry).where(OpExpiry.underlying == "NIFTY"))
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


# --- the provider's mapping -----------------------------------------------------------------


def test_the_dump_row_is_read_from_its_columns_never_from_the_symbol() -> None:
    row: dict[str, object] = {
        "instrument_token": 12345,
        "tradingsymbol": "NIFTY26OCT25000CE",
        "name": "NIFTY",
        "expiry": dt.date(2026, 10, 27),
        "strike": 25000.0,
        "instrument_type": "CE",
        "lot_size": 65,
        "tick_size": 0.05,
        "segment": "NFO-OPT",
        "exchange": "NFO",
    }
    record = _to_option_contract(row, "NIFTY")
    assert record is not None
    assert (record.expiry, record.strike, record.option_type, record.lot_size) == (
        dt.date(2026, 10, 27),
        Decimal("25000.0"),
        "CE",
        65,
    )
    assert record.tick_size == Decimal("0.05")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"name": "BANKNIFTY"}, "another underlying"),
        ({"instrument_type": "FUT"}, "a future"),
        ({"lot_size": 0}, "no lot size — never guessed"),
        ({"tick_size": None}, "no tick size"),
        ({"expiry": ""}, "no expiry"),
    ],
)
def test_rows_the_calendar_cannot_trust_are_skipped(change: dict[str, object], why: str) -> None:
    row: dict[str, object] = {
        "instrument_token": 1,
        "tradingsymbol": "X",
        "name": "NIFTY",
        "expiry": dt.date(2026, 10, 27),
        "strike": 25000,
        "instrument_type": "PE",
        "lot_size": 65,
        "tick_size": 0.05,
    }
    row.update(change)
    assert _to_option_contract(row, "NIFTY") is None, why


# --- the master and the calendar on a real database -----------------------------------------


@requires_db
class TestTheMaster:
    async def test_the_calendar_monthly_is_the_masters_and_a_holiday_shift_moves_it(
        self, op_url: str
    ) -> None:
        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(NORMAL), as_of=dt.date(2026, 9, 30))
            rows = await load_contracts(session)
            assert monthly_expiry(rows, 2026, 10) == OCT_MONTHLY
            kinds = {
                r.expiry_date: r.kind
                for r in (await session.execute(sa.select(OpExpiry))).scalars()
            }
            assert kinds[OCT_MONTHLY] == "MONTHLY"
            assert all(kinds[d] == "WEEKLY" for d in OCT_TUESDAYS)

        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(SHIFTED), as_of=dt.date(2026, 9, 30))
            rows = await load_contracts(session)
            # A weekday rule ("last Tuesday") says 27 Oct; the master says 26 Oct.
            assert monthly_expiry(rows, 2026, 10) == OCT_MONTHLY_SHIFTED
            assert OCT_MONTHLY not in expiries(rows)

    async def test_rerunning_a_night_changes_nothing_and_alerts_nothing(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            night = dt.date(2026, 9, 30)
            first = await refresh_master(session, _records(NORMAL), as_of=night)
            assert first.contracts_new == len(_records(NORMAL))
            snapshot = (
                await session.execute(
                    sa.text(
                        "SELECT count(*), max(last_seen) FROM op_contract WHERE underlying='NIFTY'"
                    )
                )
            ).one()
            second = await refresh_master(session, _records(NORMAL), as_of=night)
            assert second.contracts_new == 0 and second.changes == []
            assert master_alert(second) is None
            again = (
                await session.execute(
                    sa.text(
                        "SELECT count(*), max(last_seen) FROM op_contract WHERE underlying='NIFTY'"
                    )
                )
            ).one()
            assert tuple(again) == tuple(snapshot)

    async def test_a_moved_expiry_is_withdrawn_alerted_and_never_deleted(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(NORMAL), as_of=dt.date(2026, 9, 29))
            report = await refresh_master(
                session, _records(SHIFTED, offset=50), as_of=dt.date(2026, 9, 30)
            )
            withdrawn = [c for c in report.changes if c.what == "WITHDRAWN"]
            assert [c.expiry for c in withdrawn] == [OCT_MONTHLY]
            alert = master_alert(report)
            assert alert is not None and alert.name is AlertName.OPTIONS_MASTER_CHANGED
            # Never deleted: the old contracts are still resolvable, marked expired.
            old = (
                (
                    await session.execute(
                        sa.select(OpContract).where(OpContract.expiry == OCT_MONTHLY)
                    )
                )
                .scalars()
                .all()
            )
            assert old and all(c.expired for c in old)
            row = await session.get(OpExpiry, ("NIFTY", OCT_MONTHLY))
            assert row is not None and row.detail.get("withdrawn_on") == "2026-09-30"
            # A second run of the same night does not alert again.
            again = await refresh_master(
                session, _records(SHIFTED, offset=50), as_of=dt.date(2026, 9, 30)
            )
            assert master_alert(again) is None

    async def test_a_lot_size_change_is_alerted_and_applied(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(NORMAL), as_of=dt.date(2026, 9, 29))
            report = await refresh_master(
                session, _records(NORMAL, lot=75), as_of=dt.date(2026, 9, 30)
            )
            lot_changes = {c.expiry for c in report.changes if c.what == "LOT_SIZE"}
            assert lot_changes == set(NORMAL)
            assert master_alert(report) is not None
            row = await session.get(OpExpiry, ("NIFTY", NOV_MONTHLY))
            assert row is not None and row.lot_size == 75
            contract = await session.get(OpContract, BASE)
            assert contract is not None and contract.lot_size == 75

    async def test_a_contract_that_expired_stays_and_a_past_expiry_is_not_withdrawn(
        self, op_url: str
    ) -> None:
        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(NORMAL), as_of=dt.date(2026, 10, 5))
            later = tuple(d for d in NORMAL if d > dt.date(2026, 10, 6))
            report = await refresh_master(
                session, _records(later, offset=1), as_of=dt.date(2026, 10, 7)
            )
            assert [c for c in report.changes if c.what == "WITHDRAWN"] == []
            gone = await session.get(OpContract, BASE)  # the 6 Oct contract
            assert gone is not None and gone.expired is True

    async def test_an_empty_dump_is_refused_and_writes_nothing(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            await refresh_master(session, _records(NORMAL), as_of=dt.date(2026, 9, 29))
            with pytest.raises(EmptyMaster):
                await refresh_master(session, [], as_of=dt.date(2026, 9, 30))
            live = (
                await session.execute(sa.text("SELECT count(*) FROM op_contract WHERE NOT expired"))
            ).scalar_one()
            assert live == len(_records(NORMAL))

    async def test_a_partition_is_ensured_idempotently(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            day = dt.date(2028, 2, 14)
            assert await ensure_chain_partition(session, day) == "op_chain_snapshot_202802"
            assert await ensure_chain_partition(session, day) == "op_chain_snapshot_202802"
            await session.execute(
                sa.text(
                    "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, "
                    "option_type) VALUES ('2028-02-14 10:00+05:30', 7, '2028-02-15', 25000, 'PE')"
                )
            )


def test_month_bounds_roll_the_year() -> None:
    assert month_bounds(dt.date(2027, 12, 31)) == (dt.date(2027, 12, 1), dt.date(2028, 1, 1))
    assert partition_name(dt.date(2027, 1, 5)) == "op_chain_snapshot_202701"


# --- the worker's gates: 16 rows x every sleeve ----------------------------------------------

_FIELD = {
    SleeveGroup.O1M: "options_o1m_execution_enabled",
    SleeveGroup.O1W: "options_o1w_execution_enabled",
    SleeveGroup.O2: "options_o2_execution_enabled",
    SleeveGroup.O3: "options_o3_execution_enabled",
}


def _settings(**values: object) -> WorkerSettings:
    return WorkerSettings.model_construct(**{**WorkerSettings().model_dump(), **values})


@pytest.mark.parametrize("sleeve", list(Sleeve))
@pytest.mark.parametrize(
    ("dry_run_off", "options", "intraday", "execution"),
    list(itertools.product((False, True), repeat=4)),
)
def test_worker_gates_are_paper_for_every_row_but_all_four(
    sleeve: Sleeve, dry_run_off: bool, options: bool, intraday: bool, execution: bool
) -> None:
    settings = _settings(
        options_desk_dry_run="false" if dry_run_off else "true",
        options_desk_options_enabled="true" if options else "false",
        options_desk_intraday_enabled="true" if intraday else "false",
        **{_FIELD[group_of(sleeve)]: execution},
    )
    all_four = dry_run_off and options and intraday and execution
    assert options_gates(sleeve, settings).mode is (Mode.LIVE if all_four else Mode.PAPER)


@pytest.mark.parametrize("value", ["yes", "1", "on", "TRUE ", "True"])
def test_the_worker_parses_the_desk_switches_the_desks_way(value: str) -> None:
    """Only exactly ``true`` (any case, stripped) turns a desk switch on; DRY_RUN is on unless
    exactly ``false`` — so "yes" can never read as live here while the desk reads it as off."""
    on = value.strip().lower() == "true"
    settings = _settings(
        options_desk_dry_run="false",
        options_desk_options_enabled=value,
        options_desk_intraday_enabled="true",
        options_o2_execution_enabled=True,
    )
    assert (options_gates(Sleeve.O2, settings).mode is Mode.LIVE) is on
    dry = _settings(
        options_desk_dry_run="no",
        options_desk_options_enabled="true",
        options_desk_intraday_enabled="true",
        options_o2_execution_enabled=True,
    )
    assert options_gates(Sleeve.O2, dry).mode is Mode.PAPER


def test_the_shipped_worker_defaults_are_paper_and_the_documents_ceilings() -> None:
    defaults = {name: f.default for name, f in WorkerSettings.model_fields.items()}
    for name in (
        *_FIELD.values(),
        "options_monitor_enabled",
        "options_collect_enabled",
        "options_scan_enabled",
    ):
        assert defaults[name] is False, name
    assert not [n for n in defaults if n.startswith("options_") and "auto" in n]
    ceilings = options_ceilings(
        _settings(**{k: v for k, v in defaults.items() if k.startswith("options_")})
    )
    assert (ceilings.risk_per_trade_inr_max, ceilings.risk_pct_max, ceilings.max_lots_max) == (
        Decimal("25000"),
        Decimal("1.0"),
        10,
    )
    assert (ceilings.book_daily_loss_inr_max, ceilings.book_monthly_loss_inr_max) == (
        Decimal("30000"),
        Decimal("75000"),
    )
    assert ceilings.hard_exit_latest == dt.time(15, 0)
