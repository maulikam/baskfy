"""VB8's contract: the six `/vbt` routes, over HTTP, against a real database.

The claims worth asserting are not "does it return 200". They are the ones `docs/vbt/05` §2
depends on and a reader would be surprised to find false:

* **the funnel comes back on an empty day**, so "no candidates" can say *why* — without it a
  quiet market and a detector that never ran render identically, and the second is an alert;
* **the gate rides along with the candidates**, so a page never makes a second call to find out
  whether the rows it is showing may be acted on at all;
* **the rejects come back with their letters**, because `01` §3's ablation is the argument for
  the six filters and a page that hides the rejects makes it unreadable;
* **prices keep their stored precision** — a limit of `149.60` must not arrive as `149.6`; it is
  a number somebody types into a broker (house rule 8);
* **the sleeve belongs to one person** — a principal who is not the sole tenant is refused, not
  quietly served somebody else's book (M43.4);
* **the fill rate is null before anything has resolved**, rather than 0%, which would be a claim
  about the market instead of a fact about an empty book.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    VbBacktestRun,
    VbBreadthDaily,
    VbConfig,
    VbOrder,
    VbPosition,
    VbSignalDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.config import Gate, SignalState
from baskfy_core.vbt.published import PUBLISHED

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "vbt-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(
        VbConfig(user_id=user_id, sleeve_capital_inr=Decimal("1000000.00"), updated_by="test")
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


async def _breadth(  # noqa: PLR0913 - a breadth row is its numbers
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date = AS_OF,
    gate: Gate = Gate.OPEN,
    pct: str = "62.4000",
    detail: dict[str, object] | None = None,
) -> None:
    session.add(
        VbBreadthDaily(
            user_id=user_id,
            date=on,
            universe_count=4_186,
            measured_count=1_412,
            above_count=881,
            pct_above_dma=Decimal(pct),
            gate=gate.value,
            dma_bars=200,
            detail=detail
            or {
                "funnel": {
                    "universe": 4_186,
                    "with_bar": 1_412,
                    "scan_hits": 37,
                    "signals": 4,
                }
            },
        )
    )
    await session.flush()


async def _signal(  # noqa: PLR0913 - a signal row is its numbers
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    state: str = SignalState.SIGNAL.value,
    limit: str = "149.60",
    sma_200: str = "100.00",
    failed: list[str] | None = None,
    rank: int = 1_000_000,
    on: dt.date = AS_OF,
) -> None:
    session.add(
        VbSignalDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            state=state,
            failed_filters=failed or [],
            close=Decimal(limit),
            close_raw=Decimal(limit),
            adj_factor=Decimal(1),
            limit_price=Decimal(limit),
            stop_price=(Decimal(limit) * Decimal("0.88")).quantize(Decimal("0.01")),
            sma_200=Decimal(sma_200),
            turnover_avg_20=500_000_000,
            rank_key=rank,
        )
    )
    await session.flush()


class TestTheTodayRoute:
    async def test_a_session_comes_back_with_its_gate_and_its_candidates(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "VBTCO")
        await _breadth(screener_session, user_id=user_id)
        await _signal(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert response.status_code == 200
        body = response.json()
        assert body["as_of"] == AS_OF.isoformat()
        assert body["gate"] == "OPEN"
        assert body["gate_threshold_pct"] == 40.0
        assert [row["symbol"] for row in body["candidates"]] == ["VBTCO"]
        assert body["rejects"] == []

    async def test_the_distance_from_the_dma_is_derived_rather_than_stored(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """149.60 over a 100.00 average is 49.60% above it."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "VBTCO")
        await _breadth(screener_session, user_id=user_id)
        await _signal(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert response.json()["candidates"][0]["pct_above_dma"] == pytest.approx(49.60)

    async def test_a_limit_keeps_the_precision_it_was_stored_with(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 8. `149.60` arriving as `149.6` is a different number to a person, and
        this one is typed into a broker."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "VBTCO")
        await _breadth(screener_session, user_id=user_id)
        await _signal(screener_session, user_id=user_id, instrument_id=instrument_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert '"limit_price":149.60' in response.text.replace(" ", "")

    async def test_the_rejects_come_back_with_the_letters_that_failed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`05` §2: "AHCL — B, F". The ablation table is the argument for the six filters, and a
        page that never shows what they rejected cannot make it."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "AHCL")
        await _breadth(screener_session, user_id=user_id)
        await _signal(
            screener_session,
            user_id=user_id,
            instrument_id=instrument_id,
            state=SignalState.SCAN_ONLY.value,
            failed=["B", "F"],
        )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        body = response.json()
        assert body["candidates"] == []
        assert [row["failed_filters"] for row in body["rejects"]] == [["B", "F"]]

    async def test_an_empty_session_still_says_why(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session with no candidates writes a breadth row anyway, and the funnel on it is the
        difference between "nothing qualified" and "the detector did not run"."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        body = response.json()
        assert body["candidates"] == []
        assert body["funnel"]["funnel"]["universe"] == 4_186

    async def test_a_database_the_detector_never_ran_against_is_a_different_state(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        body = response.json()
        assert body["as_of"] is None
        assert body["gate"] is None
        assert body["funnel"] is None

    async def test_the_default_is_the_latest_session_and_not_todays_date(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §10: the sleeve's clock is the published session's. Asking for "today" during a
        session would answer with an empty page rather than the last real one."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id, on=AS_OF - dt.timedelta(days=1))
        await _breadth(screener_session, user_id=user_id, on=AS_OF, gate=Gate.SHUT, pct="31.0000")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        body = response.json()
        assert body["as_of"] == AS_OF.isoformat()
        assert body["gate"] == "SHUT"

    async def test_the_shut_count_is_over_sessions_not_days(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        for offset in range(4):
            await _breadth(
                screener_session,
                user_id=user_id,
                on=AS_OF - dt.timedelta(days=offset),
                gate=Gate.SHUT if offset % 2 else Gate.OPEN,
            )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert response.json()["shut_sessions_recent"] == 2


class TestTheSleeveBelongsToOnePerson:
    async def test_another_account_is_refused_rather_than_served(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M43.4's failure, on this surface."""
        user_id, _ = await _sole_tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "vbt-intruder@example.com")
        await _breadth(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(intruder))

        assert response.status_code == 404

    async def test_an_anonymous_caller_is_unauthenticated(
        self, settings: Settings, screener_session: AsyncSession
    ) -> None:
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"))

        assert response.status_code == 401


class TestTheBreadthRoute:
    async def test_the_series_comes_back_oldest_first_with_its_threshold(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id, on=AS_OF - dt.timedelta(days=1))
        await _breadth(screener_session, user_id=user_id, on=AS_OF)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/breadth"), headers=bearer(public_id))

        body = response.json()
        assert body["threshold_pct"] == 40.0
        assert [row["date"] for row in body["data"]] == [
            (AS_OF - dt.timedelta(days=1)).isoformat(),
            AS_OF.isoformat(),
        ]

    async def test_a_window_is_honoured(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id, on=AS_OF - dt.timedelta(days=10))
        await _breadth(screener_session, user_id=user_id, on=AS_OF)

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/vbt/breadth"),
                params={"from": AS_OF.isoformat()},
                headers=bearer(public_id),
            )

        assert [row["date"] for row in response.json()["data"]] == [AS_OF.isoformat()]


class TestTheBookRoute:
    async def test_a_working_limit_reports_its_sessions_of_three(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "VBTCO")
        await _breadth(screener_session, user_id=user_id)
        screener_session.add(
            VbOrder(
                user_id=user_id,
                instrument_id=instrument_id,
                signal_date=AS_OF - dt.timedelta(days=2),
                limit_price=Decimal("100.00"),
                stop_price=Decimal("88.00"),
                quantity=100,
                state="SENT",
                working_from=AS_OF - dt.timedelta(days=1),
                expires_after_session=AS_OF,
                sessions_worked=3,
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/book"), headers=bearer(public_id))

        row = response.json()["working"][0]
        assert (row["sessions_worked"], row["sessions_allowed"]) == (3, 3)
        assert row["expires_tonight"] is True
        assert row["value_inr"] == 10_000.0

    async def test_a_naked_position_says_so(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        instrument_id = await _instrument(screener_session, "VBTCO")
        await _breadth(screener_session, user_id=user_id)
        screener_session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                entry_date=AS_OF - dt.timedelta(days=5),
                entry_avg=Decimal("100.0000"),
                quantity_entered=100,
                quantity_open=100,
                initial_stop=Decimal("88.00"),
                stop_price=Decimal("88.00"),
                state="OPEN",
            )
        )
        screener_session.add(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=AS_OF,
                open=Decimal("110.0000"),
                high=Decimal("110.0000"),
                low=Decimal("110.0000"),
                close=Decimal("110.0000"),
                close_raw=Decimal("110.0000"),
                volume=1_000,
                volume_raw=1_000,
                turnover=Decimal("110000.00"),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/book"), headers=bearer(public_id))

        row = response.json()["open_positions"][0]
        assert row["naked"] is True
        assert row["return_pct"] == pytest.approx(10.0)
        # (110 - 100) / (100 - 88) = 0.83 R
        assert row["r_multiple"] == pytest.approx(0.83)

    async def test_the_fill_rate_is_null_before_anything_has_resolved(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A book with three working limits and no history has no fill rate. Reporting 0% would
        be a claim about the market rather than a fact about an empty book."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/book"), headers=bearer(public_id))

        rate = response.json()["fill_rate"]
        assert rate["rate_pct"] is None
        assert rate["resolved"] == 0
        assert rate["modelled_pct"] == pytest.approx(PUBLISHED.modelled_fill_rate_pct)

    async def test_a_resolved_order_moves_the_rate(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §7.3: filled over resolved. A limit still working is neither — counting it as a
        miss would make every fresh order look like a failure."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _breadth(screener_session, user_id=user_id)
        for symbol, state in (("FILLED1", "FILLED"), ("GONE1", "EXPIRED"), ("LIVE1", "SENT")):
            screener_session.add(
                VbOrder(
                    user_id=user_id,
                    instrument_id=await _instrument(screener_session, symbol),
                    signal_date=AS_OF,
                    limit_price=Decimal("100.00"),
                    stop_price=Decimal("88.00"),
                    quantity=10,
                    state=state,
                )
            )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/book"), headers=bearer(public_id))

        rate = response.json()["fill_rate"]
        assert (rate["filled"], rate["resolved"]) == (1, 2)
        assert rate["rate_pct"] == pytest.approx(50.0)


class TestTheBacktestRoute:
    async def test_the_published_numbers_come_back_even_with_no_runs(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A page must be able to say what the study found before this box has re-run it."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/backtest"), headers=bearer(public_id))

        body = response.json()
        assert body["runs"] == []
        assert body["published"]["cagr_pct"] == pytest.approx(18.23)
        assert body["published"]["trades"] == 761
        assert body["published"]["out_of_sample_cagr_pct"] == pytest.approx(26.0)

    async def test_only_the_latest_finished_run_per_source_comes_back(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`03` §11 makes the table append-only, so the page's job is to show the newest run that
        **finished** — not the newest one that started."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        base = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
        # The real shape: `stats` is keyed by book, because `04` §11 reports three over one
        # detection pass and `full` is the primary one.
        for offset, stats in (
            (0, {"full": {"cagr_pct": 17.0}}),
            (1, {"full": {"cagr_pct": 18.2}}),
        ):
            screener_session.add(
                VbBacktestRun(
                    user_id=user_id,
                    source="PLANT",
                    params={"config": "default"},
                    started_at=base + dt.timedelta(days=offset),
                    finished_at=base + dt.timedelta(days=offset, hours=1),
                    stats=stats,
                )
            )
        screener_session.add(
            VbBacktestRun(
                user_id=user_id,
                source="PLANT",
                params={"config": "default"},
                started_at=base + dt.timedelta(days=2),
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/backtest"), headers=bearer(public_id))

        runs = response.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["stats"]["full"]["cagr_pct"] == pytest.approx(18.2)

    async def test_a_failed_re_run_does_not_displace_the_last_good_number(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`03` §8: a failed run sets `finished_at` too, so "latest finished" is not enough.

        Without the stats check, a re-run that raised would replace a real result with a card
        full of blanks — which is exactly what an append-only table exists to prevent.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        base = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)
        screener_session.add(
            VbBacktestRun(
                user_id=user_id,
                source="PLANT",
                params={"config": "default"},
                started_at=base,
                finished_at=base + dt.timedelta(hours=1),
                stats={"full": {"cagr_pct": 18.2}},
            )
        )
        screener_session.add(
            VbBacktestRun(
                user_id=user_id,
                source="PLANT",
                params={"config": "default"},
                started_at=base + dt.timedelta(days=1),
                finished_at=base + dt.timedelta(days=1, hours=1),
                error="RuntimeError: the plant fell over",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/backtest"), headers=bearer(public_id))

        runs = response.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["stats"]["full"]["cagr_pct"] == pytest.approx(18.2)
        assert runs[0]["error"] is None


class TestTheConfigRoutes:
    async def test_the_read_carries_the_ceilings_and_the_paper_gate(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/config"), headers=bearer(public_id))

        body = response.json()
        # 0 since 11 Sep 2026: Maulik withdrew the paper gate (DECISIONS-VB VB11.4). The
        # counter still counts; it is no longer a condition.
        assert body["dry_run_sessions_required"] == 0
        assert body["execution_enabled"] is False
        # Strings: the ceilings ride to the form as text so a Decimal ceiling of `15.0` and an
        # integer one render the same way and neither goes through a float on the way.
        assert body["ceilings"]["max_open_positions"] == "15"

    async def test_a_setting_above_its_ceiling_is_refused_and_changes_nothing(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            refused = await client.patch(
                url("/vbt/config"),
                json={"max_open_positions": 99},
                headers=bearer(public_id),
            )
            after = await client.get(url("/vbt/config"), headers=bearer(public_id))

        assert refused.status_code == 422
        assert after.json()["max_open_positions"] == 10

    async def test_a_system_owned_field_is_refused_rather_than_ignored(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`02` §3.1's counter is the system's. A caller answered 200 would believe they had
        declared the paper run finished.

        400 rather than 422 because the app maps a body that does not match its model to
        `invalid-screen-definition` (`app.py`'s `RequestValidationError` handler), and the field
        that was refused is named in `errors`. The status is the app's convention; what this test
        is about is that the request was **refused and named**, not silently dropped.
        """
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/vbt/config"), json={"dry_run_sessions": 20}, headers=bearer(public_id)
            )

        assert response.status_code == 400
        assert "dry_run_sessions" in response.text
