"""`/options/*` over HTTP against a real database (OP5, `docs/options/05` §2, `06` OP5's AC).

What each class is a regression for:

* **the empty tab says why** — with the collector and scan flags off (the default, and the box's
  state when OP5 shipped) `op_scan` is empty, and `empty_reason` must name the switch rather than
  let the page read as "the market offered nothing" (the TWT lesson, DECISIONS-TW TW14.1);
* **the rows the worker wrote are the rows served** — an O1 `WOULD_SKIP` with all its reasons, an
  O2 `ARMED` with its distance to trigger, an O3 candidate spread — with every money string passed
  through as the worker stored it (house rule 8);
* **the clock** — `live` only while the session is open *and* the rows are today's; `stale` beyond
  two minutes; outside hours the last scanned session, not live;
* **the calendar comes from `op_expiry`**, withdrawn expiries excluded, event days marked;
* **the two writes** — a person's event day added and removed, a seeded day refused; the settings
  patch refused atomically above a ceiling;
* **`GET /options/today` p95 < 200 ms** on the dev stack (`06` OP5 AC);
* **one person's book** — a principal who is not the sole tenant is refused.
"""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal
from typing import cast

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.routers import options as options_router
from baskfy_api.settings import Settings
from baskfy_core.market_hours_cb import IST
from baskfy_core.models import (
    OpBookConfig,
    OpChainSnapshot,
    OpEventDay,
    OpExpiry,
    OpScan,
    OpSleeveConfig,
)
from baskfy_core.models.base import JsonObject

pytestmark = [requires_db, pytest.mark.db]

TODAY = dt.date(2026, 9, 22)  # a Tuesday: a weekly expiry in the fixture master
MID_SESSION = dt.datetime(2026, 9, 22, 13, 14, 30, tzinfo=IST)
AFTER_CLOSE = dt.datetime(2026, 9, 22, 16, 5, tzinfo=IST)
SCAN_MINUTE = dt.datetime(2026, 9, 22, 13, 14, tzinfo=IST)

#: The next NIFTY expiries: Tuesdays, the last of each month the monthly.
EXPIRIES = (
    (dt.date(2026, 9, 22), "WEEKLY"),
    (dt.date(2026, 9, 29), "MONTHLY"),
    (dt.date(2026, 10, 6), "WEEKLY"),
    (dt.date(2026, 10, 13), "WEEKLY"),
    (dt.date(2026, 10, 20), "WEEKLY"),
    (dt.date(2026, 10, 27), "MONTHLY"),
    (dt.date(2026, 11, 3), "WEEKLY"),
)
WITHDRAWN = dt.date(2026, 10, 1)
RBI_DAY = dt.date(2026, 10, 6)


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


@pytest.fixture
def flags_on(seeded_url: str) -> Settings:
    return api_helpers.api_settings(
        seeded_url, options_collect_enabled=True, options_scan_enabled=True
    )


def _clock(monkeypatch: pytest.MonkeyPatch, now: dt.datetime, *, session_day: bool = True) -> None:
    async def _is_session_day(_session: object, _day: dt.date) -> bool:
        return session_day

    monkeypatch.setattr(options_router, "_now", lambda: now)
    monkeypatch.setattr(options_router, "is_session_day", _is_session_day)


async def _tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "options-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    return user_id, public_id


async def _calendar(session: AsyncSession, user_id: int) -> None:
    for day, kind in EXPIRIES:
        session.add(
            OpExpiry(
                underlying="NIFTY",
                expiry_date=day,
                kind=kind,
                lot_size=75,
                first_seen=dt.date(2026, 9, 1),
                seen_on=TODAY,
                detail={},
            )
        )
    session.add(
        OpExpiry(
            underlying="NIFTY",
            expiry_date=WITHDRAWN,
            kind="WEEKLY",
            lot_size=75,
            first_seen=dt.date(2026, 9, 1),
            seen_on=dt.date(2026, 9, 15),
            detail={"withdrawn_on": "2026-09-16"},
        )
    )
    session.add(
        OpEventDay(
            user_id=user_id,
            date=RBI_DAY,
            reason="RBI_POLICY",
            source="SEED",
            source_url="https://www.rbi.org.in/",
        )
    )
    await session.flush()


