"""SW4's contract: the five `/swing` routes, over HTTP, against a real database.

The claims worth asserting are not "does it return 200". They are the ones a page depends on and
a reader of `docs/swing/05` §2 would be surprised to find false:

* **the funnel comes back with an empty day**, so "no flags today" can say *why* — `05` §2's
  empty state is "No flags today — 41 names were liquid, 0 met the base rules", and without the
  counts a page cannot tell a quiet market from a job that never ran;
* **the gate and the tier ride along with the candidates**, so a page never has to make a second
  call to find out whether the rows it is showing may be acted on;
* **prices keep their stored precision** — a trigger is a number somebody types into a broker,
  and `149.60` must not arrive as `149.6`;
* **the book belongs to one person** — a principal who is not the sole tenant is refused, not
  quietly served somebody else's positions (M43.4);
* **a setting above its ceiling is refused with the ceiling named**, and the refusal changes
  nothing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import api_helpers
import httpx
import pytest
import screener_helpers
import sqlalchemy as sa
from api_helpers import bearer, make_user, running_app, url
from fastapi.routing import APIRoute
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    SwCatalyst,
    SwConfig,
    SwMarketDaily,
    SwPosition,
    SwSetupDaily,
    SwSignal,
    SwWatch,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)


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


async def _setup_row(  # noqa: PLR0913 - one keyword per stored column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    setup: str = "FLAG",
    status: str = "SETTING_UP",
    score: str = "62.06",
    trigger: str = "149.60",
    stop_ref: str = "141.86",
    sector_slug: str | None = None,
    on: dt.date = AS_OF,
) -> None:
    session.add(
        SwSetupDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            setup=setup,
            status=status,
            score=Decimal(score),
            close=Decimal("144.75"),
            trigger=Decimal(trigger),
            stop_ref=Decimal(stop_ref),
            pivot_high=Decimal(trigger),
            adj_factor=Decimal(1),
            adr_pct=Decimal("4.08"),
            turnover_avg=112_734_212,
            base_bars=35,
            locked_upper_circuit=False,
            sector_slug=sector_slug,
            listed_within_2y=False,
        )
    )
    await session.flush()


async def _market_row(
    session: AsyncSession, *, user_id: int, on: dt.date = AS_OF, gate: str = "AMBER"
) -> None:
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=on,
            constituent_count=41,
            pct_up_strong_1m=Decimal("3.8000"),
            pct_new_52w_high=Decimal("1.2000"),
            pct_above_ma_slow=Decimal("55.0000"),
            index_slug="nifty-500",
            index_close=Decimal("20240.00"),
            index_ma_fast=Decimal("20200.00"),
            index_ma_slow=Decimal("20150.00"),
            gate=gate,
            exposure_level=0,
            max_open_positions=2,
            max_exposure_pct=Decimal("25.00"),
            new_entries_allowed=True,
            parabolic_count=1,
            detail={
                "funnel": {
                    "instruments": 180,
                    "with_a_bar_today": 180,
                    "liquid": 41,
                    "candidates": {"FLAG": 1, "EP": 0, "PARABOLIC_SHORT": 1},
                },
                "sectors": [
                    {"slug": "nifty-it", "pct_above_ma_slow": 80.0, "members": 10},
                    {"slug": "nifty-bank", "pct_above_ma_slow": 60.0, "members": 12},
                    {"slug": "nifty-metal", "pct_above_ma_slow": 40.0, "members": 15},
                    {"slug": "nifty-pharma", "pct_above_ma_slow": 20.0, "members": 20},
                ],
                "closed_r_multiples": [],
                "closed_trades_read": "simulated",
            },
        )
    )
    await session.flush()


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "swing-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(SwConfig(user_id=user_id, updated_by="test"))
    await session.flush()
    return user_id, public_id


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


class TestTheSetupsRoute:
    async def test_a_day_with_a_candidate_comes_back_with_its_gate_and_tier(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)
        await _market_row(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.status_code == 200
        body = response.json()
        assert body["as_of"] == AS_OF.isoformat()
        assert body["gate"] == "AMBER"
        assert body["exposure_level"] == 0
        assert body["max_open_positions"] == 2
        assert body["new_entries_allowed"] is True
        assert [row["symbol"] for row in body["data"]] == ["FLAGCO"]

    async def test_a_price_keeps_the_precision_it_was_stored_with(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 8. A trigger of `149.60` arriving as `149.6` is a different number to
        anyone reading it, and this one is typed into a broker."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert '"trigger":149.60' in response.text.replace(" ", "")

    async def test_the_stop_distance_is_derived_rather_than_stored(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(149.60 - 141.86) / 149.60 = 5.17%."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.json()["data"][0]["stop_distance_pct"] == pytest.approx(5.17)

    async def test_an_empty_day_still_says_why(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`05` §2's empty state is written from the funnel. Without it, "no flags today" and
        "the job never ran" render identically."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)
        await _market_row(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/swing/setups"), params={"setup": "EP"}, headers=bearer(public_id)
            )

        body = response.json()
        assert body["data"] == []
        assert body["funnel"]["liquid"] == 41
        assert body["funnel"]["candidates"]["EP"] == 0

    async def test_a_setup_that_is_not_a_setup_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/swing/setups"), params={"setup": "MOMENTUM"}, headers=bearer(public_id)
            )

        assert response.status_code == 400
        assert "FLAG" in response.text

    async def test_a_database_with_no_swing_rows_answers_an_empty_day(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the first detection run. Not a 404: the surface exists, the day does not."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.status_code == 200
        assert response.json() == {
            "as_of": None,
            "gate": None,
            "exposure_level": None,
            "max_open_positions": None,
            "max_exposure_pct": None,
            "new_entries_allowed": None,
            "funnel": None,
            "data": [],
        }


class TestTheBookBelongsToOnePerson:
    async def test_another_account_is_refused_rather_than_served(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M43.4's failure, on this surface. A principal who is not the sole tenant used to be
        silently promoted to it; here they are turned away."""
        user_id, _ = await _sole_tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "intruder@example.com")
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(intruder))

        assert response.status_code == 404

    async def test_an_anonymous_caller_is_unauthenticated(
        self, settings: Settings, screener_session: AsyncSession
    ) -> None:
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"))

        assert response.status_code == 401


