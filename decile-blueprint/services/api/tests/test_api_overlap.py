"""`GET /overlap` — the day's candidates across the sleeves, from what was stored.

Read-only over stored rows, so the assertions are about **which** rows and **whose words**:
each strategy at its own latest session, the count as arithmetic over those, the swing feed's
link and earnings date on loan, and a caller who is not the sole tenant told so rather than
shown an empty morning.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from api_helpers import api_settings, bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.overlap import Strategy, overlap
from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    SwCatalyst,
    SwMarketDaily,
    SwSetupDaily,
    TwBreadthDaily,
    TwSignalDaily,
    TwStateDaily,
    VbBreadthDaily,
    VbSignalDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

pytestmark = [requires_db, pytest.mark.db]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SWING_DAY = dt.date(2026, 9, 11)
#: The other two sleeves are a session behind — a skipped nightly step, which the view names.
OTHER_DAY = dt.date(2026, 9, 10)


async def _instrument(session: AsyncSession, symbol: str) -> int:
    instrument = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=symbol,
        name=f"{symbol} LIMITED",
        series="EQ",
        instrument_type="EQ",
        listed_on=dt.date(2011, 1, 1),
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    return int(instrument.id)


async def _swing(
    session: AsyncSession, *, user_id: int, instrument_id: int, setup: str, status: str
) -> None:
    session.add(
        SwSetupDaily(
            user_id=user_id,
            date=SWING_DAY,
            instrument_id=instrument_id,
            setup=setup,
            status=status,
            score=Decimal("62.06"),
            close=Decimal("144.75"),
            trigger=Decimal("149.60"),
            stop_ref=Decimal("141.86"),
            pivot_high=Decimal("149.60"),
            adj_factor=Decimal(1),
            locked_upper_circuit=False,
            listed_within_2y=False,
        )
    )
    await session.flush()


async def _swing_market(session: AsyncSession, *, user_id: int) -> None:
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=SWING_DAY,
            constituent_count=41,
            pct_up_strong_1m=Decimal("3.8000"),
            pct_new_52w_high=Decimal("1.2000"),
            pct_above_ma_slow=Decimal("55.0000"),
            index_slug="nifty-mid-small-400",
            index_close=Decimal("20240.00"),
            index_ma_fast=Decimal("20200.00"),
            index_ma_slow=Decimal("20150.00"),
            gate="GREEN",
            exposure_level=0,
            max_open_positions=2,
            max_exposure_pct=Decimal("25.00"),
            new_entries_allowed=True,
            parabolic_count=0,
            detail=None,
        )
    )
    await session.flush()


async def _vbt(session: AsyncSession, *, user_id: int, instrument_id: int, state: str) -> None:
    session.add(
        VbSignalDaily(
            user_id=user_id,
            date=OTHER_DAY,
            instrument_id=instrument_id,
            state=state,
            close=Decimal("100"),
            close_raw=Decimal("100.50"),
            limit_price=Decimal("101"),
            stop_price=Decimal("95"),
        )
    )
    await session.flush()


async def _vbt_breadth(session: AsyncSession, *, user_id: int) -> None:
    session.add(
        VbBreadthDaily(
            user_id=user_id,
            date=OTHER_DAY,
            universe_count=10,
            measured_count=10,
            above_count=5,
            pct_above_dma=Decimal("50"),
            gate="OPEN",
        )
    )
    await session.flush()


async def _twt(
    session: AsyncSession, *, user_id: int, instrument_id: int, signal: str | None
) -> None:
    session.add(
        TwStateDaily(
            user_id=user_id,
            date=OTHER_DAY,
            instrument_id=instrument_id,
            close=Decimal("149.60"),
            close_raw=Decimal("149.60"),
            adj_factor=Decimal(1),
            week_close_0=Decimal("149.60"),
            week_close_1=Decimal("148.90"),
            week_close_2=Decimal("147.50"),
            week_range_pct=Decimal("1.4237"),
            month_low_3=Decimal("100.00"),
            month_low_ratio=Decimal("1.4960"),
            vol_sma_50=250_000,
            volume=310_000,
            turnover_inr=500_000_000,
            turnover_avg_20=500_000_000,
            sma_dma=Decimal("120.00"),
            sessions_in_state=4,
            bars_in_window=260,
            locked_upper_circuit=False,
        )
    )
    if signal is not None:
        session.add(
            TwSignalDaily(
                user_id=user_id,
                date=OTHER_DAY,
                instrument_id=instrument_id,
                state=signal,
                failed_filters=[],
                entry_reference_close=Decimal("149.60"),
                stop_preview=Decimal("119.68"),
                sessions_out_before=7,
                rank_key=500_000_000,
                turnover_avg_20=500_000_000,
            )
        )
    await session.flush()


async def _twt_breadth(session: AsyncSession, *, user_id: int) -> None:
    session.add(
        TwBreadthDaily(
            user_id=user_id,
            date=OTHER_DAY,
            universe_count=3_413,
            measured_count=1_769,
            above_count=905,
            pct_above_dma=Decimal("51.1588"),
            gate="OPEN",
            dma_bars=200,
            thin_session=False,
            detail=None,
        )
    )
    await session.flush()


async def _a_morning(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Four names: BOTH on swing (EP) and tight (SIGNAL); FLAGCO swing only; QUIET merely in the
    tight state; REJECT a volume SCAN_ONLY. Plus a catalyst and an earnings date on BOTH."""
    ids = {
        symbol: await _instrument(session, symbol)
        for symbol in ("BOTH", "FLAGCO", "QUIET", "REJECT")
    }
    await _swing_market(session, user_id=user_id)
    await _swing(session, user_id=user_id, instrument_id=ids["BOTH"], setup="EP", status="GAP_DAY")
    await _swing(
        session, user_id=user_id, instrument_id=ids["FLAGCO"], setup="FLAG", status="SETTING_UP"
    )
    await _twt_breadth(session, user_id=user_id)
    await _twt(session, user_id=user_id, instrument_id=ids["BOTH"], signal="SIGNAL")
    await _twt(session, user_id=user_id, instrument_id=ids["QUIET"], signal=None)
    await _vbt_breadth(session, user_id=user_id)
    await _vbt(session, user_id=user_id, instrument_id=ids["REJECT"], state="SCAN_ONLY")
    session.add(
        SwCatalyst(
            user_id=user_id,
            instrument_id=ids["BOTH"],
            headline="Press Release - BOTH wins a multi-year order",
            published_at=dt.datetime(2026, 9, 11, 18, 32, tzinfo=IST),
            url="https://nsearchives.nseindia.com/corporate/BOTH.pdf",
            source="NSE_ANNOUNCEMENT",
        )
    )
    session.add(
        SwCatalyst(
            user_id=user_id,
            instrument_id=ids["BOTH"],
            headline="Board meeting",
            published_at=None,
            url="https://www.nseindia.com/companies-listing/corporate-filings-event-calendar",
            source="NSE_EVENT_CALENDAR",
            earnings_date=dt.date(2026, 9, 15),
        )
    )
    await session.flush()
    return ids


