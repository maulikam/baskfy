"""`GET /twt/today`, over HTTP against a real database — the read the hub never had (TW13).

WHAT THIS FILE IS A REGRESSION FOR
-----------------------------------
On 12 Sep 2026 Maulik pressed "Scan now" on `/twt`. The scan **ran and succeeded** — two
`tw_scan_run` rows, both `DONE`, ~19 s each — and the detector wrote, for the published session
of 2026-09-11: one `tw_breadth_daily` row, two `tw_signal_daily` rows (IOLCP and OPTIEMUS, both
`SIGNAL`) and 58 `tw_state_daily` rows. An independent local run of the same detector for the
same session produced 57 names, so the data was right.

The page still said **"Nothing has been read for this strategy yet."**

The reason was not a date and not a tenant. `apps/web/src/lib/twt/fetch.ts` asks for
`/api/v1/twt/today`; that route did not exist; `readOrNull` turns its 404 into `null` by design,
because a page that 500s before its first job has run is a worse page. So the empty state was
reporting an absent *reader* as though it were an absent *writer* — the one failure mode a page
built ahead of its data is most likely to hide, and the hardest for its owner to tell from a
strategy that found nothing.

Every test below is written against that shape rather than against "does it return 200":

* **the session the reader resolves is the session the writer wrote.** Not today, not the latest
  `pipeline_run`. Asserted by reading on a *later* calendar day, which is the situation the bug
  was found in — a Saturday, reading Friday's session;
* **the names in the state come back even when they had no entry event.** 56 of the 58 rows on
  11 Sep had none. An inner join would have served two rows and looked like it worked;
* **the gate rides along**, so the page never makes a second call to find out whether the rows it
  is showing may be acted on at all;
* **the funnel's wider step survives.** `tw_breadth_daily.universe_count` is already "names with a
  bar"; the screened market is only in `detail`, and a funnel whose first step silently became
  its second would misstate the market by a factor of three;
* **prices keep their stored precision** (house rule 8);
* **the sleeve is one person's** — a principal who is not the sole tenant is refused, not quietly
  served somebody else's book;
* **and the route writes nothing**, which on this sleeve is the claim that matters most.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import api_helpers
import pytest
import sqlalchemy as sa
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    TwBreadthDaily,
    TwConfig,
    TwScanRun,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.twt.config import Gate, SignalState

pytestmark = [requires_db, pytest.mark.db]

#: The session the detector actually wrote on the box, and the day the page was read.
#: They are different on purpose: the reader must not resolve "today".
SESSION = dt.date(2026, 9, 11)
READ_ON = dt.date(2026, 9, 12)

#: The box's own funnel, verbatim from `tw_breadth_daily.detail` on 2026-09-11.
FUNNEL: dict[str, object] = {
    "bars": 543_427,
    "entries": 2,
    "signals": 2,
    "in_state": 58,
    "with_a_bar": 3_413,
    "instruments": 10_155,
    "above_the_dma": 905,
    "dropped_thin_sessions": 0,
}


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "twt-today-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(
        TwConfig(
            user_id=user_id,
            sleeve_capital_inr=Decimal("1000000.00"),
            first_live_entries_left=10,
            updated_by="test",
        )
    )
    await session.flush()
    return user_id, public_id


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


async def _breadth(session: AsyncSession, *, user_id: int, on: dt.date = SESSION) -> None:
    """The box's reading for 2026-09-11, number for number."""
    session.add(
        TwBreadthDaily(
            user_id=user_id,
            date=on,
            universe_count=3_413,
            measured_count=1_769,
            above_count=905,
            pct_above_dma=Decimal("51.1588"),
            gate=Gate.OPEN.value,
            dma_bars=200,
            thin_session=False,
            detail=FUNNEL,
        )
    )
    await session.flush()


async def _state(  # noqa: PLR0913 - a state row is its numbers
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    on: dt.date = SESSION,
    close: str = "149.60",
    sessions_in_state: int = 4,
    turnover_avg_20: int = 500_000_000,
) -> None:
    session.add(
        TwStateDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            close=Decimal(close),
            close_raw=Decimal(close),
            adj_factor=Decimal(1),
            week_close_0=Decimal(close),
            week_close_1=Decimal("148.90"),
            week_close_2=Decimal("147.50"),
            week_range_pct=Decimal("1.4237"),
            month_low_3=Decimal("100.00"),
            month_low_ratio=Decimal("1.4960"),
            vol_sma_50=250_000,
            volume=310_000,
            turnover_inr=turnover_avg_20,
            turnover_avg_20=turnover_avg_20,
            sma_dma=Decimal("120.00"),
            sessions_in_state=sessions_in_state,
            bars_in_window=260,
            locked_upper_circuit=False,
        )
    )
    await session.flush()


async def _signal(  # noqa: PLR0913 - a signal row is its numbers
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    state: str = SignalState.SIGNAL.value,
    failed: list[str] | None = None,
    on: dt.date = SESSION,
    close: str = "149.60",
) -> None:
    session.add(
        TwSignalDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            state=state,
            failed_filters=failed or [],
            entry_reference_close=Decimal(close),
            stop_preview=(Decimal(close) * Decimal("0.80")).quantize(Decimal("0.01")),
            sessions_out_before=7,
            rank_key=500_000_000,
            turnover_avg_20=500_000_000,
        )
    )
    await session.flush()