class TestTheMarketAndSectorRoutes:
    async def test_the_market_history_comes_back_oldest_first(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _market_row(screener_session, user_id=user_id, on=AS_OF - dt.timedelta(days=1))
        await _market_row(screener_session, user_id=user_id, on=AS_OF, gate="GREEN")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/market"), headers=bearer(public_id))

        rows = response.json()["data"]
        assert [row["date"] for row in rows] == [
            (AS_OF - dt.timedelta(days=1)).isoformat(),
            AS_OF.isoformat(),
        ]
        assert rows[-1]["gate"] == "GREEN"

    async def test_a_backwards_range_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/swing/market"),
                params={"from": "2026-08-18", "to": "2026-08-01"},
                headers=bearer(public_id),
            )

        assert response.status_code == 400

    async def test_the_strip_marks_exactly_three_sectors_hot(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §2.6 gives its `+5` to the top three, so the page marks three — not the five the
        strip shows. The other two are context, not a bonus."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(
            screener_session,
            user_id=user_id,
            instrument_id=instrument_id,
            sector_slug="nifty-it",
        )
        await _market_row(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/sectors"), headers=bearer(public_id))

        rows = response.json()["data"]
        assert [row["slug"] for row in rows][:3] == ["nifty-it", "nifty-bank", "nifty-metal"]
        assert [row["hot"] for row in rows] == [True, True, True, False]
        assert rows[0]["candidates"] == 1
        assert rows[1]["candidates"] == 0


class TestTheBarsRoute:
    async def test_it_returns_closes_with_their_averages(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        for offset in range(30):
            on = AS_OF - dt.timedelta(days=29 - offset)
            screener_session.add(
                OhlcvDaily(
                    instrument_id=instrument_id,
                    date=on,
                    open=Decimal(100 + offset),
                    high=Decimal(100 + offset),
                    low=Decimal(100 + offset),
                    close=Decimal(100 + offset),
                    volume=1000,
                    close_raw=Decimal(100 + offset),
                    volume_raw=1000,
                    adj_factor=Decimal(1),
                    source="nse",
                )
            )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url(f"/swing/setups/{instrument_id}/bars"), headers=bearer(public_id)
            )

        body = response.json()
        assert body["symbol"] == "FLAGCO"
        assert body["adjusted"] is True
        assert len(body["data"]) == 30
        # The first nine points have no 10-day average yet; a partial one would be a different
        # statistic drawn on the same line.
        assert body["data"][8]["ma_fast"] is None
        assert body["data"][9]["ma_fast"] == pytest.approx(104.5)

    async def test_an_unknown_instrument_is_a_404(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/swing/setups/9999999/bars"), headers=bearer(public_id)
            )

        assert response.status_code == 404


class TestTheConfigRoutes:
    async def test_a_read_carries_the_ceilings_and_the_execution_flag(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The form renders "max 1.0% — set by the server" from these, rather than discovering
        the limit by being refused."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/config"), headers=bearer(public_id))

        body = response.json()
        assert body["ceilings"]["risk_per_trade_pct"] == "1.0"  # a string: it is a ceiling label
        assert body["execution_enabled"] is False
        assert body["exposure_level"] == 0

    async def test_a_setting_is_changed_and_read_back(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/swing/config"),
                json={"risk_per_trade_pct": "0.25", "sleeve_capital_inr": "500000"},
                headers=bearer(public_id),
            )

        assert response.status_code == 200
        # Asserted on the raw text, not on `response.json()`: the wire carries the stored
        # precision as a JSON *number* (`0.250`), and Python's JSON parser would collapse it to
        # `0.25` before any assertion could see the difference. That difference is the whole
        # point of `canonical_json` — house rule 8.
        text = response.text.replace(" ", "")
        assert '"risk_per_trade_pct":0.250' in text
        assert '"sleeve_capital_inr":500000.00' in text

    async def test_a_value_above_the_ceiling_is_a_422_naming_the_ceiling(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/swing/config"),
                json={"risk_per_trade_pct": "1.5"},
                headers=bearer(public_id),
            )

        assert response.status_code == 422
        body = response.json()
        assert body["type"] == "setting-above-ceiling"
        assert body["ceiling"] == "1.0"
        assert body["env_var"] == "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX"

    async def test_the_exposure_rung_cannot_be_set_by_asking(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A person who could set the rung has deleted the ladder. Refused, not ignored."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/swing/config"), json={"exposure_level": 3}, headers=bearer(public_id)
            )

        assert response.status_code == 400


