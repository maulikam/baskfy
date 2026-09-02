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
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    SwConfig,
    SwMarketDaily,
    SwPosition,
    SwSetupDaily,
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
        # A hand-added row has no expiry: the person is watching for a reason the detectors
        # cannot see, and retiring it would be the system overruling them.
        assert annotated.json()["expires_on"] is None
        assert [row["symbol"] for row in listed.json()["data"]] == ["FLAGCO"]
        assert dismissed.json()["state"] == "DISMISSED"
        assert after.json()["data"] == []

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


def test_the_helpers_are_the_ones_this_module_thinks() -> None:
    """A guard on the fixtures: `screener_helpers` moving would make every test above skip."""
    assert hasattr(screener_helpers, "seeded_database")
    assert isinstance(httpx.AsyncClient, type)