class TestTheReadModel:
    async def test_each_strategy_speaks_at_its_own_session_and_the_count_is_arithmetic(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-read@example.com")
        ids = await _a_morning(screener_session, user_id)

        view = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)

        assert view.strategies_read is True
        assert view.sessions == {
            Strategy.SWING: SWING_DAY,
            Strategy.VOLUME_BREAKOUT: OTHER_DAY,
            Strategy.THREE_WEEKS_TIGHT: OTHER_DAY,
        }
        # Actionable scope: BOTH (EP + SIGNAL) and FLAGCO (FLAG). QUIET is in-state only and
        # REJECT failed a filter — neither is a row a plan builder could take.
        assert [row.symbol for row in view.rows] == ["BOTH", "FLAGCO"]
        both = view.rows[0]
        assert both.instrument_id == ids["BOTH"]
        assert both.strategy_count == 2
        assert [
            (hit.strategy, hit.as_of, hit.detail, hit.actionable) for hit in both.strategies
        ] == [
            (Strategy.SWING, SWING_DAY, "EP · GAP_DAY", True),
            (Strategy.THREE_WEEKS_TIGHT, OTHER_DAY, "tight 4 sessions · signal", True),
        ]
        # The close is the first exchange print offered, in strategy order — swing's here.
        assert both.close == Decimal("144.75")
        assert both.catalyst is not None
        assert both.catalyst.headline == "Press Release - BOTH wins a multi-year order"
        assert both.catalyst.earnings_date == dt.date(2026, 9, 15)
        # The rules baseline read the headline: an order, high priority, and it says why.
        assert both.catalyst_tag is not None
        assert (both.catalyst_tag.event_type, both.catalyst_tag.review_priority) == (
            "order",
            "high",
        )
        assert both.catalyst_tag.matched == ("order",)
        assert both.catalyst_tag.source == "rules"
        assert view.rows[1].catalyst is None
        assert view.rows[1].catalyst_tag is None

    async def test_scope_all_keeps_every_row_the_scans_wrote_and_says_which_is_which(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-all@example.com")
        await _a_morning(screener_session, user_id)

        view = await overlap(
            screener_session, user_id=user_id, strategies_user_id=user_id, scope="all"
        )

        # Two strategies first, then actionable before not, then symbol.
        assert [(row.symbol, row.strategy_count, row.actionable) for row in view.rows] == [
            ("BOTH", 2, True),
            ("FLAGCO", 1, True),
            ("QUIET", 1, False),
            ("REJECT", 1, False),
        ]
        by_symbol = {row.symbol: row for row in view.rows}
        assert by_symbol["QUIET"].strategies[0].detail == "tight 4 sessions"
        assert by_symbol["REJECT"].strategies[0].detail == "scanned, did not pass the filters"
        assert by_symbol["REJECT"].close == Decimal("100.50")

    async def test_a_parabolic_short_is_detected_but_never_actionable(
        self, screener_session: AsyncSession
    ) -> None:
        """Detect-only (docs/swing/02 Track C): the plan builder never takes it, so neither
        does this flag. It still appears under ``scope=all`` — the scan did write it."""
        user_id, _ = await make_user(screener_session, "overlap-short@example.com")
        instrument_id = await _instrument(screener_session, "PARAB")
        await _swing_market(screener_session, user_id=user_id)
        await _swing(
            screener_session,
            user_id=user_id,
            instrument_id=instrument_id,
            setup="PARABOLIC_SHORT",
            status="SETTING_UP",
        )

        default = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        assert default.rows == ()
        everything = await overlap(
            screener_session, user_id=user_id, strategies_user_id=user_id, scope="all"
        )
        [row] = everything.rows
        assert (row.symbol, row.actionable, row.strategies[0].detail) == (
            "PARAB",
            False,
            "PARABOLIC_SHORT · SETTING_UP",
        )

    async def test_without_the_sole_tenant_the_strategies_are_not_read_and_the_view_says_so(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-other@example.com")
        await _a_morning(screener_session, user_id)

        view = await overlap(screener_session, user_id=user_id, strategies_user_id=None)

        assert view.strategies_read is False
        assert view.rows == ()
        assert all(day is None for day in view.sessions.values())


class TestTheRoute:
    @pytest.fixture
    def settings(self, seeded_url: str) -> Settings:
        return api_settings(seeded_url)

    async def test_it_needs_a_signed_in_caller_and_serves_decimals_as_stored(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-route@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await _a_morning(screener_session, user_id)

        async with running_app(settings, screener_session) as client:
            anonymous = await client.get(url("/overlap"))
            signed = await client.get(url("/overlap"), headers=bearer(public_id))
            everything = await client.get(url("/overlap?scope=all"), headers=bearer(public_id))
            bad = await client.get(url("/overlap?scope=ranked"), headers=bearer(public_id))

        assert anonymous.status_code == 401
        assert bad.status_code == 400  # the app renders validation as a 400 problem
        assert signed.status_code == 200, signed.text
        body = signed.json()
        assert body["strategies_read"] is True
        assert body["scope"] == "actionable"
        assert body["sessions"] == {
            "swing": SWING_DAY.isoformat(),
            "volume_breakout": OTHER_DAY.isoformat(),
            "three_weeks_tight": OTHER_DAY.isoformat(),
        }
        assert [row["symbol"] for row in body["data"]] == ["BOTH", "FLAGCO"]
        both = body["data"][0]
        # House rule 8: the canonical encoder keeps the stored precision — `144.75`, never `144.7`.
        assert '"close":144.75' in signed.text
        assert both["strategy_count"] == 2
        assert [hit["strategy"] for hit in both["strategies"]] == ["swing", "three_weeks_tight"]
        assert both["strategies"][0]["ref"] == "/swing"
        assert both["catalyst"]["earnings_date"] == "2026-09-15"
        assert both["catalyst"]["url"].startswith("https://nsearchives.nseindia.com/")
        assert both["catalyst"]["tag"] == {
            "event_type": "order",
            "review_priority": "high",
            "matched": ["order"],
            "source": "rules",
        }
        assert both["last_price"] is None
        assert len(everything.json()["data"]) == 4

    async def test_a_caller_who_is_not_the_sole_tenant_is_told_rather_than_shown_nothing(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sole_id, _ = await make_user(screener_session, "overlap-sole@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(sole_id))
        await _a_morning(screener_session, sole_id)
        _, other_public_id = await make_user(screener_session, "overlap-guest@example.com")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/overlap"), headers=bearer(other_public_id))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["strategies_read"] is False
        assert body["data"] == []