class TestTheWatchlistRoutes:
    async def test_a_name_can_be_watched_annotated_and_dismissed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The three writes `docs/swing/02` Track A allows, end to end. None moves money."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")

        async with running_app(settings, screener_session) as client:
            added = await client.post(
                url("/swing/watch"),
                json={
                    "instrument_id": instrument_id,
                    "setup": "FLAG",
                    "trigger": "149.60",
                    "stop_ref": "141.86",
                },
                headers=bearer(public_id),
            )
            assert added.status_code == 200
            watch_id = added.json()["id"]

            annotated = await client.patch(
                url(f"/swing/watch/{watch_id}"),
                json={"catalyst": "Q2 result, order book up"},
                headers=bearer(public_id),
            )
            listed = await client.get(url("/swing/watch"), headers=bearer(public_id))
            dismissed = await client.delete(
                url(f"/swing/watch/{watch_id}"), headers=bearer(public_id)
            )
            after = await client.get(url("/swing/watch"), headers=bearer(public_id))

        assert annotated.json()["catalyst"] == "Q2 result, order book up"
        assert annotated.json()["source"] == "MANUAL"
        # SW10.5 (STANDING-ANSWERS A14): a hand-added row expires after ten sessions unless it
        # is re-confirmed — a two-week-old typed pivot is stale. Until SW10.5 it never expired.
        assert annotated.json()["expires_on"] is not None
        assert annotated.json()["expires_on"] > annotated.json()["added_on"]
        assert annotated.json()["focus"] is False, "focus is the evening's to set"
        assert [row["symbol"] for row in listed.json()["data"]] == ["FLAGCO"]
        assert dismissed.json()["state"] == "DISMISSED"
        assert after.json()["data"] == []

    async def test_a_manual_row_can_be_reconfirmed_and_the_clock_restarts(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A14: `PATCH /swing/watch/{id} {"reconfirm": true}` is the one way a MANUAL row
        outlives its ten sessions. A non-money write: no level moves, `reconfirmed_on` is
        today and `expires_on` is ten sessions from today."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")

        async with running_app(settings, screener_session) as client:
            added = await client.post(
                url("/swing/watch"),
                json={
                    "instrument_id": instrument_id,
                    "setup": "FLAG",
                    "trigger": "149.60",
                    "stop_ref": "141.86",
                },
                headers=bearer(public_id),
            )
            watch_id = added.json()["id"]
            # Age the row: added and expiring in the past, as a fortnight-old row would be.
            await screener_session.execute(
                sa.update(SwWatch)
                .where(SwWatch.id == watch_id)
                .values(added_on=dt.date(2026, 8, 3), expires_on=dt.date(2026, 8, 17))
            )
            await screener_session.flush()
            reconfirmed = await client.patch(
                url(f"/swing/watch/{watch_id}"),
                json={"reconfirm": True},
                headers=bearer(public_id),
            )

        body = reconfirmed.json()
        assert reconfirmed.status_code == 200
        today = dt.datetime.now(tz=dt.UTC).date().isoformat()
        assert body["reconfirmed_on"] == today
        assert body["expires_on"] > today
        assert body["expires_on"] > "2026-08-17"
        assert '"trigger":149.60' in reconfirmed.text and '"stop_ref":141.86' in reconfirmed.text
        assert body["source"] == "MANUAL" and body["state"] == "WATCHING"

    async def test_a_detector_row_cannot_be_reconfirmed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The detectors refresh their own rows every evening; a person cannot extend one."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        row = SwWatch(
            user_id=user_id,
            instrument_id=instrument_id,
            setup="FLAG",
            source="DETECTOR",
            added_on=dt.date(2026, 8, 18),
            expires_on=dt.date(2026, 9, 1),
            trigger=Decimal("149.60"),
            stop_ref=Decimal("141.86"),
            state="WATCHING",
        )
        screener_session.add(row)
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url(f"/swing/watch/{row.id}"), json={"reconfirm": True}, headers=bearer(public_id)
            )

        assert response.status_code == 400
        assert "MANUAL" in response.json()["detail"]

    async def test_the_focus_flag_is_on_the_read_model(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A14: the daily focus — top 5 flags by score + every EP — is a stored flag the desk
        page and the notifier read; the hub shows it beside the score."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        row = SwWatch(
            user_id=user_id,
            instrument_id=instrument_id,
            setup="FLAG",
            source="DETECTOR",
            added_on=dt.date(2026, 8, 18),
            expires_on=dt.date(2026, 9, 1),
            trigger=Decimal("149.60"),
            stop_ref=Decimal("141.86"),
            state="WATCHING",
            score=Decimal("81.50"),
            adr_pct=Decimal("6.10"),
            focus=True,
        )
        screener_session.add(row)
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            listed = await client.get(url("/swing/watch"), headers=bearer(public_id))

        (item,) = listed.json()["data"]
        assert item["focus"] is True
        assert '"score":81.50' in listed.text and '"adr_pct":6.10' in listed.text

    async def test_a_level_cannot_be_edited_after_the_fact(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trigger a person can revise is a trigger that can be revised to match a price they
        already paid. The PATCH model carries a note and a catalyst and nothing else."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")

        async with running_app(settings, screener_session) as client:
            added = await client.post(
                url("/swing/watch"),
                json={"instrument_id": instrument_id, "setup": "FLAG", "trigger": "149.60"},
                headers=bearer(public_id),
            )
            response = await client.patch(
                url(f"/swing/watch/{added.json()['id']}"),
                json={"trigger": "100.00"},
                headers=bearer(public_id),
            )

        assert response.status_code == 400

    async def test_a_parabolic_short_cannot_be_watched(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PACK.1. The request model admits FLAG and EP; there is no third option to send."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "PARACO")

        async with running_app(settings, screener_session) as client:
            response = await client.post(
                url("/swing/watch"),
                json={"instrument_id": instrument_id, "setup": "PARABOLIC_SHORT"},
                headers=bearer(public_id),
            )

        assert response.status_code == 400

    async def test_an_unknown_instrument_cannot_be_watched(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.post(
                url("/swing/watch"),
                json={"instrument_id": 9_999_999, "setup": "FLAG"},
                headers=bearer(public_id),
            )

        assert response.status_code == 404

    async def test_another_account_cannot_read_the_list(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _sole_tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "intruder2@example.com")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/watch"), headers=bearer(intruder))

        assert response.status_code == 404


class TestThePositionsRoute:
    async def test_an_empty_book_is_an_empty_list_and_no_plan(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the first EOD run. Not a 404: the surface exists, the book is empty."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/positions"), headers=bearer(public_id))

        assert response.status_code == 200
        assert response.json() == {"data": [], "plan": None}

    async def test_a_position_with_no_resting_stop_is_flagged(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`naked` is derived on the way out, so a page cannot forget to compute it."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "NAKEDCO")
        screener_session.add(
            SwPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                setup="FLAG",
                entry_date=AS_OF,
                entry_avg=Decimal("100.0000"),
                quantity_entered=100,
                initial_stop=Decimal("96.00"),
                stop=Decimal("96.00"),
                gtt_id=None,
                trail="MA20",
                quantity_open=100,
                state="OPEN",
                simulated=True,
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/positions"), headers=bearer(public_id))

        row = response.json()["data"][0]
        assert row["naked"] is True
        assert row["simulated"] is True


# --- SW11B: the catalyst feed on the two GETs (STANDING-ANSWERS A3) ----------------------

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FILING = "https://nsearchives.nseindia.com/corporate/FLAGCO_18082026183211_PR.pdf?x=1&y=2"
CALENDAR = (
    "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol=FLAGCO"
)


async def _catalyst_rows(session: AsyncSession, *, user_id: int, instrument_id: int) -> None:
    """Two announcements (the newer one second, so order is by stamp, not by id) and the
    calendar row the feed keeps for the nearest result meeting."""
    session.add_all(
        [
            SwCatalyst(
                user_id=user_id,
                instrument_id=instrument_id,
                headline="Updates — capex approved",
                published_at=dt.datetime(2026, 8, 17, 9, 0, tzinfo=IST),
                url="https://nsearchives.nseindia.com/corporate/FLAGCO_older.pdf",
                source="NSE_ANNOUNCEMENT",
            ),
            SwCatalyst(
                user_id=user_id,
                instrument_id=instrument_id,
                headline="Press Release - FLAGCO wins a multi-year order",
                published_at=dt.datetime(2026, 8, 18, 18, 32, 11, tzinfo=IST),
                url=FILING,
                source="NSE_ANNOUNCEMENT",
            ),
            SwCatalyst(
                user_id=user_id,
                instrument_id=instrument_id,
                headline="Financial Results",
                published_at=None,
                url=CALENDAR,
                source="NSE_EVENT_CALENDAR",
                earnings_date=dt.date(2026, 10, 15),
            ),
        ]
    )
    await session.flush()


class TestTheCatalystFeedOnTheReads:
    async def test_a_setup_row_carries_the_newest_headline_its_link_and_the_earnings_date(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A3: headline + timestamp + filing URL, and the earnings flag — four fields, never
        the filing's text. The URL is verbatim, query string and all."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)
        await _catalyst_rows(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        feed = response.json()["data"][0]["catalyst_feed"]
        # The instant, whichever offset the wire spells it in.
        assert dt.datetime.fromisoformat(feed.pop("published_at")) == dt.datetime(
            2026, 8, 18, 18, 32, 11, tzinfo=IST
        )
        assert feed == {
            "headline": "Press Release - FLAGCO wins a multi-year order",
            "url": FILING,
            "earnings_date": "2026-10-15",
        }

    async def test_a_name_the_feed_has_nothing_for_reads_null_not_an_empty_object(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.json()["data"][0]["catalyst_feed"] is None

    async def test_a_watch_row_carries_the_feed_beside_its_own_text_and_earnings_flag(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`catalyst` is the person's text (or the auto-fill); `catalyst_feed` is the link;
        `earnings_date` the flag the 09:10 job keeps on the row. Three separate things."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _catalyst_rows(screener_session, user_id=user_id, instrument_id=instrument_id)
        screener_session.add(
            SwWatch(
                user_id=user_id,
                instrument_id=instrument_id,
                setup="FLAG",
                source="MANUAL",
                added_on=AS_OF,
                trigger=Decimal("149.60"),
                stop_ref=Decimal("141.86"),
                catalyst="typed by hand",
                earnings_date=dt.date(2026, 10, 15),
                state="WATCHING",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/watch"), headers=bearer(public_id))

        row = response.json()["data"][0]
        assert row["catalyst"] == "typed by hand"
        assert row["earnings_date"] == "2026-10-15"
        assert row["catalyst_feed"]["url"] == FILING
        assert row["catalyst_feed"]["headline"].startswith("Press Release")

    async def test_the_feed_belongs_to_the_tenant_that_stored_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Track C §6: a row another user's job stored is not this user's link."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        other_id, _ = await make_user(screener_session, "swing-other@example.com")
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _setup_row(screener_session, user_id=user_id, instrument_id=instrument_id)
        await _catalyst_rows(screener_session, user_id=other_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.json()["data"][0]["catalyst_feed"] is None

    def test_the_feed_adds_no_route(self) -> None:
        """A GET-only addition: the read-only proofs stay exactly as strict."""
        from baskfy_api.routers.swing import router  # noqa: PLC0415

        assert not any(
            "catalyst" in route.path for route in router.routes if isinstance(route, APIRoute)
        )
        assert any(isinstance(route, APIRoute) for route in router.routes)


# --- SW14: what the monitor raised, and the drawdown on the market row -----------------


async def _signal(  # noqa: PLR0913 - one keyword per stored column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    on: dt.date = AS_OF,
    at: dt.time = dt.time(9, 23),
    state: str = "TRIGGERED",
    range_high: str | None = "418.90",
    range_low: str | None = "412.30",
    entry: str | None = "419.35",
    stop: str | None = "412.30",
) -> None:
    session.add(
        SwSignal(
            user_id=user_id,
            instrument_id=instrument_id,
            setup="FLAG",
            session_date=on,
            raised_at=dt.datetime.combine(on, at, tzinfo=IST),
            state=state,
            or_window_minutes=5,
            range_high=None if range_high is None else Decimal(range_high),
            range_low=None if range_low is None else Decimal(range_low),
            low_of_day=None if range_low is None else Decimal(range_low),
            last_price=None if entry is None else Decimal(entry),
            entry=None if entry is None else Decimal(entry),
            stop=None if stop is None else Decimal(stop),
        )
    )
    await session.flush()


class TestTheSignalsRoute:
    async def test_the_latest_session_comes_back_newest_first_with_its_levels(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`05` §2: "fired 09:23, 5-min range 412.30 to 418.90". Every verdict is a row — the
        `BELOW_PIVOT` at 09:21 is the record of a break that was not a breakout — and an older
        session's rows are not mixed in when no date is asked for."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _signal(
            screener_session,
            user_id=user_id,
            instrument_id=instrument_id,
            at=dt.time(9, 21),
            state="BELOW_PIVOT",
            entry=None,
            stop=None,
        )
        await _signal(screener_session, user_id=user_id, instrument_id=instrument_id)
        await _signal(
            screener_session,
            user_id=user_id,
            instrument_id=instrument_id,
            on=AS_OF - dt.timedelta(days=1),
            state="LOCKED_UPPER_CIRCUIT",
        )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/signals"), headers=bearer(public_id))

        assert response.status_code == 200
        body = response.json()
        assert body["session_date"] == AS_OF.isoformat()
        assert [(row["state"], row["symbol"]) for row in body["data"]] == [
            ("TRIGGERED", "FLAGCO"),
            ("BELOW_PIVOT", "FLAGCO"),
        ]
        fired = body["data"][0]
        assert fired["or_window_minutes"] == 5
        # The wire carries the instant (UTC); the page says "fired 09:23" in exchange time.
        raised_at = dt.datetime.fromisoformat(fired["raised_at"]).astimezone(IST)
        assert (raised_at.date(), raised_at.hour, raised_at.minute) == (AS_OF, 9, 23)
        # House rule 8: the range is typed into a broker, and `412.30` is not `412.3`.
        assert '"range_low":412.30' in response.text.replace(" ", "")
        assert '"range_high":418.90' in response.text.replace(" ", "")
        assert fired["plan_line_id"] is None

    async def test_a_date_and_an_instrument_narrow_the_read(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        flagco = await _instrument(screener_session, "FLAGCO")
        other = await _instrument(screener_session, "OTHERCO")
        yesterday = AS_OF - dt.timedelta(days=1)
        await _signal(screener_session, user_id=user_id, instrument_id=flagco, on=yesterday)
        await _signal(screener_session, user_id=user_id, instrument_id=other, on=yesterday)
        await _signal(screener_session, user_id=user_id, instrument_id=flagco)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/swing/signals"),
                params={"date": yesterday.isoformat(), "instrument_id": flagco},
                headers=bearer(public_id),
            )

        body = response.json()
        assert body["session_date"] == yesterday.isoformat()
        assert [row["symbol"] for row in body["data"]] == ["FLAGCO"]

    async def test_a_tenant_with_no_signals_reads_an_empty_session(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the monitor has run. Not a 404: the surface exists, the session does not."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/signals"), headers=bearer(public_id))

        assert response.status_code == 200
        assert response.json() == {"session_date": None, "data": []}

    async def test_the_signals_belong_to_one_person(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, _ = await _sole_tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "intruder3@example.com")
        instrument_id = await _instrument(screener_session, "FLAGCO")
        await _signal(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            refused = await client.get(url("/swing/signals"), headers=bearer(intruder))
            anonymous = await client.get(url("/swing/signals"))

        assert refused.status_code == 404
        assert anonymous.status_code == 401


class TestTheMarketRowCarriesTheDrawdown:
    async def test_the_lock_out_and_the_drawdown_ride_along(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §8.5: the page shows "Locked out · 15.30% below its peak" in place of the rung,
        and it can only do that if the row it reads says so."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _market_row(screener_session, user_id=user_id)
        await screener_session.execute(
            sa.update(SwMarketDaily)
            .where(SwMarketDaily.user_id == user_id)
            .values(drawdown_pct=Decimal("15.30"), drawdown_locked=True)
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/market"), headers=bearer(public_id))

        row = response.json()["data"][-1]
        assert row["drawdown_locked"] is True
        assert '"drawdown_pct":15.30' in response.text.replace(" ", "")


class TestWatchingBySymbol:
    async def test_a_symbol_from_the_search_box_resolves_to_the_instrument(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SW14: the web form's lookup is `GET /search`, whose instrument hit carries the
        symbol; the add-manual action posts it as typed, and the route resolves it."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "FLAGCO")

        async with running_app(settings, screener_session) as client:
            added = await client.post(
                url("/swing/watch"),
                json={"symbol": " flagco ", "setup": "FLAG", "trigger": "149.60"},
                headers=bearer(public_id),
            )
            unknown = await client.post(
                url("/swing/watch"),
                json={"symbol": "NOSUCHCO", "setup": "FLAG"},
                headers=bearer(public_id),
            )
            both = await client.post(
                url("/swing/watch"),
                json={"symbol": "FLAGCO", "instrument_id": instrument_id, "setup": "FLAG"},
                headers=bearer(public_id),
            )
            neither = await client.post(
                url("/swing/watch"), json={"setup": "FLAG"}, headers=bearer(public_id)
            )

        assert added.status_code == 200
        assert added.json()["instrument_id"] == instrument_id
        assert added.json()["symbol"] == "FLAGCO"
        assert unknown.status_code == 404
        assert both.status_code == 400
        assert neither.status_code == 400


def test_the_helpers_are_the_ones_this_module_thinks() -> None:
    """A guard on the fixtures: `screener_helpers` moving would make every test above skip."""
    assert hasattr(screener_helpers, "seeded_database")
    assert isinstance(httpx.AsyncClient, type)
