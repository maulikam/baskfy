"""`GET /overlap` — the day's candidates across the sleeves, from what was stored.

Read-only over stored rows, so the assertions are about **which** rows and **whose words**:
each strategy at its own latest session, the count as arithmetic over those, the swing feed's
link and earnings date on loan, and a caller who is not the sole tenant told so rather than
shown an empty morning.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from decimal import Decimal

import pytest
from api_helpers import api_settings, bearer, make_user, problem, running_app, url
from redis.asyncio import Redis
from screener_helpers import requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import overlap_scan
from baskfy_api.curated_tenant import SOLE_TENANT_REFUSED
from baskfy_api.overlap import REVIEW_WANTED_KEY, WANTED_KEY, Strategy, best_catalyst_for, overlap
from baskfy_api.problems import STATUS_FOR, ProblemType
from baskfy_api.settings import Settings
from baskfy_core.candidate_review import review_key, review_state, shown
from baskfy_core.catalyst_tags import cache_key
from baskfy_core.models import (
    CandidateReviewLabel,
    CatalystTagCorrection,
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


class TestTheFilingWorthOpening:
    """The row shows the material filing, not merely the newest (25 Sep 2026, the box)."""

    async def test_a_material_filing_beats_a_newer_routine_notice(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-best@example.com")
        ids = await _a_morning(screener_session, user_id)
        # Two newer routine notices on BOTH, above the order win the fixture already stores.
        for hours, headline in (
            (1, "Copy of Newspaper Publication — BOTH has informed"),
            (2, "Analysts/Institutional Investor Meet/Con. Call Updates — BOTH"),
        ):
            screener_session.add(
                SwCatalyst(
                    user_id=user_id,
                    instrument_id=ids["BOTH"],
                    headline=headline,
                    published_at=dt.datetime(2026, 9, 12, hours, 0, tzinfo=IST),
                    url=f"https://nsearchives.nseindia.com/corporate/BOTH_{hours}.pdf",
                    source="NSE_ANNOUNCEMENT",
                )
            )
        await screener_session.flush()

        view = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        both = view.rows[0]
        assert both.symbol == "BOTH"
        assert both.catalyst is not None
        assert both.catalyst.headline == "Press Release - BOTH wins a multi-year order"
        assert both.catalyst.earnings_date == dt.date(2026, 9, 15)
        assert both.catalyst_tag is not None and both.catalyst_tag.event_type == "order"

    async def test_outside_the_window_the_newest_filing_stands(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-window@example.com")
        ids = await _a_morning(screener_session, user_id)
        screener_session.add(
            SwCatalyst(
                user_id=user_id,
                instrument_id=ids["BOTH"],
                headline="Copy of Newspaper Publication — BOTH has informed",
                published_at=dt.datetime(2026, 9, 12, 9, 0, tzinfo=IST),
                url="https://nsearchives.nseindia.com/corporate/BOTH_np.pdf",
                source="NSE_ANNOUNCEMENT",
            )
        )
        await screener_session.flush()
        # Asked as of a day far past the window, nothing is "recent": the newest filing stands.
        far = await best_catalyst_for(
            screener_session,
            user_id=user_id,
            instrument_ids=[ids["BOTH"]],
            today=dt.date(2027, 1, 1),
        )
        assert far[ids["BOTH"]].headline == "Copy of Newspaper Publication — BOTH has informed"
        # Asked inside the window, the order win wins over the newer notice.
        near = await best_catalyst_for(
            screener_session,
            user_id=user_id,
            instrument_ids=[ids["BOTH"]],
            today=dt.date(2026, 9, 25),
        )
        assert near[ids["BOTH"]].headline == "Press Release - BOTH wins a multi-year order"

    async def test_a_miss_in_the_cache_asks_the_sidecar_for_that_headline(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-wanted@example.com")
        await _a_morning(screener_session, user_id)
        await screen_cache.delete(WANTED_KEY)
        try:
            await overlap(
                screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
            )
            pipe = screen_cache.pipeline()
            pipe.smembers(WANTED_KEY)
            [members] = await pipe.execute()
            wanted = {(m.decode() if isinstance(m, bytes) else str(m)) for m in members}
            assert "Press Release - BOTH wins a multi-year order" in wanted
            assert await screen_cache.ttl(WANTED_KEY) > 0
        finally:
            await screen_cache.delete(WANTED_KEY)


class TestTheOpinion:
    """The technicals in words plus the filing go to Laya; the answer is attention, gated."""

    async def test_a_miss_queues_the_row_state_in_words_and_a_sure_answer_is_shown(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-opinion@example.com")
        await _a_morning(screener_session, user_id)
        # The answers are content-addressed, so a key left behind is the same key next run.
        keys = [
            review_key(row.state())
            for row in (
                await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
            ).rows
        ]
        await screen_cache.delete(REVIEW_WANTED_KEY, *keys)
        try:
            first = await overlap(
                screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
            )
            both = first.rows[0]
            assert both.opinion is None
            state = review_state(both.facts(), both.catalyst.headline if both.catalyst else None)
            # The words the sidecar will be shown: the scans' numbers, said plainly, and the filing.
            assert state["setup"].startswith("Swing episodic pivot, gap day")
            assert "Three weeks tight, entry today" in state["setup"]
            assert state["filing"] == "Press Release - BOTH wins a multi-year order"
            pipe = screen_cache.pipeline()
            pipe.hgetall(REVIEW_WANTED_KEY)
            [queued] = await pipe.execute()
            queued_keys = {(k.decode() if isinstance(k, bytes) else str(k)) for k in queued}
            assert review_key(state) in queued_keys

            # The sidecar answers, sure on this one and unsure on FLAGCO's.
            await screen_cache.set(
                review_key(state), json.dumps({"choice": "look_first", "confidence": 0.82})
            )
            flagco = first.rows[1]
            flag_state = review_state(
                flagco.facts(), flagco.catalyst.headline if flagco.catalyst else None
            )
            await screen_cache.set(
                review_key(flag_state), json.dumps({"choice": "skip", "confidence": 0.34})
            )
            second = await overlap(
                screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
            )
        finally:
            await screen_cache.delete(REVIEW_WANTED_KEY, *keys)
        sure, unsure = second.rows[0].opinion, second.rows[1].opinion
        assert sure is not None and (sure.label, sure.confidence) == ("look_first", 0.82)
        assert unsure is not None and unsure.confidence == 0.34
        assert shown(sure) and not shown(unsure)


class TestTheModelColumn:
    """The sidecar's cached answer, resolved against the rules, on the wire."""

    async def test_a_sure_model_answer_is_served_with_its_number_and_the_rules_kept_on_disagreement(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-laya@example.com")
        await _a_morning(screener_session, user_id)
        headline = "Press Release - BOTH wins a multi-year order"
        # What `infra/laya/laya_loop.py` writes — measured shape, a deliberately different answer.
        await screen_cache.set(
            cache_key(headline),
            json.dumps(
                {"choice": "corporate_action", "confidence": 0.91, "model": "laya-rl-agent"}
            ),
        )
        try:
            view = await overlap(
                screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
            )
        finally:
            await screen_cache.delete(cache_key(headline))
        tag = view.rows[0].catalyst_tag
        assert tag is not None
        assert (tag.source, tag.event_type, tag.confidence, tag.disagrees_with) == (
            "laya",
            "corporate_action",
            0.91,
            "rules:order",
        )

    async def test_an_unsure_answer_a_miss_and_no_cache_all_mean_the_rules(
        self, screener_session: AsyncSession, screen_cache: Redis
    ) -> None:
        user_id, _ = await make_user(screener_session, "overlap-laya2@example.com")
        await _a_morning(screener_session, user_id)
        headline = "Press Release - BOTH wins a multi-year order"
        await screen_cache.set(
            cache_key(headline), json.dumps({"choice": "routine", "confidence": 0.31})
        )
        try:
            unsure = await overlap(
                screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
            )
        finally:
            await screen_cache.delete(cache_key(headline))
        miss = await overlap(
            screener_session, user_id=user_id, strategies_user_id=user_id, cache=screen_cache
        )
        none = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        for view, disagreement in ((unsure, "laya:routine"), (miss, None), (none, None)):
            tag = view.rows[0].catalyst_tag
            assert tag is not None
            assert (tag.source, tag.event_type, tag.confidence) == ("rules", "order", None)
            assert tag.disagrees_with == disagreement


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
        # No sidecar pass in this database: the page can say so rather than show a blank.
        assert body["laya"] == {"last_pass_at": None, "answered": 0, "shown": 0, "of": 1}
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
            "confidence": None,
            "disagrees_with": None,
            "corrected": False,
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


HEADLINE = "Press Release - BOTH wins a multi-year order"
TAGS = url("/overlap/tags")


async def _correction_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(CatalystTagCorrection)
                .where(CatalystTagCorrection.user_id == user_id)
            )
        ).scalar_one()
    )


