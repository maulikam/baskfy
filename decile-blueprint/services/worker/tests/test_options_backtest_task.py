"""OP12 — a sleeve's backtest over stored days, into ``op_backtest_run`` (`docs/options/06` OP12).

* the run reads the days the way the live scan does and writes one row: one sleeve, one tier, the
  caveats verbatim (`TestOnADatabase`);
* "Tier 3 over fixture snapshots reproduces the paper path to the rupee" — the stored minutes, read
  back from ``op_chain_snapshot``, give the ₹ the same minutes give in memory;
* a day the master cannot calendar is counted, never guessed;
* the task is on the compute queue, has no Beat entry and refuses without a tenant (`TestTheTask`).
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from test_options_scan_task import (
    _index_def,
    _index_instrument,
    _rolled_back,
    _snapshot_rows,
    op_url,  # noqa: F401 - the module-scoped migration fixture
    requires_db,
)

from baskfy_core.models import (
    AppUser,
    IndexSnapshotDaily,
    OpBacktestRun,
    OpChainSnapshot,
    OpContract,
    OpEventDay,
    OpIndexMinute,
)
from baskfy_core.options.backtest import TIER1_CAVEAT, TIER2_CAVEAT, Tier
from baskfy_core.options.backtest_suite import run_tier
from baskfy_core.options.bars import Bar
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.replay_day import StoredChain
from baskfy_core.options.scan import DayContext, Snapshot
from baskfy_worker.options.backtest_run import run_backtest

CORE_TESTS: Final = Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"
if str(CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(CORE_TESTS))

import options_scan_fixtures as F  # noqa: E402 - after the sys.path insert above

DAY = F.GAP_HOLD  # Tue 13 Oct 2026, a weekly expiry: O3-B's gap holds at 09:45
AFTER_THE_MASTER = dt.date(2028, 1, 4)  # a Tuesday past every expiry the fixture master lists
CFG = OptionsConfig()
CEIL = OptionsCeilings()


def _chain_at(bars: tuple[Bar, ...], minute: dt.datetime) -> Snapshot:
    spot = [b for b in bars if b.ts + dt.timedelta(minutes=1) <= minute][-1].close
    exps = sorted({c.expiry for c in F.MASTER if c.expiry >= DAY})[:2]
    fwd = {e: F.carry_forward(spot, minute, e) for e in exps}
    return F.snapshot(minute, spot, exps, F.flat(0.14), forwards=fwd)


def _minutes() -> list[dt.datetime]:
    """The collector's minutes from O3-B's decision to the last closed bar, every minute."""
    first = F.at(DAY, 9, 45)
    return [first + dt.timedelta(minutes=i) for i in range(0, 345)]


async def _seed(session: AsyncSession, *, chains: bool) -> int:
    user = AppUser(
        public_id=f"op12-{uuid.uuid4().hex[:8]}", email=f"op12-{uuid.uuid4().hex[:8]}@x.test"
    )
    session.add(user)
    await session.flush()
    await session.execute(
        sa.insert(OpContract),
        [
            {
                "instrument_token": c.instrument_token, "tradingsymbol": c.tradingsymbol,
                "underlying": "NIFTY", "expiry": c.expiry, "strike": c.strike,
                "option_type": c.option_type.value, "lot_size": c.lot_size,
                "tick_size": c.tick_size, "first_seen": DAY, "last_seen": DAY,
                # An expired row still calendars the day it was listed on (03 §1).
                "expired": c.expiry < DAY + dt.timedelta(days=1),
            }
            for c in F.MASTER
        ],
    )  # fmt: skip
    nifty = await _index_instrument(session)
    later = tuple(
        Bar(b.ts.replace(year=2028, month=1, day=4), b.open, b.high, b.low, b.close)
        for b in F.gap_hold_bars()
    )
    await session.execute(
        sa.insert(OpIndexMinute),
        [
            {"instrument_id": nifty, "ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "source": "KITE_HIST"}
            for b in (*F.gap_hold_bars(), *later)
        ],
    )  # fmt: skip
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
    if chains:
        for minute in _minutes():
            await session.execute(
                sa.insert(OpChainSnapshot), _snapshot_rows(_chain_at(F.gap_hold_bars(), minute))
            )
    await session.flush()
    return int(user.id)


@requires_db
class TestOnADatabase:
    async def test_tier_one_writes_one_row_with_its_caveat(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _seed(session, chains=False)
            report = await run_backtest(
                session, uid, Sleeve.O3B, Tier.SIGNALS, DAY, AFTER_THE_MASTER,
                options=CFG, ceilings=CEIL, sha="abc123",
            )  # fmt: skip
            row = await session.get(OpBacktestRun, report.run_id)
            assert report.trading_days == 2 and report.uncalendared == 1
            assert row is not None
            assert (row.sleeve, row.tier, row.sessions, row.signals, row.traded) == (
                "O3B",
                1,
                1,
                1,
                0,
            )
            assert row.caveats == TIER1_CAVEAT
            assert row.net_pnl_inr is None and row.win_rate is None
            assert row.params_json["uncalendared"] == 1 and row.git_sha == "abc123"
            sensitivity = row.params_json["sensitivity"]
            assert isinstance(sensitivity, list) and len(sensitivity) == 4  # two O3-B gates x 2
            assert {s["threshold"] for s in sensitivity if isinstance(s, dict)} == {
                "expiry_setups.o3b_gap_min_pct",
                "expiry_setups.o3b_gap_max_pct",
            }

    async def test_an_event_day_is_skipped_as_the_live_scan_would(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _seed(session, chains=False)
            session.add(OpEventDay(date=DAY, reason="MANUAL", source="USER", user_id=uid))
            await session.flush()
            report = await run_backtest(
                session, uid, Sleeve.O3B, Tier.SIGNALS, DAY, DAY, options=CFG, ceilings=CEIL
            )
            assert report.result["skipped_by_reason"] == {"EVENT_DAY": 1}

    async def test_tier_two_stores_the_model_caveat(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _seed(session, chains=False)
            report = await run_backtest(
                session, uid, Sleeve.O3B, Tier.MODELLED, DAY, DAY, options=CFG, ceilings=CEIL
            )
            row = await session.get(OpBacktestRun, report.run_id)
            assert row is not None and row.tier == 2 and row.caveats == TIER2_CAVEAT
            assert row.sessions == 1

    async def test_tier_three_from_the_table_is_the_same_rupees(self, op_url: str) -> None:  # noqa: F811
        async with _rolled_back(op_url) as session:
            uid = await _seed(session, chains=True)
            report = await run_backtest(
                session, uid, Sleeve.O3B, Tier.OBSERVED, DAY, DAY, options=CFG, ceilings=CEIL
            )
            row = await session.get(OpBacktestRun, report.run_id)
            chain = StoredChain({m: _chain_at(F.gap_hold_bars(), m) for m in _minutes()})
            market = F.market(DAY, F.gap_hold_bars())
            expected = run_tier(
            Sleeve.O3B, Tier.OBSERVED, [market], context=DayContext(), options=CFG,
            ceilings=CEIL, source_for=lambda _m: chain,
            )  # fmt: skip
            assert expected.traded == 1  # the stored minutes carry a whole trade
            assert row is not None and row.traded == 1
            assert row.net_pnl_inr == expected.net_pnl_inr
            assert row.caveats.splitlines() == list(expected.caveats)


class TestTheTask:
    def test_on_the_compute_queue_and_never_on_beat(self) -> None:
        from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE  # noqa: PLC0415
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        assert celery_tasks.options_backtest_task.queue == QUEUE_COMPUTE
        assert all(e["task"] != "baskfy.options.backtest" for e in BEAT_SCHEDULE.values())

    def test_refused_without_a_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415

        def no_db(operation: object) -> object:
            raise AssertionError(f"a database session was opened for {operation!r}")

        monkeypatch.setattr(celery_tasks, "run_in_session", no_db)
        monkeypatch.setattr(celery_tasks, "sole_user_id", lambda: None)
        out = celery_tasks.options_backtest_task.run("O2", "1", "2026-01-01", "2026-09-01")
        assert out["skipped"] == "no BASKFY_SOLE_USER_ID configured"