async def _the_box(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str, dict[str, int]]:
    """The box's 12 Sep 2026 state, in miniature: a breadth row, four names in the state on the
    published session, two of which had an entry event and one of which was rejected.

    Four rather than fifty-eight: the claim is that a name **without** an event is served, and
    fifty-four more of them prove nothing the fourth does not.
    """
    user_id, public_id = await _sole_tenant(session, monkeypatch)
    ids = {
        symbol: await _instrument(session, symbol)
        for symbol in ("IOLCP", "OPTIEMUS", "QUIETONE", "THINONE")
    }
    await _breadth(session, user_id=user_id)
    for symbol, close in (
        ("IOLCP", "149.60"),
        ("OPTIEMUS", "612.35"),
        ("QUIETONE", "88.10"),
        ("THINONE", "44.05"),
    ):
        await _state(
            session,
            user_id=user_id,
            instrument_id=ids[symbol],
            close=close,
            turnover_avg_20=500_000_000 if symbol != "THINONE" else 12_000_000,
        )
    await _signal(session, user_id=user_id, instrument_id=ids["IOLCP"])
    await _signal(session, user_id=user_id, instrument_id=ids["OPTIEMUS"], close="612.35")
    await _signal(
        session,
        user_id=user_id,
        instrument_id=ids["THINONE"],
        state=SignalState.SCAN_ONLY.value,
        failed=["TURNOVER"],
        close="44.05",
    )
    return user_id, public_id, ids


