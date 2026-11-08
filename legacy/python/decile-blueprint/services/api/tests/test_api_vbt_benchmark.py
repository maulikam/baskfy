"""`GET /vbt/today` p95 < 300 ms with 2,500 instruments (VB8's budget, `docs/vbt/06`).

The `test_api_swing_benchmark.py` pattern: in-process over an ASGI transport, so the socket and
uvicorn are excluded and the query, the join, the tenant check and the serialisation are
included.

**The universe is the worst case, not a typical evening.** 2,500 synthetic instruments each with
a `vb_signal_daily` row for the session, half of them signals and half rejects — a real evening
produces a handful of signals and a few dozen rejects (`01` §4: 6,293 signals over 2,396
sessions). The route is measured against the shape that would hurt it, because that is the shape
a bad day or a loosened filter would produce.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from benchmarks.budgets import BUDGET_BY_KEY, record
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import Instrument, VbBreadthDaily, VbConfig, VbSignalDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.config import Gate, SignalState

pytestmark = [requires_db, pytest.mark.db, pytest.mark.benchmark]

AS_OF = dt.date(2026, 8, 18)
INSTRUMENTS = 2_500
REQUESTS = 40


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]


async def _universe(session: AsyncSession, user_id: int) -> None:
    instruments = [
        Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=f"VBB{index:04d}",
            name=f"Volume breakout bench {index}",
            series="EQ",
            instrument_type="EQ",
            listed_on=dt.date(2011, 1, 1),
            is_active=True,
        )
        for index in range(INSTRUMENTS)
    ]
    session.add_all(instruments)
    await session.flush()
    session.add_all(
        VbSignalDaily(
            user_id=user_id,
            date=AS_OF,
            instrument_id=instrument.id,
            # Half signals, half rejects: the page renders both lists, so measuring only one
            # would measure half the route.
            state=(SignalState.SIGNAL.value if index % 2 == 0 else SignalState.SCAN_ONLY.value),
            failed_filters=[] if index % 2 == 0 else ["B", "F"],
            close=Decimal("149.60"),
            close_raw=Decimal("149.60"),
            adj_factor=Decimal(1),
            limit_price=Decimal("149.60"),
            stop_price=Decimal("131.65"),
            change_pct=Decimal("8.40"),
            rvol=Decimal("3.20"),
            close_position=Decimal("0.9100"),
            ret_20_pct=Decimal("12.40"),
            turnover_avg_20=340_000_000,
            sma_200=Decimal("100.00"),
            ema_21=Decimal("138.20"),
            high_20_prior=Decimal("146.00"),
            rank_key=340_000_000 - index,
            locked_upper_circuit=bool(index % 11 == 0),
        )
        for index, instrument in enumerate(instruments)
    )
    session.add(
        VbBreadthDaily(
            user_id=user_id,
            date=AS_OF,
            universe_count=INSTRUMENTS,
            measured_count=INSTRUMENTS,
            above_count=1_560,
            pct_above_dma=Decimal("62.4000"),
            gate=Gate.OPEN.value,
            dma_bars=200,
            detail={
                "funnel": {
                    "universe": INSTRUMENTS,
                    "with_bar": INSTRUMENTS,
                    "with_dma": INSTRUMENTS,
                    "scan_hits": INSTRUMENTS,
                    "signals": INSTRUMENTS // 2,
                }
            },
        )
    )
    await session.flush()


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def test_vbt_today_p95_is_under_300_ms_over_2500_instruments(
    settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, public_id = await make_user(screener_session, "vbt-bench@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    screener_session.add(VbConfig(user_id=user_id, updated_by="test"))
    await screener_session.flush()
    await _universe(screener_session, user_id)

    took: list[float] = []
    async with running_app(settings, screener_session) as client:
        headers = bearer(public_id)
        first = await client.get(url("/vbt/today"), headers=headers)
        assert first.status_code == 200, first.text
        body = first.json()
        assert len(body["candidates"]) + len(body["rejects"]) == INSTRUMENTS
        for _ in range(REQUESTS):
            started = time.perf_counter()
            response = await client.get(url("/vbt/today"), headers=headers)
            took.append(time.perf_counter() - started)
            assert response.status_code == 200
    p95_ms = _p95(took) * 1000
    limit = BUDGET_BY_KEY["vbt_today_p95"].limit
    assert limit is not None
    assert p95_ms < limit, f"/vbt/today p95 {p95_ms:.0f} ms >= {limit} ms"
    record(
        "vbt_today_p95",
        round(p95_ms, 1),
        unit="ms",
        method=f"p95 of {REQUESTS} in-process ASGI GET /vbt/today, every one serialising "
        f"{INSTRUMENTS} rows (median {sorted(took)[len(took) // 2] * 1000:.0f} ms)",
        dataset=f"the seeded dataset plus {INSTRUMENTS} synthetic instruments, each with a "
        f"signal row for the session — the route's worst case, not a typical evening",
    )
