"""`GET /swing/setups` p95 < 300 ms with 2,500 instruments (SW4's deferred budget, SW11; B9).

The `test_load` pattern: in-process over an ASGI transport, so the socket and uvicorn are
excluded and the query, the join, the entitlement check and the serialisation are included. The
universe is **2,500 synthetic instruments with a detection row each** — every one of them a
candidate on the day, which is the worst case for this route (a real day lines a few dozen), and
the number is recorded through ``benchmarks.budgets.record`` beside the docs/11 rows.
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
from baskfy_core.models import Instrument, SwConfig, SwMarketDaily, SwSetupDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID

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
            symbol=f"SWB{i:04d}",
            name=f"Swing bench {i}",
            series="EQ",
            instrument_type="EQ",
            listed_on=dt.date(2011, 1, 1),
            is_active=True,
        )
        for i in range(INSTRUMENTS)
    ]
    session.add_all(instruments)
    await session.flush()
    session.add_all(
        SwSetupDaily(
            user_id=user_id,
            date=AS_OF,
            instrument_id=instrument.id,
            setup="FLAG" if i % 5 else "EP",
            status="SETTING_UP" if i % 7 else "BREAKOUT_TODAY",
            score=Decimal(str(40 + (i % 60))),
            close=Decimal("144.75"),
            trigger=Decimal("149.60"),
            stop_ref=Decimal("141.86"),
            pivot_high=Decimal("149.60"),
            adj_factor=Decimal(1),
            adr_pct=Decimal("4.08"),
            turnover_avg=112_734_212,
            base_bars=35,
            locked_upper_circuit=bool(i % 11 == 0),
            sector_slug=None,
            listed_within_2y=False,
        )
        for i, instrument in enumerate(instruments)
    )
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=AS_OF,
            constituent_count=INSTRUMENTS,
            pct_up_strong_1m=Decimal("3.8000"),
            pct_new_52w_high=Decimal("1.2000"),
            pct_above_ma_slow=Decimal("55.0000"),
            index_slug="nifty-500",
            gate="GREEN",
            exposure_level=1,
            max_open_positions=4,
            max_exposure_pct=Decimal("50.00"),
            new_entries_allowed=True,
            parabolic_count=0,
            detail={"sectors": [], "closed_r_multiples": [], "closed_trades_read": "real"},
        )
    )
    await session.flush()


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def test_swing_setups_p95_is_under_300_ms_over_2500_instruments(
    settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, public_id = await make_user(screener_session, "swing-bench@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    screener_session.add(SwConfig(user_id=user_id, updated_by="test"))
    await screener_session.flush()
    await _universe(screener_session, user_id)

    took: list[float] = []
    async with running_app(settings, screener_session) as client:
        headers = bearer(public_id)
        first = await client.get(url("/swing/setups"), headers=headers)
        assert first.status_code == 200, first.text
        assert len(first.json()["data"]) == INSTRUMENTS
        for _ in range(REQUESTS):
            started = time.perf_counter()
            response = await client.get(url("/swing/setups"), headers=headers)
            took.append(time.perf_counter() - started)
            assert response.status_code == 200
    p95_ms = _p95(took) * 1000
    limit = BUDGET_BY_KEY["swing_setups_p95"].limit
    assert limit is not None
    assert p95_ms < limit, f"/swing/setups p95 {p95_ms:.0f} ms ≥ {limit} ms"
    record(
        "swing_setups_p95",
        round(p95_ms, 1),
        unit="ms",
        method=f"p95 of {REQUESTS} in-process ASGI GET /swing/setups, every one serialising "
        f"{INSTRUMENTS} candidate rows (median {sorted(took)[len(took) // 2] * 1000:.0f} ms)",
        dataset=f"the seeded dataset plus {INSTRUMENTS} synthetic instruments each with a "
        f"detection row for the day — the route's worst case, not a typical morning",
    )
