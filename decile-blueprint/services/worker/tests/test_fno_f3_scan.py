"""F3-4: the evening scan's F3 rows (``gates/f3-4-scan.md``; ``04`` §11, §8; DECISIONS-FO M.5).

Rides ``test_fno_scan_night``'s fixture market (the migrated database, rolled back per test) and
adds what F3 reads: ``fo_index_daily`` history, ``op_index_minute`` bars for the 75-minute
confirm, and the NIFTY chain the fixture already prices.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from test_fno_scan_night import (
    E1,
    HISTORY,
    IST,
    T,
    _market,
    _open_position,
    _rolled_back,
    _rows,
    _user,
    fo_url,  # noqa: F401 - the module-scoped fixture, re-exported for pytest
    requires_db,
)

from baskfy_api.seed import seed_reference
from baskfy_core.fno.config import ScanState
from baskfy_core.models import FoIndexDaily, Instrument, OpIndexMinute
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.fno.scan import run_scan

NIFTY_TOKEN = 256265


def _at(obj: object, *keys: str) -> object:
    """A nested read of a JSON detail without an ``Any``: each key must find a mapping."""
    for key in keys:
        assert isinstance(obj, dict), f"{key}: not a mapping"
        obj = obj[key]
    return obj


def _closes(*, dip: bool = True, flat: bool = False) -> list[Decimal]:
    """Seventy closes ending at 20,000: rising ten a session, with a 300-point dip eight
    sessions back (a pivot low, the support) unless ``flat``."""
    days = HISTORY[-70:]
    out: list[Decimal] = []
    for i, _ in enumerate(days):
        level = Decimal(20000) if flat else Decimal(20000 - 10 * (len(days) - 1 - i))
        if dip and not flat and i == len(days) - 8:
            level -= 300
        out.append(level)
    return out


async def _ready(session: AsyncSession) -> None:
    """The reference rows the fixture market's foreign keys need (the exchange, above all): the
    migration suite drops them and only a worker `session` fixture reseeds them, so a test that
    happens to run first — this file sorts before those — seeds them itself, idempotently."""
    await seed_reference(session)
    await _market(session)


async def _index_history(session: AsyncSession, underlying: str, closes: list[Decimal]) -> None:
    days = HISTORY[-len(closes) :]
    await session.execute(
        sa.insert(FoIndexDaily).values(
            [
                {
                    "underlying": underlying,
                    "trade_date": day,
                    "open": c - 5,
                    "high": c + 20,
                    "low": c - 20,
                    "close": c,
                    "source": "KITE_HIST",
                }
                for day, c in zip(days, closes, strict=True)
            ]
        )
    )


async def _index_minutes(session: AsyncSession, symbol: str, *, sessions: int = 2) -> None:
    """The index's instrument row and every minute of the last ``sessions`` sessions, rising
    through the day so the last completed 75-minute bar closes above its ten-bar average."""
    existing = (
        await session.execute(
            sa.select(Instrument).where(
                Instrument.exchange_id == NSE_EXCHANGE_ID, Instrument.symbol == symbol
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=symbol,
            name=symbol,
            series=None,
            instrument_type="INDEX",
            kite_token=NIFTY_TOKEN,
            listed_on=HISTORY[0],
            is_active=True,
        )
        session.add(existing)
        await session.flush()
    else:
        existing.instrument_type = "INDEX"
        existing.kite_token = NIFTY_TOKEN
        await session.flush()
    rows: list[dict[str, object]] = []
    days = HISTORY[-sessions:]
    for di, day in enumerate(days):
        start = dt.datetime.combine(day, dt.time(9, 15), IST)
        for m in range(375):
            price = Decimal(19900 + di * 50) + Decimal(m) / 10
            rows.append(
                {
                    "instrument_id": existing.id,
                    "ts": start + dt.timedelta(minutes=m),
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "source": "KITE_HIST",
                }
            )
    await session.execute(sa.insert(OpIndexMinute).values(rows))


@requires_db
class TestF3Scan:
    async def test_the_candidate_names_the_spread_the_level_and_the_expiry(
        self,
        fo_url: str,  # noqa: F811 - the module-scoped fixture
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            await _index_history(session, "NIFTY", _closes())
            await _index_minutes(session, "NIFTY 50")
            user_id = await _user(session)

            out = await run_scan(session, T, user_ids=[user_id])

            assert _at(out, "users", str(user_id), "F3N") == {ScanState.CANDIDATE.value: 1}
            row = (await _rows(session, user_id, "F3N"))["NIFTY"]
            detail = row.detail
            assert _at(detail, "direction") == "UP" and _at(detail, "direction_daily") == "UP"
            assert _at(detail, "confirm", "agrees") is True
            assert (
                _at(detail, "expiry") == E1.isoformat() and _at(detail, "expiry_kind") == "weekly"
            )
            assert _at(detail, "option_type") == "PE"
            assert Decimal(str(_at(detail, "short_strike"))) == 19700
            assert Decimal(str(_at(detail, "wing_strike"))) == 19300
            legs = _at(detail, "legs")
            assert isinstance(legs, list)
            assert [_at(leg, "role") for leg in legs] == ["LONG_PUT", "SHORT_PUT"]
            assert Decimal(str(_at(detail, "levels", "support"))) == Decimal(
                "19610"
            )  # the dip's low
            assert Decimal(str(_at(detail, "level"))) == 19610
            assert row.credit is not None and row.credit > 10
            assert row.max_loss_per_lot is not None and row.max_loss_per_lot > 0
            assert _at(detail, "lots") == 1  # paper at ₹0: one lot
            assert any("out at once if the index trades beyond 19610" in r for r in row.reasons)
            assert any("09:20" in r for r in row.reasons), "the intraday check is the monitor's"

    async def test_no_index_history_is_no_data_that_names_the_backfill(self, fo_url: str) -> None:  # noqa: F811 - the module-scoped fixture
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            user_id = await _user(session)

            out = await run_scan(session, T, user_ids=[user_id])

            assert _at(out, "users", str(user_id), "F3B") == {ScanState.NO_DATA.value: 1}
            row = (await _rows(session, user_id, "F3B"))["BANKNIFTY"]
            assert "fo_index_daily" in row.reasons[0] and "index-daily" in row.reasons[0]

    async def test_no_minute_bars_is_no_data_not_an_unconfirmed_trade(self, fo_url: str) -> None:  # noqa: F811 - the module-scoped fixture
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            await _index_history(session, "NIFTY", _closes())
            user_id = await _user(session)

            await run_scan(session, T, user_ids=[user_id])

            row = (await _rows(session, user_id, "F3N"))["NIFTY"]
            assert row.state == ScanState.NO_DATA.value
            assert "75-minute" in row.reasons[0]
            assert _at(row.detail, "direction_daily") == "UP"

    async def test_a_flat_tape_is_no_signal(self, fo_url: str) -> None:  # noqa: F811 - the module-scoped fixture
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            await _index_history(session, "NIFTY", _closes(flat=True))
            await _index_minutes(session, "NIFTY 50")
            user_id = await _user(session)

            await run_scan(session, T, user_ids=[user_id])

            row = (await _rows(session, user_id, "F3N"))["NIFTY"]
            assert row.state == ScanState.NO_SIGNAL.value
            assert _at(row.detail, "direction") == "NONE"

    async def test_an_open_spread_is_reported_and_nothing_else_is_proposed(
        self,
        fo_url: str,  # noqa: F811 - the module-scoped fixture
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            await _index_history(session, "NIFTY", _closes())
            await _index_minutes(session, "NIFTY 50")
            user_id = await _user(session)
            position_id = await _open_position(session, user_id, "F3N", "NIFTY")

            await run_scan(session, T, user_ids=[user_id])

            row = (await _rows(session, user_id, "F3N"))["NIFTY"]
            assert row.state == ScanState.OPEN_POSITION.value
            assert _at(row.detail, "position_id") == position_id
            assert "one per underlying" in row.reasons[0]

    async def test_a_rerun_leaves_one_row_per_underlying(self, fo_url: str) -> None:  # noqa: F811 - the module-scoped fixture
        async with _rolled_back(fo_url) as session:
            await _ready(session)
            await _index_history(session, "NIFTY", _closes())
            await _index_minutes(session, "NIFTY 50")
            user_id = await _user(session)

            await run_scan(session, T, user_ids=[user_id])
            await run_scan(session, T, user_ids=[user_id])

            assert set(await _rows(session, user_id, "F3N")) == {"NIFTY"}
            assert set(await _rows(session, user_id, "F3B")) == {"BANKNIFTY"}
