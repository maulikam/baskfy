"""SW11B's worker half: the 09:17 catalyst feed, against a real database.

`docs/swing/STANDING-ANSWERS.md` A3, asserted clause by clause:

* **watchlist + EP-candidate symbols only** — the provider is handed the WATCHING names and the
  last session's EP rows, and nothing else (a dismissed name, a FLAG candidate, a name whose
  watch row TRIGGERED are all outside the feed);
* **store headline + timestamp + filing URL** — one `sw_catalyst` row per filing, idempotent on
  the URL: a second morning rewrites the same rows;
* **auto-fill `sw_watch.catalyst`** — with the newest headline, only while empty; a typed note
  survives every run;
* **an earnings-date flag from the results calendar** — the nearest result meeting on or
  after the session, refreshed each run, cleared when the calendar names none;
* **fail soft** — one refused symbol is a count; every symbol refused is a SUCCEEDED step with
  zero rows and the error in the note. Nothing raises into the morning.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import pytest
import sqlalchemy as sa
from celery.schedules import crontab
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, SwCatalyst, SwConfig, SwSetupDaily, SwWatch, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.errors import UpstreamUnavailable
from baskfy_providers.nse import IST
from baskfy_providers.records import CatalystRecord, EarningsDateRecord
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks import swing_catalyst
from baskfy_worker.tasks.celery_tasks import swing_catalyst_task
from baskfy_worker.tasks.swing_catalyst import (
    feed_symbols,
    nearest_result,
    run_swing_catalyst,
)

pytestmark = requires_db

SESSION = dt.date(2026, 8, 19)
CALENDAR = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol="


async def _last_session(session: AsyncSession, before: dt.date) -> dt.date:
    row = await session.execute(
        sa.select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date < before,
        )
        .order_by(TradingDay.date.desc())
        .limit(1)
    )
    return row.scalar_one()


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="sw11b-user", email="sw11b@example.com")
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _watch(  # noqa: PLR0913 - one keyword per column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    setup: str = "FLAG",
    state: str = "WATCHING",
    catalyst: str | None = None,
    source: str = "DETECTOR",
) -> int:
    row = SwWatch(
        user_id=user_id,
        instrument_id=instrument_id,
        setup=setup,
        source=source,
        added_on=SESSION,
        trigger=Decimal("110.00"),
        stop_ref=Decimal("104.00"),
        catalyst=catalyst,
        state=state,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _ep_candidate(
    session: AsyncSession, *, user_id: int, instrument_id: int, on: dt.date, setup: str = "EP"
) -> None:
    session.add(
        SwSetupDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            setup=setup,
            status="SETTING_UP",
            score=Decimal("80.00"),
            close=Decimal("120.00"),
            adj_factor=Decimal("1"),
            locked_upper_circuit=False,
            listed_within_2y=False,
        )
    )
    await session.flush()


def _announcement(
    symbol: str, stamp: dt.datetime | None, url: str, headline: str
) -> CatalystRecord:
    return CatalystRecord(symbol=symbol, headline=headline, published_at=stamp, url=url)


def _result(symbol: str, on: dt.date, purpose: str = "Financial Results") -> EarningsDateRecord:
    return EarningsDateRecord(
        symbol=symbol, event_date=on, purpose=purpose, url=f"{CALENDAR}{symbol}"
    )


class FakeNSE:
    """Answers per symbol from a script; counts what it was asked; refuses on demand."""

    def __init__(
        self,
        announcements: dict[str, list[CatalystRecord]] | None = None,
        calendar: dict[str, list[EarningsDateRecord]] | None = None,
        *,
        refuse: Sequence[str] = (),
    ) -> None:
        self._announcements = announcements or {}
        self._calendar = calendar or {}
        self._refuse = set(refuse)
        self.asked: list[str] = []

    def announcements(self, symbols: Sequence[str], *, on: dt.date) -> list[CatalystRecord]:
        del on
        out: list[CatalystRecord] = []
        for symbol in symbols:
            self.asked.append(symbol)
            if symbol in self._refuse:
                raise UpstreamUnavailable(f"GET {symbol} returned 503", provider="nse")
            out.extend(self._announcements.get(symbol, []))
        return out

    def results_calendar(self, symbols: Sequence[str], *, on: dt.date) -> list[EarningsDateRecord]:
        del on
        out: list[EarningsDateRecord] = []
        for symbol in symbols:
            if symbol in self._refuse:
                raise UpstreamUnavailable(f"GET {symbol} returned 503", provider="nse")
            out.extend(self._calendar.get(symbol, []))
        return out


async def _run(session: AsyncSession, user_id: int, provider: FakeNSE | None) -> StepOutcome:
    outcome = StepOutcome()
    report = await run_swing_catalyst(
        session, outcome, SESSION, user_id=user_id, provider=provider, now=None
    )
    assert report.as_detail() == outcome.detail
    return outcome


async def _catalysts(session: AsyncSession, user_id: int) -> list[SwCatalyst]:
    rows = await session.execute(
        sa.select(SwCatalyst).where(SwCatalyst.user_id == user_id).order_by(SwCatalyst.id)
    )
    return list(rows.scalars())


async def _watch_row(session: AsyncSession, watch_id: int) -> SwWatch:
    await session.flush()
    row = await session.get(SwWatch, watch_id)
    assert row is not None
    await session.refresh(row)
    return row


class TestTheSymbolsTheFeedServes:
    async def test_watching_rows_and_the_last_sessions_ep_candidates_and_nothing_else(
        self, session: AsyncSession
    ) -> None:
        """A3: "watchlist + EP-candidate symbols only"."""
        user_id = await _user(session)
        last = await _last_session(session, SESSION)
        watched = await make_instrument(session, "WATCHED")
        dismissed = await make_instrument(session, "DISMISSED")
        triggered = await make_instrument(session, "TRIGGERED")
        ep_candidate = await make_instrument(session, "EPCAND")
        flag_candidate = await make_instrument(session, "FLAGCAND")
        stale_ep = await make_instrument(session, "STALEEP")
        await _watch(session, user_id=user_id, instrument_id=watched)
        await _watch(session, user_id=user_id, instrument_id=dismissed, state="DISMISSED")
        await _watch(session, user_id=user_id, instrument_id=triggered, state="TRIGGERED")
        await _ep_candidate(session, user_id=user_id, instrument_id=ep_candidate, on=last)
        await _ep_candidate(
            session, user_id=user_id, instrument_id=flag_candidate, on=last, setup="FLAG"
        )
        await _ep_candidate(
            session, user_id=user_id, instrument_id=stale_ep, on=last - dt.timedelta(days=7)
        )

        symbols = await feed_symbols(session, user_id=user_id, session_date=SESSION)

        assert symbols == {"EPCAND": ep_candidate, "WATCHED": watched}

    async def test_the_provider_is_asked_exactly_those_symbols_one_at_a_time(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        last = await _last_session(session, SESSION)
        watched = await make_instrument(session, "WATCHED")
        ep_candidate = await make_instrument(session, "EPCAND")
        await _watch(session, user_id=user_id, instrument_id=watched)
        await _ep_candidate(session, user_id=user_id, instrument_id=ep_candidate, on=last)
        provider = FakeNSE()

        outcome = await _run(session, user_id, provider)

        assert provider.asked == ["EPCAND", "WATCHED"]
        assert outcome.status is StepStatus.SUCCEEDED
        assert outcome.detail["symbols"] == 2
        assert outcome.detail["rows_written"] == 0

    async def test_nothing_watched_is_a_noted_no_op_not_a_provider_call(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        provider = FakeNSE()
        outcome = await _run(session, user_id, provider)
        assert provider.asked == []
        assert outcome.status is StepStatus.SUCCEEDED
        assert "nothing watched" in str(outcome.detail["skipped_reason"])


class TestAnnouncementsAreStoredAsLinks:
    async def test_a_headline_a_stamp_and_a_url_per_filing_idempotent_on_the_url(
        self, session: AsyncSession
    ) -> None:
        """Rerunning the morning rewrites the same rows: the URL is the identity of a filing."""
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        await _watch(session, user_id=user_id, instrument_id=watched)
        newest = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        older = dt.datetime(2026, 8, 17, 9, 0, tzinfo=IST)
        provider = FakeNSE(
            {
                "WATCHED": [
                    _announcement("WATCHED", newest, "https://a/1.pdf?x=1", "Press Release - deal"),
                    _announcement("WATCHED", older, "https://a/2.pdf", "Updates - capex"),
                ]
            }
        )

        await _run(session, user_id, provider)
        first = await _catalysts(session, user_id)
        await _run(session, user_id, provider)
        second = await _catalysts(session, user_id)

        assert [(r.url, r.headline, r.published_at, r.source) for r in first] == [
            ("https://a/1.pdf?x=1", "Press Release - deal", newest, "NSE_ANNOUNCEMENT"),
            ("https://a/2.pdf", "Updates - capex", older, "NSE_ANNOUNCEMENT"),
        ]
        assert [(r.id, r.url) for r in second] == [(r.id, r.url) for r in first]

    async def test_a_filing_nse_republishes_under_the_same_url_is_one_row(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        await _watch(session, user_id=user_id, instrument_id=watched)
        stamp = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        provider = FakeNSE(
            {
                "WATCHED": [
                    _announcement("WATCHED", stamp, "https://a/1.pdf", "Updates"),
                    _announcement("WATCHED", stamp, "https://a/1.pdf", "Updates (re-listed)"),
                ]
            }
        )
        await _run(session, user_id, provider)
        assert len(await _catalysts(session, user_id)) == 1

    async def test_the_step_counts_what_it_wrote(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        await _watch(session, user_id=user_id, instrument_id=watched)
        stamp = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        provider = FakeNSE(
            {"WATCHED": [_announcement("WATCHED", stamp, "https://a/1.pdf", "Updates")]},
            {"WATCHED": [_result("WATCHED", dt.date(2026, 10, 15))]},
        )
        outcome = await _run(session, user_id, provider)
        assert outcome.rows_in == 1
        assert outcome.rows_out == 2
        assert outcome.detail["announcements"] == 1
        assert outcome.detail["earnings_dates"] == 1


class TestTheWatchRowIsAutoFilled:
    async def test_an_empty_catalyst_gets_the_newest_headline(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(session, user_id=user_id, instrument_id=watched)
        newest = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        older = dt.datetime(2026, 8, 17, 9, 0, tzinfo=IST)
        provider = FakeNSE(
            {
                "WATCHED": [
                    _announcement("WATCHED", newest, "https://a/1.pdf", "Press Release - deal"),
                    _announcement("WATCHED", older, "https://a/2.pdf", "Updates - capex"),
                ]
            }
        )
        outcome = await _run(session, user_id, provider)
        row = await _watch_row(session, watch_id)
        assert row.catalyst == "Press Release - deal"
        assert outcome.detail["catalysts_filled"] == 1

    async def test_a_typed_catalyst_is_never_overwritten(self, session: AsyncSession) -> None:
        """The person's note is the person's. The feed fills blanks and nothing else."""
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(
            session,
            user_id=user_id,
            instrument_id=watched,
            catalyst="Q2 result, order book up",
            source="MANUAL",
        )
        stamp = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        provider = FakeNSE(
            {"WATCHED": [_announcement("WATCHED", stamp, "https://a/1.pdf", "Press Release")]}
        )
        outcome = await _run(session, user_id, provider)
        row = await _watch_row(session, watch_id)
        assert row.catalyst == "Q2 result, order book up"
        assert outcome.detail["catalysts_filled"] == 0
        # The link is still stored — the page links out beside the typed note.
        assert [r.url for r in await _catalysts(session, user_id)] == ["https://a/1.pdf"]

    async def test_a_blank_catalyst_counts_as_empty(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(session, user_id=user_id, instrument_id=watched, catalyst="")
        stamp = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        provider = FakeNSE(
            {"WATCHED": [_announcement("WATCHED", stamp, "https://a/1.pdf", "Press Release")]}
        )
        await _run(session, user_id, provider)
        assert (await _watch_row(session, watch_id)).catalyst == "Press Release"


class TestTheEarningsFlag:
    async def test_the_nearest_result_on_or_after_the_session_is_the_flag(self) -> None:
        records = [
            _result("X", dt.date(2026, 4, 16)),
            _result("X", dt.date(2026, 10, 15)),
            _result("X", dt.date(2026, 8, 19)),
        ]
        picked = nearest_result(records, on_or_after=SESSION)
        assert picked is not None
        assert picked.event_date == SESSION
        assert nearest_result(records[:1], on_or_after=SESSION) is None

    async def test_the_flag_is_written_to_the_watch_row_and_a_calendar_row_links_out(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(session, user_id=user_id, instrument_id=watched)
        provider = FakeNSE(
            calendar={
                "WATCHED": [
                    _result("WATCHED", dt.date(2026, 4, 16), "Financial Results/Dividend"),
                    _result("WATCHED", dt.date(2026, 10, 15)),
                ]
            }
        )
        outcome = await _run(session, user_id, provider)
        row = await _watch_row(session, watch_id)
        assert row.earnings_date == dt.date(2026, 10, 15)
        assert outcome.detail["earnings_flagged"] == 1
        rows = await _catalysts(session, user_id)
        assert [(r.source, r.earnings_date, r.headline, r.url) for r in rows] == [
            ("NSE_EVENT_CALENDAR", dt.date(2026, 10, 15), "Financial Results", f"{CALENDAR}WATCHED")
        ]

    async def test_a_flag_is_cleared_when_the_calendar_no_longer_names_a_result(
        self, session: AsyncSession
    ) -> None:
        """A flag is a reading of the calendar, not a note; it goes when the date does."""
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(session, user_id=user_id, instrument_id=watched)
        with_result = FakeNSE(calendar={"WATCHED": [_result("WATCHED", dt.date(2026, 10, 15))]})
        await _run(session, user_id, with_result)
        assert (await _watch_row(session, watch_id)).earnings_date == dt.date(2026, 10, 15)

        await _run(session, user_id, FakeNSE())
        assert (await _watch_row(session, watch_id)).earnings_date is None
        assert await _catalysts(session, user_id) == []


class TestFailSoft:
    async def test_a_provider_outage_is_a_succeeded_step_with_a_note_and_zero_rows(
        self, session: AsyncSession
    ) -> None:
        """A3: "fail soft (empty catalyst allowed, a crashed pre-open scan not)"."""
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        watch_id = await _watch(session, user_id=user_id, instrument_id=watched)
        provider = FakeNSE(refuse=["WATCHED"])

        outcome = await _run(session, user_id, provider)

        assert outcome.status is StepStatus.SUCCEEDED
        assert outcome.detail["rows_written"] == 0
        assert outcome.detail["failed"] == ["WATCHED"]
        assert "503" in str(outcome.detail["provider_error"])
        assert await _catalysts(session, user_id) == []
        assert (await _watch_row(session, watch_id)).catalyst is None

    async def test_one_refused_symbol_does_not_cost_the_others_their_links(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        good = await make_instrument(session, "GOOD")
        bad = await make_instrument(session, "BAD")
        await _watch(session, user_id=user_id, instrument_id=good)
        await _watch(session, user_id=user_id, instrument_id=bad)
        stamp = dt.datetime(2026, 8, 18, 18, 30, tzinfo=IST)
        provider = FakeNSE(
            {"GOOD": [_announcement("GOOD", stamp, "https://a/g.pdf", "Updates")]},
            refuse=["BAD"],
        )
        outcome = await _run(session, user_id, provider)
        assert outcome.status is StepStatus.SUCCEEDED
        assert outcome.detail["failed"] == ["BAD"]
        assert [r.url for r in await _catalysts(session, user_id)] == ["https://a/g.pdf"]

    async def test_no_provider_is_a_noted_skip(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        watched = await make_instrument(session, "WATCHED")
        await _watch(session, user_id=user_id, instrument_id=watched)
        outcome = await _run(session, user_id, None)
        assert outcome.status is StepStatus.SUCCEEDED
        assert "no catalyst provider" in str(outcome.detail["skipped_reason"])

    def test_the_task_module_names_no_placing_verb(self) -> None:
        """Track C: a feed can tell; it can never act."""
        source = inspect.getsource(swing_catalyst)
        for word in ("place_order", "place_gtt", "OrderGateway", "kc."):
            assert word not in source


class TestTheCeleryBinding:
    def test_beat_runs_the_feed_at_0910_ist_on_weekdays_after_the_gap_scan(self) -> None:
        """A3: one Beat entry, its own task, a minute after the 09:16 gap scan (A4) so the live-gap
        watch rows are on the list."""
        entry = BEAT_SCHEDULE["swing-catalyst"]
        assert entry["task"] == "baskfy.swing.catalyst"
        schedule = cast(crontab, entry["schedule"])
        assert schedule.hour == {9}
        assert schedule.minute == {17}
        assert schedule.day_of_week == {1, 2, 3, 4, 5}
        assert entry["options"] == {"queue": QUEUE_COMPUTE}
        assert swing_catalyst_task.name == "baskfy.swing.catalyst"
        assert swing_catalyst_task.acks_late is True

    def test_without_a_sole_user_the_task_skips_rather_than_inventing_a_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_SOLE_USER_ID", raising=False)
        outcome = swing_catalyst_task(session_date="2026-08-19")
        assert outcome == {"date": "2026-08-19", "skipped": "no BASKFY_SOLE_USER_ID configured"}