class TestTheCorrection:
    """A person's word wins on the page, is one row per headline, and exports as the set."""

    @pytest.fixture
    def settings(self, seeded_url: str) -> Settings:
        return api_settings(seeded_url)

    async def test_a_correction_wins_over_a_sure_model_and_names_what_it_overruled(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        screen_cache: Redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-correct@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await _a_morning(screener_session, user_id)
        # Laya, sure and wrong (measured shape); the rules say order; the person says order.
        await screen_cache.set(
            cache_key(HEADLINE), json.dumps({"choice": "corporate_action", "confidence": 0.91})
        )
        try:
            async with running_app(settings, screener_session) as client:
                before = await client.get(url("/overlap"), headers=bearer(public_id))
                put = await client.put(
                    TAGS,
                    headers=bearer(public_id),
                    json={"headline": HEADLINE, "event_type": "order", "note": None},
                )
                after = await client.get(url("/overlap"), headers=bearer(public_id))
        finally:
            await screen_cache.delete(cache_key(HEADLINE))

        assert before.json()["data"][0]["catalyst"]["tag"]["source"] == "laya"
        assert put.status_code == 200, put.text
        corrected: dict[str, object] = {
            "event_type": "order",
            "review_priority": "high",
            "matched": [],
            "source": "corrected",
            "confidence": None,
            "disagrees_with": "laya:corporate_action",
            "corrected": True,
        }
        assert put.json() == corrected
        assert after.json()["data"][0]["catalyst"]["tag"] == corrected
        # What the two readers said at correction time is the row's training signal.
        row = (
            await screener_session.scalars(
                select(CatalystTagCorrection).where(CatalystTagCorrection.user_id == user_id)
            )
        ).one()
        assert (row.headline_key, row.headline, row.event_type) == (
            cache_key(HEADLINE),
            HEADLINE,
            "order",
        )
        assert (row.rules_event_type, row.laya_event_type, row.laya_confidence) == (
            "order",
            "corporate_action",
            Decimal("0.9100"),
        )

    async def test_a_second_correction_updates_the_one_row(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-again@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await _a_morning(screener_session, user_id)

        async with running_app(settings, screener_session) as client:
            first = await client.put(
                TAGS,
                headers=bearer(public_id),
                json={"headline": HEADLINE, "event_type": "routine", "note": "not material"},
            )
            # The same headline, differently spaced and cased, is the same key.
            second = await client.put(
                TAGS,
                headers=bearer(public_id),
                json={"headline": f"  {HEADLINE.upper()} ", "event_type": "governance"},
            )
            page = await client.get(url("/overlap"), headers=bearer(public_id))

        assert first.status_code == 200, first.text
        assert first.json()["event_type"] == "routine"
        assert first.json()["disagrees_with"] == "rules:order"
        assert second.status_code == 200, second.text
        assert second.json()["event_type"] == "governance"
        assert await _correction_count(screener_session, user_id) == 1
        tag = page.json()["data"][0]["catalyst"]["tag"]
        assert (tag["event_type"], tag["source"], tag["review_priority"]) == (
            "governance",
            "corrected",
            "medium",
        )
        row = (
            await screener_session.scalars(
                select(CatalystTagCorrection).where(CatalystTagCorrection.user_id == user_id)
            )
        ).one()
        assert row.note is None, "the second correction carried no note, so the row has none"

    async def test_removing_the_correction_restores_the_readers_word(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-clear@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await _a_morning(screener_session, user_id)

        async with running_app(settings, screener_session) as client:
            await client.put(
                TAGS, headers=bearer(public_id), json={"headline": HEADLINE, "event_type": "other"}
            )
            removed = await client.delete(
                TAGS, headers=bearer(public_id), params={"headline": HEADLINE}
            )
            again = await client.delete(
                TAGS, headers=bearer(public_id), params={"headline": HEADLINE}
            )
            page = await client.get(url("/overlap"), headers=bearer(public_id))

        assert removed.status_code == 204, removed.text
        assert again.status_code == 404, again.text
        assert await _correction_count(screener_session, user_id) == 0
        tag = page.json()["data"][0]["catalyst"]["tag"]
        assert (tag["source"], tag["event_type"], tag["corrected"], tag["disagrees_with"]) == (
            "rules",
            "order",
            False,
            None,
        )

    async def test_the_export_is_one_ndjson_line_per_correction_oldest_first(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-export@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await _a_morning(screener_session, user_id)

        async with running_app(settings, screener_session) as client:
            empty = await client.get(url("/overlap/tags/export"), headers=bearer(public_id))
            await client.put(
                TAGS,
                headers=bearer(public_id),
                json={"headline": HEADLINE, "event_type": "order", "note": "a real order win"},
            )
            await client.put(
                TAGS,
                headers=bearer(public_id),
                json={"headline": "Closure of Trading Window", "event_type": "routine"},
            )
            export = await client.get(url("/overlap/tags/export"), headers=bearer(public_id))

        assert empty.status_code == 200 and empty.text == ""
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in export.text.splitlines()]
        assert len(lines) == 2
        assert export.text.endswith("\n")
        first, second = lines
        assert set(first) == {
            "headline",
            "event_type",
            "rules_event_type",
            "laya_event_type",
            "laya_confidence",
            "note",
            "corrected_at",
        }
        assert (first["headline"], first["event_type"], first["rules_event_type"]) == (
            HEADLINE,
            "order",
            "order",
        )
        assert (first["laya_event_type"], first["laya_confidence"]) == (None, None)
        assert first["note"] == "a real order win"
        assert dt.datetime.fromisoformat(first["corrected_at"]).tzinfo is not None
        assert (second["headline"], second["event_type"], second["note"]) == (
            "Closure of Trading Window",
            "routine",
            None,
        )

    async def test_a_caller_who_is_not_the_sole_tenant_is_refused_the_writes_and_the_export(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same refusal `/swing`'s writes give: `scoped_sole_user_id` raises a not-found
        problem, so a second account learns nothing about whose labels these are."""
        sole_id, _ = await make_user(screener_session, "overlap-labels@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(sole_id))
        _, guest = await make_user(screener_session, "overlap-stranger@example.com")
        refused = STATUS_FOR[ProblemType.NOT_FOUND]

        async with running_app(settings, screener_session) as client:
            put = await client.put(
                TAGS, headers=bearer(guest), json={"headline": HEADLINE, "event_type": "order"}
            )
            delete = await client.delete(TAGS, headers=bearer(guest), params={"headline": HEADLINE})
            export = await client.get(url("/overlap/tags/export"), headers=bearer(guest))
            anonymous = await client.put(TAGS, json={"headline": HEADLINE, "event_type": "order"})

        assert anonymous.status_code == 401
        for response in (put, delete, export):
            assert response.status_code == refused, response.text
            assert problem(response)["reason"] == SOLE_TENANT_REFUSED
        assert await _correction_count(screener_session, sole_id) == 0

    async def test_a_word_outside_the_vocabulary_or_a_blank_headline_is_refused(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-bad@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))

        async with running_app(settings, screener_session) as client:
            merger = await client.put(
                TAGS, headers=bearer(public_id), json={"headline": HEADLINE, "event_type": "merger"}
            )
            blank = await client.put(
                TAGS, headers=bearer(public_id), json={"headline": "   ", "event_type": "order"}
            )

        assert merger.status_code == 400, merger.text
        assert blank.status_code == 400, blank.text
        assert await _correction_count(screener_session, user_id) == 0


REVIEWS = url("/overlap/reviews")


async def _label_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(CandidateReviewLabel)
                .where(CandidateReviewLabel.user_id == user_id)
            )
        ).scalar_one()
    )


class TestTheLabel:
    """A person's word on a row's opinion wins on the page, is one row per state, and exports
    as the set the row question's fine-tune trains on."""

    @pytest.fixture
    def settings(self, seeded_url: str) -> Settings:
        return api_settings(seeded_url)

    async def test_a_label_wins_over_the_model_is_always_shown_and_records_what_it_overruled(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        screen_cache: Redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "labelwins@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        ids = await _a_morning(screener_session, user_id)
        view = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        both = view.rows[0]
        state = both.state()
        # Laya, unsure (measured shape): the page says "not sure". The person says skip.
        await screen_cache.set(
            review_key(state), json.dumps({"choice": "look_first", "confidence": 0.41})
        )
        try:
            async with running_app(settings, screener_session) as client:
                before = await client.get(url("/overlap"), headers=bearer(public_id))
                put = await client.put(
                    REVIEWS,
                    headers=bearer(public_id),
                    json={"instrument_id": ids["BOTH"], "label": "skip", "note": "stale gap"},
                )
                after = await client.get(url("/overlap"), headers=bearer(public_id))
        finally:
            await screen_cache.delete(review_key(state))

        assert before.json()["data"][0]["opinion"] == {
            "label": "look_first",
            "confidence": 0.41,
            "source": "laya",
            "shown": False,
            "floor": 0.6,
            "labelled": False,
        }
        assert put.status_code == 200, put.text
        labelled: dict[str, object] = {
            "label": "skip",
            "confidence": 0.0,
            "source": "labelled",
            "shown": True,
            "floor": 0.6,
            "labelled": True,
        }
        assert put.json() == labelled
        assert after.json()["data"][0]["opinion"] == labelled
        # The other row is untouched: no label, and no cached answer, so no opinion.
        assert after.json()["data"][1]["opinion"] is None
        # The state stored is exactly what the page showed the model, keyed the same way.
        row = (
            await screener_session.scalars(
                select(CandidateReviewLabel).where(CandidateReviewLabel.user_id == user_id)
            )
        ).one()
        assert (row.review_key, row.state, row.instrument_id, row.symbol) == (
            review_key(state),
            state,
            ids["BOTH"],
            "BOTH",
        )
        assert (row.label, row.note, row.laya_label, row.laya_confidence) == (
            "skip",
            "stale gap",
            "look_first",
            Decimal("0.4100"),
        )

    async def test_a_second_label_updates_the_one_row(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "labelagain@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        ids = await _a_morning(screener_session, user_id)

        async with running_app(settings, screener_session) as client:
            first = await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["BOTH"], "label": "worth_a_look", "note": "maybe"},
            )
            second = await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["BOTH"], "label": "look_first"},
            )
            page = await client.get(url("/overlap"), headers=bearer(public_id))

        assert first.status_code == 200, first.text
        assert first.json()["label"] == "worth_a_look"
        assert second.status_code == 200, second.text
        assert second.json()["label"] == "look_first"
        assert await _label_count(screener_session, user_id) == 1
        opinion = page.json()["data"][0]["opinion"]
        assert (opinion["label"], opinion["source"], opinion["shown"]) == (
            "look_first",
            "labelled",
            True,
        )
        row = (
            await screener_session.scalars(
                select(CandidateReviewLabel).where(CandidateReviewLabel.user_id == user_id)
            )
        ).one()
        assert row.note is None, "the second label carried no note, so the row has none"
        # No cached answer in this database: the model's side of the signal is honestly null.
        assert (row.laya_label, row.laya_confidence) == (None, None)

    async def test_removing_the_label_restores_the_models_opinion(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        screen_cache: Redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "labelclear@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        ids = await _a_morning(screener_session, user_id)
        view = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        key = review_key(view.rows[0].state())
        await screen_cache.set(key, json.dumps({"choice": "look_first", "confidence": 0.82}))
        try:
            async with running_app(settings, screener_session) as client:
                await client.put(
                    REVIEWS,
                    headers=bearer(public_id),
                    json={"instrument_id": ids["BOTH"], "label": "skip"},
                )
                removed = await client.delete(
                    REVIEWS, headers=bearer(public_id), params={"instrument_id": ids["BOTH"]}
                )
                again = await client.delete(
                    REVIEWS, headers=bearer(public_id), params={"instrument_id": ids["BOTH"]}
                )
                page = await client.get(url("/overlap"), headers=bearer(public_id))
        finally:
            await screen_cache.delete(key)

        assert removed.status_code == 204, removed.text
        assert again.status_code == 404, again.text
        assert await _label_count(screener_session, user_id) == 0
        opinion = page.json()["data"][0]["opinion"]
        assert (opinion["label"], opinion["source"], opinion["labelled"], opinion["shown"]) == (
            "look_first",
            "laya",
            False,
            True,
        )

    async def test_the_export_is_one_ndjson_line_per_label_oldest_first(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "labelexport@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        ids = await _a_morning(screener_session, user_id)
        view = await overlap(screener_session, user_id=user_id, strategies_user_id=user_id)
        states = {row.symbol: row.state() for row in view.rows}

        async with running_app(settings, screener_session) as client:
            empty = await client.get(url("/overlap/reviews/export"), headers=bearer(public_id))
            await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["BOTH"], "label": "look_first", "note": "order win"},
            )
            await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["FLAGCO"], "label": "skip"},
            )
            export = await client.get(url("/overlap/reviews/export"), headers=bearer(public_id))

        assert empty.status_code == 200 and empty.text == ""
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in export.text.splitlines()]
        assert len(lines) == 2
        assert export.text.endswith("\n")
        first, second = lines
        assert set(first) == {
            "symbol",
            "state",
            "label",
            "laya_label",
            "laya_confidence",
            "note",
            "labelled_at",
        }
        assert (first["symbol"], first["state"], first["label"]) == (
            "BOTH",
            states["BOTH"],
            "look_first",
        )
        assert (first["laya_label"], first["laya_confidence"]) == (None, None)
        assert first["note"] == "order win"
        assert dt.datetime.fromisoformat(first["labelled_at"]).tzinfo is not None
        assert (second["symbol"], second["state"], second["label"], second["note"]) == (
            "FLAGCO",
            states["FLAGCO"],
            "skip",
            None,
        )
        assert second["state"]["filing"] == "No filing on record."

    async def test_a_caller_who_is_not_the_sole_tenant_is_refused_the_writes_and_the_export(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sole_id, _ = await make_user(screener_session, "labelowner@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(sole_id))
        ids = await _a_morning(screener_session, sole_id)
        _, guest = await make_user(screener_session, "labelguest@example.com")
        refused = STATUS_FOR[ProblemType.NOT_FOUND]

        async with running_app(settings, screener_session) as client:
            put = await client.put(
                REVIEWS,
                headers=bearer(guest),
                json={"instrument_id": ids["BOTH"], "label": "skip"},
            )
            delete = await client.delete(
                REVIEWS, headers=bearer(guest), params={"instrument_id": ids["BOTH"]}
            )
            export = await client.get(url("/overlap/reviews/export"), headers=bearer(guest))
            anonymous = await client.put(
                REVIEWS, json={"instrument_id": ids["BOTH"], "label": "skip"}
            )

        assert anonymous.status_code == 401
        for response in (put, delete, export):
            assert response.status_code == refused, response.text
            assert problem(response)["reason"] == SOLE_TENANT_REFUSED
        assert await _label_count(screener_session, sole_id) == 0

    async def test_a_name_not_on_the_list_or_a_word_outside_the_vocabulary_is_refused(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """There is no state to label for a name the scans did not write today, so the server
        cannot key a label for it — and will not take one from the caller."""
        user_id, public_id = await make_user(screener_session, "labelbad@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        ids = await _a_morning(screener_session, user_id)
        absent = await _instrument(screener_session, "NOSCAN")

        async with running_app(settings, screener_session) as client:
            unknown = await client.put(
                REVIEWS, headers=bearer(public_id), json={"instrument_id": absent, "label": "skip"}
            )
            unknown_delete = await client.delete(
                REVIEWS, headers=bearer(public_id), params={"instrument_id": absent}
            )
            buy = await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["BOTH"], "label": "buy"},
            )
            # A row the scans wrote but no plan builder could take is still on the list.
            quiet = await client.put(
                REVIEWS,
                headers=bearer(public_id),
                json={"instrument_id": ids["QUIET"], "label": "skip"},
            )

        assert unknown.status_code == 404, unknown.text
        assert unknown_delete.status_code == 404, unknown_delete.text
        assert buy.status_code == 400, buy.text
        assert quiet.status_code == 200, quiet.text
        assert await _label_count(screener_session, user_id) == 1


SCAN = url("/overlap/catalyst-scan")


class _RecordingQueue:
    """Stands in for the Celery producer. Records what would have been published."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


class TestTheFilingsScan:
    """The "Scan filings with Laya" button: one queued read of the listed names' filings."""

    @pytest.fixture
    def settings(self, seeded_url: str) -> Settings:
        return api_settings(seeded_url)

    @pytest.fixture(autouse=True)
    def _outside_the_morning_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The route reads the wall clock; these tests are about the queue, not the time."""
        monkeypatch.setattr(overlap_scan, "in_busy_window", lambda _now: False)

    async def test_it_queues_one_scan_reports_it_and_refuses_a_second_while_it_runs(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        screen_cache: Redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "overlap-scan@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        queue = _RecordingQueue()
        keys = (overlap_scan.status_key(user_id), overlap_scan.lock_key(user_id))
        await screen_cache.delete(*keys)
        try:
            async with running_app(settings, screener_session, task_queue=queue) as client:
                idle = await client.get(SCAN, headers=bearer(public_id))
                first = await client.post(SCAN, headers=bearer(public_id), params={"scope": "all"})
                second = await client.post(SCAN, headers=bearer(public_id))
                progress = await client.get(SCAN, headers=bearer(public_id))
        finally:
            await screen_cache.delete(*keys)

        assert idle.status_code == 200, idle.text
        assert idle.json()["state"] == "idle"
        assert first.status_code == 202, first.text
        assert first.json()["state"] == "queued"
        assert first.json()["scope"] == "all"
        assert queue.sent == [(overlap_scan.SCAN_TASK, [user_id, "all"])]
        assert second.status_code == STATUS_FOR[ProblemType.SCAN_IN_FLIGHT], second.text
        assert progress.json()["state"] == "queued"

    async def test_a_caller_who_is_not_the_sole_tenant_cannot_start_or_read_a_scan(
        self,
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sole_id, _ = await make_user(screener_session, "scanowner@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(sole_id))
        _, guest = await make_user(screener_session, "scanguest@example.com")
        queue = _RecordingQueue()

        async with running_app(settings, screener_session, task_queue=queue) as client:
            post = await client.post(SCAN, headers=bearer(guest))
            status = await client.get(SCAN, headers=bearer(guest))
            anonymous = await client.post(SCAN)

        assert anonymous.status_code == 401
        for response in (post, status):
            assert response.status_code == STATUS_FOR[ProblemType.NOT_FOUND], response.text
            assert problem(response)["reason"] == SOLE_TENANT_REFUSED
        assert queue.sent == []