def _scan(  # noqa: PLR0913 - one row is its fields
    user_id: int,
    sleeve: str,
    state: str,
    *,
    reasons: list[str],
    numbers: dict[str, object],
    candidates: list[dict[str, object]] | None = None,
    day: dt.date = TODAY,
    minute: dt.datetime = SCAN_MINUTE,
) -> OpScan:
    return OpScan(
        user_id=user_id,
        sleeve=sleeve,
        trade_date=day,
        ts=minute,
        state=state,
        reasons=reasons,
        numbers=numbers,
        # The column is typed as a JSON object and holds a JSON array (OP4's note).
        candidates=cast("JsonObject", candidates or []),
        as_of_minute=minute,
        stale=False,
    )


O3_CANDIDATE: dict[str, object] = {
    "structure": "DEBIT_SPREAD",
    "expiry": "2026-09-22",
    "direction": "UP",
    "legs": [
        {
            "role": "LONG",
            "strike": "25100.00",
            "option_type": "CE",
            "side": "BUY",
            "bid": "61.20",
            "ask": "61.60",
            "iv": "0.118000",
            "delta": "0.520000",
            "limit_price": "61.60",
            "expiry": "2026-09-22",
            "instrument_token": 1,
        },
        {
            "role": "SHORT",
            "strike": "25200.00",
            "option_type": "CE",
            "side": "SELL",
            "bid": "34.45",
            "ask": "34.85",
            "iv": "0.121000",
            "delta": "0.330000",
            "limit_price": "34.45",
            "expiry": "2026-09-22",
            "instrument_token": 2,
        },
    ],
    "points": "27.15",
    "lot_size": 75,
    "lots": 1,
    "sizing_mode": "PAPER_ONE_LOT",
    "max_loss_inr": "2036.25",
    "round_trip_inr": "118.40",
    "cost_share": "0.0164",
    "rejection": None,
}


async def _the_morning(session: AsyncSession, user_id: int) -> None:
    """`06` OP5's AC fixture: O1 `WOULD_SKIP` with all its reasons, O2 `ARMED` with the distance
    to its trigger, an O3 candidate spread."""
    session.add_all(
        [
            _scan(
                user_id,
                "O1W",
                "WOULD_SKIP",
                reasons=["RANGE_TOO_WIDE", "NOT_CONTAINED", "ER_TOO_HIGH"],
                numbers={
                    "gap_pct": "0.2100",
                    "range_pct": "0.9400",
                    "range_max_pct": "0.80",
                    "er": "0.4100",
                    "er_max": "0.30",
                    "contained": False,
                },
            ),
            _scan(
                user_id,
                "O1M",
                "NOT_TODAY",
                reasons=["NOT_MONTHLY"],
                numbers={"next_date": "2026-09-29"},
            ),
            _scan(
                user_id,
                "O2",
                "ARMED",
                reasons=[],
                numbers={
                    "trend": "UP",
                    "trigger_level": "25162.55",
                    "last_close": "25131.10",
                    "distance_points": "31.45",
                    "distance_pct": "0.1251",
                },
            ),
            _scan(
                user_id,
                "O3A",
                "TRIGGERED",
                reasons=[],
                numbers={"range_pct": "0.6100"},
                candidates=[O3_CANDIDATE],
            ),
            _scan(
                user_id,
                "O3B",
                "DAY_SKIPPED",
                reasons=["GAP_TOO_SMALL"],
                numbers={"gap_pct": "0.1200"},
            ),
        ]
    )
    await session.flush()


