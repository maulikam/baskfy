"""SW8's contract: `GET /swing/journal`, over HTTP, against a real database.

The shape is contract C2's, and a sibling page renders exactly it. The claims worth asserting are
the ones `docs/swing/04` §10 and `05` §2 make and a reader would be surprised to find false:

* **real and simulated closes are two cards, never one** — a simulated close never appears in
  the real card, whichever way the execution flag is set;
* **an empty journal is zeros, not an error** — the surface exists before the first trade does;
* **the histogram is exactly the six buckets, in order, always** — a bar with no trades is a
  zero, not an absent key, and the boundaries mean what the labels say;
* **the ladder card is the loop closed** — its ``level`` is `sw_config.exposure_level`, the
  number the EOD job wrote, with the last five R values and which book they came from;
* **the session count is against twenty** (`02` §3.2);
* **numbers keep their stored precision** — `2.50` arrives as `2.50`;
* **the book belongs to one person** (M43.4).
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
from baskfy_api.swing_journal import HISTOGRAM_BUCKETS, PAPER_SESSIONS_REQUIRED, bucket_of
from baskfy_core.models import Instrument, SwConfig, SwMarketDaily, SwPosition, SwSession
from baskfy_core.seed_data import NSE_EXCHANGE_ID

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)

ZERO_STATS = {
    "trades": 0,
    "win_rate_pct": 0,
    "avg_win_r": 0,
    "avg_loss_r": 0,
    "expectancy_r": 0,
    "profit_factor": None,
    "net_r": 0,
    "largest_win_r": 0,
    "largest_loss_r": 0,
    "current_loss_streak": 0,
}


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


async def _sole_tenant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, rung: int = 0
) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "swing-journal@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(SwConfig(user_id=user_id, exposure_level=rung, updated_by="test"))
    await session.flush()
    return user_id, public_id


async def _closed(  # noqa: PLR0913 - one keyword per column a test may set
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    r: str | None,
    closed_on: dt.date = AS_OF,
    setup: str = "FLAG",
    simulated: bool = True,
    close_reason: str = "CLOSE_BELOW_TRAIL_MA",
) -> None:
    """Entry 100, initial stop 96 — one R is 4 — so `exit_avg` and `pnl_inr` follow from ``r``."""
    instrument_id = await _instrument(session, symbol)
    r_multiple = None if r is None else Decimal(r)
    exit_avg = None if r_multiple is None else Decimal(100) + r_multiple * 4
    session.add(
        SwPosition(
            user_id=user_id,
            instrument_id=instrument_id,
            setup=setup,
            entry_date=closed_on - dt.timedelta(days=5),
            entry_avg=Decimal("100.0000"),
            quantity_entered=50,
            initial_stop=Decimal("96.00"),
            stop=Decimal("96.00"),
            gtt_id=None,
            trail="MA20",
            partial_done=False,
            quantity_open=0,
            state="CLOSED",
            closed_on=closed_on,
            exit_avg=exit_avg,
            close_reason=close_reason,
            r_multiple=r_multiple,
            pnl_inr=None if r_multiple is None else r_multiple * 4 * 50,
            simulated=simulated,
        )
    )
    await session.flush()


async def _market(
    session: AsyncSession, *, user_id: int, gate: str = "GREEN", level: int = 1
) -> None:
    session.add(
        SwMarketDaily(
            user_id=user_id,
            date=AS_OF,
            constituent_count=40,
            pct_up_strong_1m=Decimal("8.0000"),
            gate=gate,
            exposure_level=level,
            max_open_positions=4,
            max_exposure_pct=Decimal("50.00"),
            new_entries_allowed=gate != "RED",
            parabolic_count=0,
            detail={},
        )
    )
    await session.flush()


async def _sessions(session: AsyncSession, *, user_id: int, count: int) -> None:
    for offset in range(count):
        session.add(
            SwSession(
                user_id=user_id,
                session_date=AS_OF - dt.timedelta(days=offset),
                mode="DRY_RUN",
                monitor_ran=False,
                plan_ids={"plans": []},
                notes="test",
            )
        )
    await session.flush()


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


class TestTheShape:
    async def test_an_empty_journal_is_zeros_not_an_error(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the first trade, before the first detection run: the whole C2 shape, with
        nothing in it, and a 200."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"real", "simulated", "sessions", "ladder", "backtest"}
        for card in (body["real"], body["simulated"]):
            assert card["stats"] == ZERO_STATS
            assert card["histogram"] == [{"bucket": b, "count": 0} for b in HISTOGRAM_BUCKETS]
            assert card["by_setup"] == []
            assert card["by_month"] == []
            assert card["trades"] == []
        assert body["sessions"] == {"logged": 0, "required": PAPER_SESSIONS_REQUIRED}
        assert body["ladder"] == {
            "level": 0,
            "gate": "UNKNOWN",
            "max_open_positions": 2,
            "max_exposure_pct": 25.00,
            "new_entries_allowed": False,
            "last_r": [],
            "reads": "SIMULATED",
        }
        assert body["backtest"] is None

    async def test_simulated_and_real_closes_are_separate_cards(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §10: "Simulated fills are summarised separately from real ones on the page"."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="PAPER", r="2.00", simulated=True)
        await _closed(screener_session, user_id=user_id, symbol="REAL", r="-1.00", simulated=False)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        body = response.json()
        assert [row["symbol"] for row in body["simulated"]["trades"]] == ["PAPER"]
        assert [row["symbol"] for row in body["real"]["trades"]] == ["REAL"]
        assert body["simulated"]["stats"]["trades"] == 1
        assert body["simulated"]["stats"]["net_r"] == 2.00
        assert body["real"]["stats"]["trades"] == 1
        assert body["real"]["stats"]["net_r"] == -1.00
        assert body["real"]["stats"]["current_loss_streak"] == 1

    async def test_the_histogram_is_the_six_buckets_in_order_and_the_labels_mean_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`-1.00` — a stop hit exactly, the commonest loss there is — sits in `-1..0`, not under
        `<-1`; a `3.00` sits in `2..3`, not under `>3`; a scratch at `0.00` is `0..1`."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        rs = ("-1.50", "-1.00", "-0.25", "0.00", "0.50", "1.00", "1.99", "2.50", "3.00", "4.00")
        for index, r in enumerate(rs):
            await _closed(screener_session, user_id=user_id, symbol=f"T{index}", r=r)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        histogram = response.json()["simulated"]["histogram"]
        assert [bar["bucket"] for bar in histogram] == list(HISTOGRAM_BUCKETS)
        assert [bar["count"] for bar in histogram] == [1, 2, 2, 2, 2, 1]

    def test_the_bucket_boundaries(self) -> None:
        assert bucket_of(Decimal("-1.01")) == "<-1"
        assert bucket_of(Decimal("-1.00")) == "-1..0"
        assert bucket_of(Decimal("0.00")) == "0..1"
        assert bucket_of(Decimal("1.00")) == "1..2"
        assert bucket_of(Decimal("2.00")) == "2..3"
        assert bucket_of(Decimal("3.00")) == "2..3"
        assert bucket_of(Decimal("3.01")) == ">3"

    async def test_by_setup_and_by_month_group_the_closes(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trade belongs to the month it was **closed** in; months come back in order, setups
        alphabetically, and every number is two-place R."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        july = dt.date(2026, 7, 20)
        await _closed(screener_session, user_id=user_id, symbol="F1", r="2.00", closed_on=july)
        await _closed(screener_session, user_id=user_id, symbol="F2", r="-1.00", closed_on=AS_OF)
        await _closed(
            screener_session, user_id=user_id, symbol="E1", r="3.50", closed_on=AS_OF, setup="EP"
        )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        card = response.json()["simulated"]
        assert card["by_setup"] == [
            {"setup": "EP", "trades": 1, "net_r": 3.50, "expectancy_r": 3.50},
            {"setup": "FLAG", "trades": 2, "net_r": 1.00, "expectancy_r": 0.50},
        ]
        assert card["by_month"] == [
            {"month": "2026-07", "trades": 1, "net_r": 2.00},
            {"month": "2026-08", "trades": 2, "net_r": 2.50},
        ]

    async def test_profit_factor_is_null_without_a_loss(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`04` §10: "profit factor (gross win R / gross loss R, null when no losses)". Not
        infinity, not zero — there is nothing to divide by, and the page says so."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="W1", r="2.00")
        await _closed(screener_session, user_id=user_id, symbol="W2", r="1.00")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        stats = response.json()["simulated"]["stats"]
        assert stats["profit_factor"] is None
        assert stats["win_rate_pct"] == 100.00
        assert stats["avg_win_r"] == 1.50

    async def test_the_statistics_are_the_journal_module_s(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Five closes, worked by hand against `04` §10: wins 2.00, 1.50, 2.50 (gross 6.00);
        losses -1.00, -0.50 (gross 1.50); expectancy 4.50 / 5 = 0.90; profit factor 4.00."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        first = AS_OF - dt.timedelta(days=4)
        for index, r in enumerate(("2.00", "-1.00", "1.50", "-0.50", "2.50")):
            await _closed(
                screener_session,
                user_id=user_id,
                symbol=f"T{index}",
                r=r,
                closed_on=first + dt.timedelta(days=index),
            )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        assert response.json()["simulated"]["stats"] == {
            "trades": 5,
            "win_rate_pct": 60.00,
            "avg_win_r": 2.00,
            "avg_loss_r": -0.75,
            "expectancy_r": 0.90,
            "profit_factor": 4.00,
            "net_r": 4.50,
            "largest_win_r": 2.50,
            "largest_loss_r": -1.00,
            "current_loss_streak": 0,
        }

    async def test_the_loss_streak_is_counted_from_the_latest_close_backwards(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A win, then two losses: the streak is 2. The same three closes the other way round
        would be 0 — the order they closed in is the order that counts."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        first = AS_OF - dt.timedelta(days=2)
        for index, r in enumerate(("2.00", "-1.00", "-0.50")):
            await _closed(
                screener_session,
                user_id=user_id,
                symbol=f"T{index}",
                r=r,
                closed_on=first + dt.timedelta(days=index),
            )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        stats = response.json()["simulated"]["stats"]
        assert stats["current_loss_streak"] == 2
        assert stats["largest_loss_r"] == -1.00

    async def test_trades_come_newest_first_with_their_stored_numbers(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(
            screener_session,
            user_id=user_id,
            symbol="OLD",
            r="1.00",
            closed_on=AS_OF - dt.timedelta(days=3),
        )
        await _closed(
            screener_session,
            user_id=user_id,
            symbol="NEW",
            r="2.50",
            closed_on=AS_OF,
            close_reason="HARD_STOP_HIT",
        )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        trades = response.json()["simulated"]["trades"]
        assert [row["symbol"] for row in trades] == ["NEW", "OLD"]
        assert trades[0] == {
            "symbol": "NEW",
            "setup": "FLAG",
            "entry_date": (AS_OF - dt.timedelta(days=5)).isoformat(),
            "exit_date": AS_OF.isoformat(),
            "entry": 100.0000,
            "initial_stop": 96.00,
            "exit_avg": 110.0000,
            "quantity": 50,
            "r_multiple": 2.50,
            "pnl_inr": 500.00,
            "close_reason": "HARD_STOP_HIT",
        }

    async def test_a_price_keeps_the_precision_it_was_stored_with(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 8. `2.50` R and `96.00` are the stored numbers; the wire carries them."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="NEW", r="2.50")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        text = response.text.replace(" ", "")
        assert '"r_multiple":2.50' in text
        assert '"initial_stop":96.00' in text
        assert '"net_r":2.50' in text

    async def test_a_closed_row_without_an_r_multiple_is_not_a_trade(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A close-out that never finished writing is not a result; counting it as a zero would
        put a trade with no outcome into the expectancy."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="DONE", r="2.00")
        await _closed(screener_session, user_id=user_id, symbol="HALF", r=None)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        card = response.json()["simulated"]
        assert card["stats"]["trades"] == 1
        assert [row["symbol"] for row in card["trades"]] == ["DONE"]


class TestTheLadderCard:
    async def test_it_shows_the_rung_in_force_and_the_last_five_r(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`level` is `sw_config.exposure_level` — the loop closed — with the gate and the tier
        from the market row, and the last `lookback_trades` closes oldest first."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch, rung=1)
        await _market(screener_session, user_id=user_id, gate="GREEN", level=1)
        first = AS_OF - dt.timedelta(days=5)
        for index, r in enumerate(("-3.00", "2.00", "-1.00", "1.50", "-0.50", "2.50")):
            await _closed(
                screener_session,
                user_id=user_id,
                symbol=f"T{index}",
                r=r,
                closed_on=first + dt.timedelta(days=index),
            )

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        assert response.json()["ladder"] == {
            "level": 1,
            "gate": "GREEN",
            "max_open_positions": 4,
            "max_exposure_pct": 50.00,
            "new_entries_allowed": True,
            "last_r": [2.00, -1.00, 1.50, -0.50, 2.50],
            "reads": "SIMULATED",
        }

    async def test_it_reads_the_paper_book_while_execution_is_off(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PACK.6. A real close is in the real card and nowhere near the ladder."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="REAL", r="2.00", simulated=False)
        await _closed(screener_session, user_id=user_id, symbol="PAPER", r="-1.00", simulated=True)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        ladder = response.json()["ladder"]
        assert ladder["reads"] == "SIMULATED"
        assert ladder["last_r"] == [-1.00]

    async def test_it_reads_the_real_book_once_execution_is_on(
        self, seeded_url: str, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _closed(screener_session, user_id=user_id, symbol="REAL", r="2.00", simulated=False)
        await _closed(screener_session, user_id=user_id, symbol="PAPER", r="-1.00", simulated=True)
        enabled = api_helpers.api_settings(seeded_url, swing_execution_enabled=True)

        async with running_app(enabled, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        ladder = response.json()["ladder"]
        assert ladder["reads"] == "REAL"
        assert ladder["last_r"] == [2.00]

    async def test_a_red_gate_shows_entries_disallowed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch, rung=0)
        await _market(screener_session, user_id=user_id, gate="RED", level=0)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        ladder = response.json()["ladder"]
        assert ladder["gate"] == "RED"
        assert ladder["new_entries_allowed"] is False
        assert ladder["level"] == 0


class TestTheSessionCount:
    async def test_it_counts_against_twenty(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`02` §3.2, `05` §2: "14 of 20 paper sessions logged"."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _sessions(screener_session, user_id=user_id, count=14)

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(public_id))

        assert response.json()["sessions"] == {"logged": 14, "required": 20}
        assert PAPER_SESSIONS_REQUIRED == 20


class TestTheBookBelongsToOnePerson:
    async def test_another_account_is_refused_rather_than_served(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, _ = await _sole_tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "intruder@example.com")
        await _closed(screener_session, user_id=user_id, symbol="MINE", r="2.00")

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"), headers=bearer(intruder))

        assert response.status_code == 404

    async def test_an_anonymous_caller_is_unauthenticated(
        self, settings: Settings, screener_session: AsyncSession
    ) -> None:
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/journal"))

        assert response.status_code == 401