class TestTheSessionTheReaderResolves:
    async def test_a_scan_that_wrote_yesterday_is_read_today(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug, as one assertion.

        Nothing is asked for by date. The calendar day is 12 Sep and the only session the
        detector wrote is 11 Sep, which is the situation "Scan now" leaves behind every time it
        is pressed — the worker detects the latest *published* session, never today. A reader
        that resolved today, or the newest `pipeline_run`, answers an empty page here.
        """
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["as_of"] == SESSION.isoformat(), (
            "the reader resolved a session the writer never wrote"
        )
        assert body["as_of"] != READ_ON.isoformat(), "the reader asked for today"
        assert body["gate"]["gate"] == "OPEN"
        assert body["gate"]["date"] == SESSION.isoformat()

    async def test_the_page_has_something_to_say(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/twt` renders "Nothing has been read for this strategy yet" when `gate.gate` is null.
        This is that sentence's negation, asserted at the payload rather than at the page."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        assert body["gate"]["gate"] is not None, "the hub would still render its empty state"
        assert body["tight"], "the hub would still render an empty table"

    async def test_a_named_session_with_no_reading_is_stamped_with_the_date_asked_for(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So the page can name the session it found nothing for, rather than claiming that
        nothing has ever been read."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (
                await client.get(
                    url("/twt/today"), params={"date": "2026-09-04"}, headers=bearer(public_id)
                )
            ).json()

        assert body["as_of"] == "2026-09-04"
        assert body["gate"]["gate"] is None
        assert body["tight"] == []

    async def test_nothing_detected_at_all_is_a_null_as_of_and_not_an_error(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The state the sleeve was genuinely in for its first weeks. A 404 here would have been
        indistinguishable from the bug this route fixes."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["as_of"] is None
        assert body["gate"]["gate"] is None
        assert body["gate"]["threshold_pct"] == "40.0", "the threshold is served even at zero"
        assert isinstance(body["gate"]["threshold_pct"], str), (
            "a decimal must arrive quoted; `JSON.parse` on a bare token loses the precision "
            "`@/lib/twt/numbers` is built on"
        )


class TestTheNamesInTheState:
    async def test_a_name_with_no_entry_event_is_served(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """56 of the box's 58 rows were in exactly this state. An inner join onto
        `tw_signal_daily` would have served two names out of fifty-eight and looked correct."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        by_symbol = {row["symbol"]: row for row in body["tight"]}
        assert set(by_symbol) == {"IOLCP", "OPTIEMUS", "QUIETONE", "THINONE"}
        assert by_symbol["QUIETONE"]["signal_state"] is None
        assert by_symbol["QUIETONE"]["failed_filters"] == []
        assert by_symbol["QUIETONE"]["sessions_in_state"] == 4

    async def test_the_entries_and_the_reject_carry_their_state(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`05` §1.2: the rejects are shown, with the filter that rejected them. A screen that
        hides what it passed over cannot be audited by the person whose money it is."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        by_symbol = {row["symbol"]: row for row in body["tight"]}
        assert [by_symbol["IOLCP"]["signal_state"], by_symbol["OPTIEMUS"]["signal_state"]] == [
            "SIGNAL",
            "SIGNAL",
        ]
        assert by_symbol["THINONE"]["signal_state"] == "SCAN_ONLY"
        assert by_symbol["THINONE"]["failed_filters"] == ["TURNOVER"]

    async def test_prices_keep_the_precision_they_were_stored_at(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 8: storage precision is the contract, so the page, the CSV and the figure
        beside a stop can never disagree. `close_raw` is stored at `PRICE_RAW`'s four decimals and
        arrives at four; `week_close_0` is `PRICE`'s two and arrives at two. Neither is trimmed to
        look tidier, and neither becomes `149.6`.

        Quoted, too. `JSON.parse` on the bare token `149.6000` is the double `149.6`, and
        `@/lib/twt/numbers` does `BigInt` arithmetic over decimal strings precisely so that a
        rupee never passes through a float.
        """
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            raw = (await client.get(url("/twt/today"), headers=bearer(public_id))).text

        assert '"close_raw":"149.6000"' in raw, raw[:400]
        assert '"week_close_0":"149.60"' in raw, raw[:400]
        assert '"pct_above_dma":"51.1588"' in raw
        assert "149.6," not in raw and '"149.6"' not in raw, "a decimal was rendered as a float"


class TestTheGateAndItsFunnel:
    async def test_the_funnel_keeps_the_screened_market_and_the_names_with_a_bar_apart(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`tw_breadth_daily.universe_count` is *already* "names with a bar on the date". The
        wider number — the screened market — exists only in `detail`, and the page's funnel reads
        them as two separate steps. Serving the column as both would have understated the market
        by a factor of three and still looked plausible.
        """
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            gate = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()["gate"]

        assert gate["universe_count"] == 10_155, "the screened market"
        assert gate["with_bar_count"] == 3_413, "of those, the ones that printed"
        assert gate["measured_count"] == 1_769
        assert gate["above_count"] == 905
        assert gate["universe_count"] > gate["with_bar_count"] > gate["measured_count"], (
            "each funnel step must be a subset of the one above it"
        )

    async def test_a_row_without_a_funnel_serves_nulls_rather_than_zeroes(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero and an unknown render identically in a funnel, and only one of them is a
        statement about the market. The web app drops a null step; it cannot drop a zero."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        screener_session.add(
            TwBreadthDaily(
                user_id=user_id,
                date=SESSION,
                universe_count=3_413,
                measured_count=1_769,
                above_count=905,
                pct_above_dma=Decimal("51.1588"),
                gate=Gate.OPEN.value,
                dma_bars=200,
                thin_session=False,
                detail=None,
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            gate = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()["gate"]

        assert gate["universe_count"] is None
        assert gate["with_bar_count"] == 3_413


class TestTheRestOfThePayload:
    async def test_the_half_size_counter_says_trading_is_off(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`05` §1.4. The counter must not tick while nothing can trade — that would be a number
        describing an event that has not occurred — so the flag is served beside it."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        assert body["half_size"] == {
            "entries_left": 10,
            "entries_total": 10,
            "execution_enabled": False,
        }

    async def test_the_book_is_empty_and_that_is_a_list_not_a_null(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sleeve has never bought anything. It owns only what it bought (`02` Track C §5),
        so an empty book is the correct answer and never the account's holdings."""
        _, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        assert body["positions"] == []

    async def test_the_newest_scan_rides_along_so_the_button_costs_no_second_request(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`fetchLastScan` takes the run inline when the payload names it, and `id` is the field
        it reads — the shape `/swing` already serves."""
        user_id, public_id, _ = await _the_box(screener_session, monkeypatch)
        older = TwScanRun(user_id=user_id, status="DONE", source="web", session_date=SESSION)
        newer = TwScanRun(user_id=user_id, status="DONE", source="web", session_date=SESSION)
        screener_session.add_all([older, newer])
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/twt/today"), headers=bearer(public_id))).json()

        assert body["last_scan"] is not None
        assert body["last_scan"]["id"] == int(newer.id)
        assert body["last_scan"]["status"] == "DONE"
        assert "provisional" not in body["last_scan"], "TW12.2: this sleeve has no provisional run"


class TestItIsOnePersonsBook:
    async def test_a_principal_who_is_not_the_sole_tenant_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M43.4: refused, not quietly promoted onto somebody else's rows."""
        await _the_box(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "twt-today-intruder@example.com")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/twt/today"), headers=bearer(intruder))

        assert response.status_code == 404, response.text

    async def test_an_anonymous_caller_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/twt/today"))

        assert response.status_code == 401, response.text


class TestItWritesNothing:
    async def test_a_read_leaves_every_row_of_the_sleeve_alone(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim that matters most on this sleeve, asserted after a request rather than
        reasoned about: the capital is untouched and the countdown has not moved."""
        user_id, public_id, _ = await _the_box(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            assert (
                await client.get(url("/twt/today"), headers=bearer(public_id))
            ).status_code == 200

        config = (
            await screener_session.execute(sa.select(TwConfig).where(TwConfig.user_id == user_id))
        ).scalar_one()
        assert config.sleeve_capital_inr == Decimal("1000000.00")
        assert config.first_live_entries_left == 10
        scans = (
            await screener_session.execute(
                sa.select(sa.func.count())
                .select_from(TwScanRun)
                .where(TwScanRun.user_id == user_id)
            )
        ).scalar_one()
        assert scans == 0, "a read queued a scan"