class TestTheEmptyTabSaysWhy:
    async def test_with_the_collector_off_the_reason_is_the_collector(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        _clock(monkeypatch, MID_SESSION)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/options/today"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scans"] == []
        assert body["empty_reason"] == "collector_off"
        assert body["live"] is False
        assert body["collect_enabled"] is False
        assert body["scan_enabled"] is False
        assert all(gate["mode"] == "PAPER" for gate in body["gates"])

    async def test_with_the_flags_on_and_no_row_it_is_no_scan_yet(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        screener_session.add(
            _scan(
                user_id,
                "O2",
                "WINDOW_CLOSED",
                reasons=[],
                numbers={},
                day=dt.date(2026, 9, 21),
                minute=dt.datetime(2026, 9, 21, 15, 29, tzinfo=IST),
            )
        )
        await screener_session.flush()
        _clock(monkeypatch, dt.datetime(2026, 9, 22, 9, 5, tzinfo=IST))
        async with running_app(flags_on, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()
        assert body["empty_reason"] == "no_scan_yet_today"
        assert body["scan_date"] == "2026-09-21", "yesterday's rows are shown, dated, not live"
        assert body["live"] is False


class TestTheRowsTheWorkerWrote:
    async def test_the_ac_morning_is_served_as_written(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        await _the_morning(screener_session, user_id)
        _clock(monkeypatch, MID_SESSION)
        async with running_app(flags_on, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()

        assert body["empty_reason"] is None
        assert [s["sleeve"] for s in body["scans"]] == ["O1M", "O1W", "O2", "O3A", "O3B"]
        by = {s["sleeve"]: s for s in body["scans"]}
        assert by["O1W"]["state"] == "WOULD_SKIP"
        assert by["O1W"]["reasons"] == ["RANGE_TOO_WIDE", "NOT_CONTAINED", "ER_TOO_HIGH"]
        assert by["O2"]["state"] == "ARMED"
        assert by["O2"]["numbers"]["distance_points"] == "31.45"
        assert by["O2"]["numbers"]["distance_pct"] == "0.1251"
        spread = by["O3A"]["candidates"][0]
        assert spread["points"] == "27.15"
        assert [leg["strike"] for leg in spread["legs"]] == ["25100.00", "25200.00"]
        assert body["live"] is True
        assert body["stale"] is False

    async def test_the_newest_row_per_sleeve_wins(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        earlier = SCAN_MINUTE - dt.timedelta(minutes=5)
        screener_session.add_all(
            [
                _scan(user_id, "O2", "BUILDING_RANGE", reasons=[], numbers={}, minute=earlier),
                _scan(user_id, "O2", "ARMED", reasons=[], numbers={}),
            ]
        )
        await screener_session.flush()
        _clock(monkeypatch, MID_SESSION)
        async with running_app(flags_on, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()
            history = (await client.get(url("/options/scan/O2"), headers=bearer(public_id))).json()
        assert [s["state"] for s in body["scans"]] == ["ARMED"]
        assert [r["state"] for r in history["rows"]] == ["BUILDING_RANGE", "ARMED"]

    async def test_an_unknown_sleeve_is_a_404(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _tenant(screener_session, monkeypatch)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/options/scan/BANKNIFTY"), headers=bearer(public_id))
        assert response.status_code == 404


class TestTheClock:
    async def test_stale_beyond_two_minutes(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _the_morning(screener_session, user_id)
        _clock(monkeypatch, SCAN_MINUTE + dt.timedelta(minutes=2, seconds=30))
        async with running_app(flags_on, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()
        assert body["live"] is True
        assert body["stale"] is True

    async def test_after_the_close_it_is_as_of_close_not_live(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _the_morning(screener_session, user_id)
        _clock(monkeypatch, AFTER_CLOSE)
        async with running_app(flags_on, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()
        assert body["market_open"] is False
        assert body["live"] is False
        assert body["stale"] is False
        assert body["scan_date"] == TODAY.isoformat()
        assert body["as_of_minute"].startswith("2026-09-22T07:44"), "13:14 IST, as UTC"


class TestTheCalendar:
    async def test_the_header_reads_op_expiry(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        _clock(monkeypatch, MID_SESSION)
        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/options/today"), headers=bearer(public_id))).json()
        dates = [e["expiry_date"] for e in body["expiries"]]
        assert dates == [d.isoformat() for d, _ in EXPIRIES[:6]], "six, withdrawn excluded"
        rbi = next(e for e in body["expiries"] if e["expiry_date"] == RBI_DAY.isoformat())
        assert rbi["event_day"] is True
        assert rbi["event_reason"] == "RBI_POLICY"
        roles = {r["sleeve"]: r for r in body["roles"]}
        assert roles["O1W"]["today"] is True
        assert roles["O1M"]["today"] is False
        assert roles["O1M"]["reason"] == "NOT_MONTHLY"
        assert roles["O1M"]["next_date"] == "2026-09-29"
        assert roles["O2"]["today"] is True
        assert roles["O3A"]["today"] is True
        # 6 Oct is an event day, so O1-W's next is 13 Oct, not 6 Oct (04 §1.3: skipped, not moved)
        assert roles["O1W"]["next_date"] == "2026-10-13"

    async def test_the_year_view(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        _clock(monkeypatch, MID_SESSION)
        async with running_app(settings, screener_session) as client:
            body = (
                await client.get(url("/options/calendar?year=2026"), headers=bearer(public_id))
            ).json()
        assert len(body["expiries"]) == len(EXPIRIES)
        assert body["event_days"][0]["removable"] is False


class TestTheTwoWrites:
    async def test_a_person_adds_and_removes_their_own_event_day(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        day = "2026-10-20"
        async with running_app(settings, screener_session) as client:
            added = await client.post(
                url("/options/event-day"),
                json={"date": day, "note": "state election results"},
                headers=bearer(public_id),
            )
            again = await client.post(
                url("/options/event-day"), json={"date": day}, headers=bearer(public_id)
            )
            seeded = await client.delete(
                url(f"/options/event-day?date={RBI_DAY.isoformat()}"), headers=bearer(public_id)
            )
            removed = await client.delete(
                url(f"/options/event-day?date={day}"), headers=bearer(public_id)
            )
            missing = await client.delete(
                url(f"/options/event-day?date={day}"), headers=bearer(public_id)
            )
        assert added.status_code == 201, added.text
        assert added.json()["source"] == "USER"
        assert added.json()["reason"] == "MANUAL"
        assert again.status_code == 400
        assert seeded.status_code == 400, "a seeded, source-verified day is not removable"
        assert removed.status_code == 204
        assert missing.status_code == 404
        still = await screener_session.get(OpEventDay, (user_id, RBI_DAY))
        assert still is not None

    async def test_an_unknown_reason_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _tenant(screener_session, monkeypatch)
        async with running_app(settings, screener_session) as client:
            response = await client.post(
                url("/options/event-day"),
                json={"date": "2026-10-20", "reason": "HUNCH"},
                headers=bearer(public_id),
            )
        assert response.status_code == 400

    async def test_an_unseeded_config_says_so(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _tenant(screener_session, monkeypatch)
        async with running_app(settings, screener_session) as client:
            body = (await client.get(url("/options/config"), headers=bearer(public_id))).json()
        assert body["seeded"] is False
        assert body["book"] is None
        assert body["ceilings"]["risk_pct_max"] == "1.0"
        keys = {(t["section"], t["key"]) for t in body["thresholds"]}
        assert ("condor_monthly", "er_max") in keys
        assert ("directional", "vix_max") in keys

    async def test_a_ceiling_breach_is_refused_atomically(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        screener_session.add(OpBookConfig(user_id=user_id, updated_by="test"))
        for group in ("O1M", "O1W", "O2", "O3"):
            screener_session.add(
                OpSleeveConfig(
                    user_id=user_id,
                    sleeve=group,
                    sleeve_capital_inr=Decimal("0"),
                    risk_per_trade_pct=Decimal("0.50"),
                    max_lots=2,
                    paper_enabled=True,
                    hard_exit_time=dt.time(14, 30),
                    updated_by="test",
                )
            )
        await screener_session.flush()
        async with running_app(settings, screener_session) as client:
            refused = await client.patch(
                url("/options/config"),
                json={"book": {"account_inr": "500000"}, "sleeves": {"O2": {"max_lots": 99}}},
                headers=bearer(public_id),
            )
            accepted = await client.patch(
                url("/options/config"),
                json={"sleeves": {"O2": {"max_lots": 3}}},
                headers=bearer(public_id),
            )
            paused = await client.patch(
                url("/options/config"),
                json={"book": {"paused_until": "2026-12-31"}},
                headers=bearer(public_id),
            )
        assert refused.status_code == 422, refused.text
        assert refused.json()["env_var"] == "BASKFY_OPTIONS_MAX_LOTS_MAX"
        book = (
            await screener_session.execute(
                select(OpBookConfig).where(OpBookConfig.user_id == user_id)
            )
        ).scalar_one()
        assert book.account_inr == Decimal("0"), "the first part must not have been written"
        assert accepted.status_code == 200, accepted.text
        o2 = next(s for s in accepted.json()["sleeves"] if s["sleeve"] == "O2")
        assert o2["max_lots"] == 3
        # This API answers an unknown field as a 400 (docs/07's validation convention): the pause
        # is the risk ledger's (OP11), and the patch has no field for it at all.
        assert paused.status_code == 400, "a pause is the risk ledger's, not a field here"
        assert book.paused_until is None


class TestTheOtherReads:
    async def test_every_read_answers_an_empty_database_honestly(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _tenant(screener_session, monkeypatch)
        _clock(monkeypatch, MID_SESSION)
        async with running_app(settings, screener_session) as client:
            chain = (await client.get(url("/options/chain"), headers=bearer(public_id))).json()
            positions = (
                await client.get(url("/options/positions"), headers=bearer(public_id))
            ).json()
            sessions = (
                await client.get(url("/options/sessions"), headers=bearer(public_id))
            ).json()
            journal = (await client.get(url("/options/journal"), headers=bearer(public_id))).json()
            backtest = (
                await client.get(url("/options/backtest"), headers=bearer(public_id))
            ).json()
        assert chain == {"expiries": []}
        assert positions == {"positions": [], "closed_today": []}
        assert sessions == {"sessions": []}
        assert journal["summaries"] == []
        assert [p["group"] for p in journal["progress"]] == ["O1M", "O1W", "O2", "O3"]
        assert [p["sessions_needed"] for p in journal["progress"]] == [6, 12, 60, 20]
        assert backtest["runs"] == []
        assert backtest["reason"]


class TestItIsFastAndItIsOnePersons:
    async def test_today_p95_under_200ms(
        self, flags_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`06` OP5 AC: `GET /options/today` p95 < 200 ms on the dev stack, with a full
        morning's rows and calendar in place."""
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        await _the_morning(screener_session, user_id)
        _clock(monkeypatch, MID_SESSION)
        timings: list[float] = []
        async with running_app(flags_on, screener_session) as client:
            await client.get(url("/options/today"), headers=bearer(public_id))  # warm
            for _ in range(40):
                start = time.perf_counter()
                response = await client.get(url("/options/today"), headers=bearer(public_id))
                timings.append(time.perf_counter() - start)
                assert response.status_code == 200
        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        assert p95 < 0.200, f"p95 {p95 * 1000:.1f} ms"

    async def test_a_principal_who_is_not_the_sole_tenant_is_refused(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _tenant(screener_session, monkeypatch)
        _, intruder = await make_user(screener_session, "options-intruder@example.com")
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/options/today"), headers=bearer(intruder))
            anonymous = await client.get(url("/options/today"))
        assert response.status_code == 404, response.text
        assert anonymous.status_code == 401


class TestTheChain:
    async def test_strikes_around_atm_with_oi_change_and_pcr(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The collector's latest minute for the expiry, ΔOI against the day's first minute,
        ATM from the parity forward, and the put-call OI ratio as a number."""
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _calendar(screener_session, user_id)
        opening = dt.datetime(2026, 9, 22, 9, 16, tzinfo=IST)
        for ts, oi_step in ((opening, 0), (SCAN_MINUTE, 500)):
            for strike in ("25000", "25100", "25200"):
                for option_type, base in (("CE", 1000), ("PE", 1500)):
                    screener_session.add(
                        OpChainSnapshot(
                            ts=ts,
                            instrument_token=int(strike) * 10 + (1 if option_type == "CE" else 2),
                            expiry=TODAY,
                            strike=Decimal(strike),
                            option_type=option_type,
                            spot=Decimal("25120.00"),
                            forward=Decimal("25118.40"),
                            bid=Decimal("40.00"),
                            ask=Decimal("40.50"),
                            last=Decimal("40.25"),
                            oi=base + oi_step,
                            iv=Decimal("0.120000"),
                            delta=Decimal("0.500000"),
                        )
                    )
        await screener_session.flush()
        _clock(monkeypatch, MID_SESSION)
        async with running_app(settings, screener_session) as client:
            body = (
                await client.get(url(f"/options/chain?expiry={TODAY}"), headers=bearer(public_id))
            ).json()
        [expiry] = body["expiries"]
        assert expiry["atm_strike"] == "25100.00"
        assert expiry["ts"].startswith("2026-09-22T07:44")
        assert len(expiry["rows"]) == 6
        assert {row["oi_change"] for row in expiry["rows"]} == {500}
        assert expiry["pcr_oi"] == "1.3333"
