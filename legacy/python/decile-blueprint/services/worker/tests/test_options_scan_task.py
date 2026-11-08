"""OP4 — the ``baskfy.options.scan`` task: gated, database-only, idempotent per minute.

``docs/options/06`` OP4 AC asserted here:

* **the scan task is idempotent per minute** — the same minute run twice leaves one row per sleeve
  with the same values (an upsert on ``(user_id, sleeve, ts)``, house rule 7);
* it is **gated** — with ``BASKFY_OPTIONS_SCAN_ENABLED`` or ``BASKFY_OPTIONS_COLLECT_ENABLED``
  off (both default off), outside 09:15-15:30 or with no sole tenant, not even a database session
  is opened; on an NSE holiday nothing is scanned;
* it makes **no Kite call at all** (PACK.11): its module names no provider, and the task never
  builds one;
* on a real database, the quiet-monthly fixture (the same bars and chain as the core tests, stored
  as OP3 stores them) yields O1-M ``WOULD_TRADE`` priced from the 10:00 snapshot.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import (
    AppUser,
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OpBookConfig,
    OpChainSnapshot,
    OpContract,
    OpIndexMinute,
    OpScan,
)
from baskfy_core.options.config import Mode, Sleeve
from baskfy_core.options.scan import Snapshot
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.options import scan as S
from baskfy_worker.options.scan import scan_gate_free, scan_minute_for

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
CORE_TESTS: Final = REPO_ROOT / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

import options_scan_fixtures as F  # noqa: E402 - after the sys.path insert above

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = REPO_ROOT / "services" / "api"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY: Final = F.QUIET_MONTHLY
INSIDE: Final = "2026-10-27T10:01:20+05:30"


# --- the free gate -------------------------------------------------------------------------------


class TestTheGate:
    NOW = dt.datetime(2026, 10, 27, 10, 1, 20, tzinfo=IST)

    def test_the_scan_flag_refuses_first(self) -> None:
        out = scan_gate_free(self.NOW, scan_enabled=False, collect_enabled=True, user_id=1)
        assert out == "BASKFY_OPTIONS_SCAN_ENABLED is false"

    def test_the_collectors_flag_refuses_next(self) -> None:
        out = scan_gate_free(self.NOW, scan_enabled=True, collect_enabled=False, user_id=1)
        assert out is not None and out.startswith("BASKFY_OPTIONS_COLLECT_ENABLED is false")

    @pytest.mark.parametrize(
        ("clock", "refused"),
        [((9, 14), True), ((9, 15), False), ((15, 30), False), ((15, 31), True)],
    )
    def test_the_window(self, clock: tuple[int, int], refused: bool) -> None:
        now = dt.datetime(2026, 10, 27, *clock, tzinfo=IST)
        out = scan_gate_free(now, scan_enabled=True, collect_enabled=True, user_id=1)
        assert (out is not None) is refused

    def test_no_tenant(self) -> None:
        out = scan_gate_free(self.NOW, scan_enabled=True, collect_enabled=True, user_id=None)
        assert out == "no BASKFY_SOLE_USER_ID configured"

    def test_everything_on(self) -> None:
        assert scan_gate_free(self.NOW, scan_enabled=True, collect_enabled=True, user_id=1) is None


class TestTheCeleryTask:
    def _wire(
        self, monkeypatch: pytest.MonkeyPatch, *, scan: bool, collect: bool, user: int | None
    ) -> list[str]:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        calls: list[str] = []

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        monkeypatch.setattr(celery_tasks, "sole_user_id", lambda: user)
        monkeypatch.setattr(
            celery_tasks,
            "get_worker_settings",
            lambda: WorkerSettings(
                _env_file=None, options_scan_enabled=scan, options_collect_enabled=collect
            ),
        )
        return calls

    @pytest.mark.parametrize(
        ("scan", "collect", "user", "at", "reason"),
        [
            (False, False, 1, INSIDE, "BASKFY_OPTIONS_SCAN_ENABLED is false"),
            (False, True, 1, INSIDE, "BASKFY_OPTIONS_SCAN_ENABLED is false"),
            (True, False, 1, INSIDE, "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to scan)"),
            (True, True, 1, "2026-10-27T08:59:00+05:30", "outside 09:15-15:30 IST"),
            (True, True, None, INSIDE, "no BASKFY_SOLE_USER_ID configured"),
        ],
    )
    def test_refused_before_any_database_session(  # noqa: PLR0913, PLR0917 - one per switch
        self,
        monkeypatch: pytest.MonkeyPatch,
        scan: bool,
        collect: bool,
        user: int | None,
        at: str,
        reason: str,
    ) -> None:
        from baskfy_worker.tasks.celery_tasks import options_scan_task  # noqa: PLC0415

        self._wire(monkeypatch, scan=scan, collect=collect, user=user)
        assert options_scan_task.run(at)["skipped"] == reason

    def test_a_holiday_scans_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        async def holiday(session: object, day: dt.date) -> bool:
            del session, day
            return False

        async def never(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("scanned on a holiday")

        def run_in_session(
            operation: Callable[[object], Coroutine[object, object, object]],
        ) -> object:
            return asyncio.run(operation(object()))

        monkeypatch.setattr(celery_tasks, "is_session_day", holiday)
        monkeypatch.setattr(celery_tasks, "scan_minute_for", never)
        monkeypatch.setattr(celery_tasks, "run_in_session", run_in_session)
        monkeypatch.setattr(celery_tasks, "sole_user_id", lambda: 7)
        monkeypatch.setattr(
            celery_tasks,
            "get_worker_settings",
            lambda: WorkerSettings(
                _env_file=None, options_scan_enabled=True, options_collect_enabled=True
            ),
        )
        out = celery_tasks.options_scan_task.run("2026-10-02T11:00:00+05:30")  # Gandhi Jayanti
        assert out["skipped"] == "not an NSE trading day"

    def test_both_flags_default_off(self) -> None:
        from baskfy_worker.settings import WorkerSettings  # noqa: PLC0415

        settings = WorkerSettings(_env_file=None)
        assert settings.options_scan_enabled is False
        assert settings.options_collect_enabled is False

    def test_the_task_and_its_module_never_reach_kite(self) -> None:
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        body = inspect.getsource(celery_tasks.options_scan_task)
        assert "build_options_kite" not in body and "kite" not in body.lower().replace(
            "no kite call", ""
        )
        source = inspect.getsource(S)
        for name in ("baskfy_providers", "KiteProvider", "place_order", "baskfy_execution"):
            assert name not in source, name

    def test_beat_sends_it_every_market_minute_after_the_collector(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT  # noqa: PLC0415

        entry = BEAT_SCHEDULE["options-scan"]
        assert entry["task"] == "baskfy.options.scan"
        options = entry["options"]
        assert isinstance(options, dict)
        assert options["queue"] == QUEUE_DEFAULT
        assert options["expires"] == 55 and options["countdown"] == 20


# --- on a real database --------------------------------------------------------------------------


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
        await session.execute(sa.delete(OpContract).where(OpContract.underlying == "NIFTY"))
        await session.execute(sa.delete(OpIndexMinute))
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _index_instrument(session: AsyncSession) -> int:
    existing = (
        await session.execute(
            sa.select(Instrument).where(
                Instrument.exchange_id == NSE_EXCHANGE_ID, Instrument.symbol == "NIFTY 50"
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.instrument_type = "INDEX"
        existing.kite_token = 256265
        await session.flush()
        return existing.id
    row = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol="NIFTY 50",
        name="NIFTY 50",
        instrument_type="INDEX",
        kite_token=256265,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _index_def(session: AsyncSession, slug: str, ident: int) -> int:
    found = (
        await session.execute(sa.select(IndexDef.id).where(IndexDef.slug == slug))
    ).scalar_one_or_none()
    if found is None:
        session.add(IndexDef(id=ident, slug=slug, name=slug, is_universe=False, sort_order=0))
        await session.flush()
        found = ident
    await session.execute(sa.delete(IndexSnapshotDaily).where(IndexSnapshotDaily.index_id == found))
    return int(found)


def _snapshot_rows(snap: Snapshot) -> list[dict[str, object]]:
    return [
        {
            "ts": snap.ts,
            "instrument_token": q.instrument_token,
            "expiry": q.expiry,
            "strike": q.strike,
            "option_type": q.option_type.value,
            "spot": snap.spot,
            "bid": q.bid,
            "ask": q.ask,
            "last": None,
            "bid_qty": q.bids[0].quantity if q.bids else None,
            "ask_qty": q.asks[0].quantity if q.asks else None,
            "depth_json": {
                "buy": [{"price": str(lv.price), "quantity": lv.quantity} for lv in q.bids],
                "sell": [{"price": str(lv.price), "quantity": lv.quantity} for lv in q.asks],
            },
            "volume": 0,
            "oi": q.oi,
            "source": "QUOTE",
        }
        for q in snap.quotes
    ]


def _chain_at(minute: dt.datetime, smile: F.Smile) -> Snapshot:
    bars = F.quiet_monthly_bars()
    spot = [b for b in bars if b.ts + dt.timedelta(minutes=1) <= minute][-1].close
    exps = [DAY, dt.date(2026, 11, 3)]
    fwd = {e: F.carry_forward(spot, minute, e) for e in exps}
    return F.snapshot(minute, spot, exps, smile, forwards=fwd)


async def _seed_day(session: AsyncSession) -> int:
    user = AppUser(
        public_id=f"op4-{uuid.uuid4().hex[:8]}", email=f"op4-{uuid.uuid4().hex[:8]}@x.test"
    )
    session.add(user)
    await session.flush()
    await session.execute(
        sa.insert(OpContract),
        [
            {
                "instrument_token": c.instrument_token,
                "tradingsymbol": c.tradingsymbol,
                "underlying": "NIFTY",
                "expiry": c.expiry,
                "strike": c.strike,
                "option_type": c.option_type.value,
                "lot_size": c.lot_size,
                "tick_size": c.tick_size,
                "first_seen": DAY,
                "last_seen": DAY,
                "expired": False,
            }
            for c in F.MASTER
        ],
    )
    nifty = await _index_instrument(session)
    await session.execute(
        sa.insert(OpIndexMinute),
        [
            {
                "instrument_id": nifty,
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "source": "KITE_HIST",
            }
            for b in F.quiet_monthly_bars()
            if b.ts < F.at(DAY, 10, 10)
        ],
    )
    n50 = await _index_def(session, "nifty-50", 32001)
    vix = await _index_def(session, "india-vix", 32002)
    closes = F.rising_closes()
    await session.execute(
        sa.insert(IndexSnapshotDaily),
        [
            {"index_id": n50, "date": DAY - dt.timedelta(days=len(closes) - i), "level": level}
            for i, level in enumerate(closes)
        ]
        + [{"index_id": vix, "date": DAY - dt.timedelta(days=1), "level": Decimal(14)}],
    )
    await session.execute(
        sa.insert(OpChainSnapshot), _snapshot_rows(_chain_at(F.at(DAY, 10, 0), F.skew(0.30, 0.33)))
    )
    await session.execute(
        sa.insert(OpChainSnapshot), _snapshot_rows(_chain_at(F.at(DAY, 10, 5), F.skew(0.5, 0.5)))
    )
    await session.flush()
    return int(user.id)


def _paper(_sleeve: Sleeve) -> Mode:
    return Mode.PAPER


async def _rows(session: AsyncSession, user_id: int) -> list[OpScan]:
    return list(
        (await session.execute(sa.select(OpScan).where(OpScan.user_id == user_id))).scalars().all()
    )


@requires_db
class TestTheScanOnADatabase:
    async def test_a_minute_writes_one_row_per_sleeve_idempotently(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            now = dt.datetime(2026, 10, 27, 10, 1, 20, tzinfo=IST)
            first = await scan_minute_for(session, user_id, now, trading_day=True, mode_of=_paper)
            assert first.written == 5
            before = {
                (r.sleeve, r.state, tuple(r.reasons), str(r.candidates), r.stale)
                for r in await _rows(session, user_id)
            }
            again = await scan_minute_for(session, user_id, now, trading_day=True, mode_of=_paper)
            rows = await _rows(session, user_id)
            assert again.written == 5 and len(rows) == 5
            after = {
                (r.sleeve, r.state, tuple(r.reasons), str(r.candidates), r.stale) for r in rows
            }
            assert after == before

    async def test_the_quiet_monthly_would_trade_from_the_1000_snapshot(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            # At 10:06 the latest snapshot is 10:05 (different premiums); O1-M reads 10:00.
            now = dt.datetime(2026, 10, 27, 10, 6, 20, tzinfo=IST)
            report = await scan_minute_for(session, user_id, now, trading_day=True, mode_of=_paper)
            assert report.states["O1M"] == "WOULD_TRADE"
            assert report.states["O1W"] == "NOT_TODAY"
            (o1m,) = [r for r in await _rows(session, user_id) if r.sleeve == "O1M"]
            assert o1m.as_of_minute == F.at(DAY, 10, 0)
            assert o1m.ts == dt.datetime(2026, 10, 27, 10, 6, tzinfo=IST)
            # The column is typed as a JSON object; OP4 stores a list of candidates in it.
            stored: object = o1m.candidates
            assert isinstance(stored, list) and len(stored) == 1
            candidate = stored[0]
            assert isinstance(candidate, dict)
            assert candidate["points"] == "39.65" and candidate["lots"] == 1
            assert candidate["sizing_mode"] == "PAPER_ONE_LOT"
            assert o1m.numbers["prev_close"] == "25000.00"

    async def test_a_paused_book_shows_paused(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _seed_day(session)
            session.add(OpBookConfig(user_id=user_id, paused_until=DAY, paused_reason="BOOK"))
            await session.flush()
            now = dt.datetime(2026, 10, 27, 10, 1, 20, tzinfo=IST)
            report = await scan_minute_for(session, user_id, now, trading_day=True, mode_of=_paper)
            assert report.states["O1M"] == "PAUSED" and report.states["O2"] == "PAUSED"
            assert report.states["O1W"] == "NOT_TODAY"
