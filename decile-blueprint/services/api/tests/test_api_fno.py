"""`/fno/*` over HTTP against a real database (FO5, `docs/fno/05` §2-§3, `04` §5, `06` FO5).

What each class is a regression for:

* **the empty page says why** — with `BASKFY_FNO_SCAN_ENABLED` off (the default) `fo_scan` is
  empty, and `empty_reason` names the switch rather than letting the page read as "nothing today"
  (the TWT lesson, DECISIONS-TW TW14.1);
* **the overnight page serves what the scan wrote** — F1 per underlying with the next entry date
  and the proposed condor passed through as the worker stored it (house rule 8), the open
  structure with its settle mark and its R, the "hard exit tomorrow" line, the paper journal never
  pooled, the evidence card with its Tier 2E caveat verbatim (`07` §4), and F2's candidates and
  `REJECTED_SIZE` rows only;
* **the information table is `04` §5's columns** — 1-year IV percentile from stored `iv_atm`, the
  5-session OI change, days to the near monthly and its lot from the master, the ban flag, the
  default sort by futures turnover; the verdict table with the latest re-test beside its row; the
  spread sample's sessions; the MISSING nights;
* **the settings PATCH** — audited to `fo_config_audit`; a value above a ceiling refused with the
  ceiling named and nothing written (atomic across the parts); no pause field.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import fno_read
from baskfy_api.routers import fno as fno_router
from baskfy_api.settings import Settings
from baskfy_core.market_hours_cb import IST
from baskfy_core.models import (
    FoBacktestRun,
    FoBookConfig,
    FoConfigAudit,
    FoContractDaily,
    FoIngestDay,
    FoJournal,
    FoMark,
    FoPosition,
    FoScan,
    FoSleeveConfig,
    FoSpreadSample,
    FoUnderlyingDaily,
    OpExpiry,
)

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 9, 22)  # the scanned session (a Tuesday)
NOW = dt.datetime(2026, 9, 23, 13, 14, tzinfo=IST)
NEXT_SESSION = dt.date(2026, 9, 24)


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


@pytest.fixture
def scan_on(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url, fno_scan_enabled=True)


def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _next(_session: object, _day: dt.date) -> dt.date:
        return NEXT_SESSION

    monkeypatch.setattr(fno_router, "_now", lambda: NOW)
    monkeypatch.setattr(fno_read, "next_session_after", _next)


async def _tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "fno-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    return user_id, public_id


async def _config(session: AsyncSession, user_id: int) -> None:
    session.add(FoBookConfig(user_id=user_id, monthly_pause_inr=Decimal(0)))
    session.add_all(
        [
            FoSleeveConfig(
                user_id=user_id,
                sleeve="F1",
                capital_inr=Decimal("2500000"),
                risk_per_trade_pct=Decimal("1.00"),
                max_lots=2,
                max_open_positions=2,
                paper_enabled=True,
            ),
            FoSleeveConfig(
                user_id=user_id,
                sleeve="F2",
                capital_inr=Decimal(0),
                risk_per_trade_pct=Decimal("1.00"),
                max_lots=2,
                max_open_positions=5,
                paper_enabled=True,
            ),
            # F3 (DECISIONS-FO M.5): the settings read walks every group, so the row must exist.
            FoSleeveConfig(
                user_id=user_id,
                sleeve="F3",
                capital_inr=Decimal(0),
                risk_per_trade_pct=Decimal("1.00"),
                max_lots=2,
                max_open_positions=2,
                paper_enabled=True,
            ),
        ]
    )
    await session.flush()


def _scan(user_id: int, sleeve: str, symbol: str, state: str, detail: dict[str, object]) -> FoScan:
    return FoScan(
        user_id=user_id,
        sleeve=sleeve,
        trade_date=AS_OF,
        symbol=symbol,
        state=state,
        reasons=[f"{state} for {symbol}"],
        detail=detail,
    )


CONDOR_DETAIL: dict[str, object] = {
    "underlying": "BANKNIFTY",
    "entry_session": "2026-09-23",
    "hard_exit_date": "2026-10-26",
    "legs": [
        {"entry_seq": 1, "role": "LONG_PUT", "strike": "51000", "option_type": "PE"},
        {"entry_seq": 2, "role": "LONG_CALL", "strike": "56500", "option_type": "CE"},
        {"entry_seq": 3, "role": "SHORT_PUT", "strike": "52000", "option_type": "PE"},
        {"entry_seq": 4, "role": "SHORT_CALL", "strike": "55500", "option_type": "CE"},
    ],
    "credit_points": "212.35",
    "max_loss_per_lot_inr": "23629.50",
    "iv_rv": "1.2345",
    "iv_rv_note": "recorded, not used",
}


F3_NO_SIGNAL_DETAIL: dict[str, object] = {
    "underlying": "NIFTY",
    "sleeve": "F3N",
    "next_session": "2026-09-23",
    "direction_daily": "DOWN",
    "confirm": {"agrees": False, "message": "the 75-minute bar disagrees"},
}
F3_CANDIDATE_DETAIL: dict[str, object] = {
    "underlying": "BANKNIFTY",
    "sleeve": "F3B",
    "next_session": "2026-09-23",
    "entry_session": "2026-09-23",
    "direction": "UP",
    "level": "54210.00",
    "expiry": "2026-09-29",
    "expiry_kind": "monthly",
    "legs": [
        {"entry_seq": 1, "role": "LONG_PUT", "strike": "52300", "option_type": "PE"},
        {"entry_seq": 2, "role": "SHORT_PUT", "strike": "53400", "option_type": "PE"},
    ],
    "credit_points": "31.45",
    "max_loss_per_lot_inr": "32119.50",
}


async def _book(session: AsyncSession, user_id: int) -> int:
    """F1 and F2 scans, one open F1 structure with a mark, closed paper trades, re-test rows."""
    session.add_all(
        [
            _scan(
                user_id,
                "F1N",
                "NIFTY",
                "NOT_ENTRY_DAY",
                {"underlying": "NIFTY", "next_entry_date": "2026-10-06"},
            ),
            _scan(user_id, "F1B", "BANKNIFTY", "CANDIDATE", CONDOR_DETAIL),
            _scan(user_id, "F2", "RELIANCE", "CANDIDATE", {"breakout_level": "1450.00"}),
            _scan(user_id, "F2", "TCS", "REJECTED_SIZE", {"risk_per_lot_inr": "61250.00"}),
            _scan(user_id, "F2", "INFY", "NO_SIGNAL", {}),
            _scan(user_id, "F3N", "NIFTY", "NO_SIGNAL", F3_NO_SIGNAL_DETAIL),
            _scan(user_id, "F3B", "BANKNIFTY", "CANDIDATE", F3_CANDIDATE_DETAIL),
        ]
    )
    session.add(
        FoContractDaily(
            trade_date=AS_OF,
            instrument="FUTIDX",
            symbol="NIFTY",
            expiry=dt.date(2026, 9, 29),
            strike=Decimal(0),
            option_type="XX",
            close=Decimal("25130.00"),
            settle=Decimal("25131.10"),
            underlying=Decimal("25100.50"),
            lot_size=65,
            source_key="test",
        )
    )
    opened = dt.datetime(2026, 9, 3, 9, 25, tzinfo=IST)
    open_f1 = FoPosition(
        user_id=user_id,
        sleeve="F1N",
        symbol="NIFTY",
        structure="IRON_CONDOR",
        entry_plan_id="F1N-2026-09-03",
        legs={},
        lots=1,
        lot_size=65,
        entry_credit=Decimal("98.40"),
        max_loss_inr=Decimal("20000.00"),
        hard_exit_date=NEXT_SESSION,
        opened_at=opened,
        simulated=True,
    )
    closed_f1 = FoPosition(
        user_id=user_id,
        sleeve="F1B",
        symbol="BANKNIFTY",
        structure="IRON_CONDOR",
        entry_plan_id="F1B-2026-08-05",
        legs={},
        lots=1,
        lot_size=30,
        opened_at=dt.datetime(2026, 8, 5, 9, 25, tzinfo=IST),
        closed_at=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST),
        closed_reason="PROFIT_TAKE",
        simulated=True,
    )
    closed_f2 = FoPosition(
        user_id=user_id,
        sleeve="F2",
        symbol="LT",
        structure="FUTURE",
        entry_plan_id="F2-2026-07-01-LT",
        legs={},
        lots=1,
        lot_size=150,
        opened_at=dt.datetime(2026, 7, 1, 9, 25, tzinfo=IST),
        closed_at=dt.datetime(2026, 8, 25, 15, 0, tzinfo=IST),
        closed_reason="TRAIL_STOP",
        simulated=True,
    )
    session.add_all([open_f1, closed_f1, closed_f2])
    await session.flush()
    session.add(
        FoMark(
            position_id=open_f1.id,
            trade_date=AS_OF,
            user_id=user_id,
            mark_points=Decimal("80.00"),
            pnl_inr=Decimal("1196.00"),
            detail={},
        )
    )
    session.add_all(
        [
            _journal(user_id, closed_f1, "IRON_CONDOR", Decimal("0.42"), rolls=0),
            _journal(user_id, closed_f2, "FUTURE", Decimal("-1.00"), rolls=1),
        ]
    )
    for run_at, net in (
        (dt.datetime(2026, 7, 1, tzinfo=dt.UTC), "0.0300"),
        (
            dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            "0.0280",
        ),
    ):
        session.add(
            FoBacktestRun(
                user_id=user_id,
                family="B4",
                params={},
                tier="2E",
                caveat=fno_read.TIER_2E_CAVEAT,
                sample_from=dt.date(2022, 1, 3),
                sample_to=dt.date(2026, 8, 31),
                n=101,
                net_r=Decimal(net),
                gross_r=Decimal("0.0450"),
                per_year={},
                slippage_source="ASSUMED",
                run_at=run_at,
            )
        )
    await session.flush()
    return open_f1.id


def _journal(
    user_id: int, position: FoPosition, structure: str, r: Decimal, *, rolls: int
) -> FoJournal:
    assert position.closed_at is not None
    return FoJournal(
        position_id=position.id,
        user_id=user_id,
        sleeve=position.sleeve,
        symbol=position.symbol,
        structure=structure,
        opened_on=position.opened_at.date(),
        closed_on=position.closed_at.date(),
        entry_inr=Decimal("6000.00"),
        exit_inr=Decimal("3000.00"),
        gross_pnl_inr=Decimal("3100.00"),
        costs_inr=Decimal("100.00"),
        net_pnl_inr=Decimal("3000.00"),
        risk_budget_inr=Decimal("25000.00"),
        r_multiple=r,
        closed_reason=position.closed_reason or "",
        sessions_held=12,
        rolls=rolls,
        simulated=True,
        sizing_mode="BUDGET",
    )


class TestTheEmptyPageSaysWhy:
    async def test_with_the_scan_off_the_reason_is_the_scan(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _user_id, public_id = await _tenant(screener_session, monkeypatch)
        _clock(monkeypatch)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/fno/overnight"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["empty_reason"] == "scan_off"
        assert body["scan_date"] is None
        assert [u["symbol"] for u in body["underlyings"]] == ["NIFTY", "BANKNIFTY"]
        assert all(u["scan"] is None for u in body["underlyings"])
        assert [(g["group"], g["mode"]) for g in body["gates"]] == [
            ("F1", "PAPER"),
            ("F2", "PAPER"),
            ("F3", "PAPER"),
        ]
        f3 = body["f3"]
        assert f3["scan_date"] is None
        assert [(u["symbol"], u["sleeve"]) for u in f3["underlyings"]] == [
            ("NIFTY", "F3N"),
            ("BANKNIFTY", "F3B"),
        ]
        assert all(u["scan"] is None for u in f3["underlyings"])
        assert f3["open"] == [] and f3["closed"] == [] and f3["backtests"] == []
        assert "the 75-minute confirm" in f3["not_tested"]
        assert "-0.021R" in f3["research_line"]
        assert body["evidence"]["caveat"] == fno_read.TIER_2E_CAVEAT

    async def test_with_the_scan_on_and_no_row_it_is_never_scanned(
        self, scan_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _user_id, public_id = await _tenant(screener_session, monkeypatch)
        _clock(monkeypatch)
        async with running_app(scan_on, screener_session) as client:
            body = (await client.get(url("/fno/overnight"), headers=bearer(public_id))).json()
        assert body["empty_reason"] == "never_scanned"


class TestTheOvernightPage:
    async def test_it_serves_what_the_scan_and_the_book_wrote(
        self, scan_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        open_id = await _book(screener_session, user_id)
        _clock(monkeypatch)
        async with running_app(scan_on, screener_session) as client:
            response = await client.get(url("/fno/overnight"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["empty_reason"] is None
        assert body["scan_date"] == "2026-09-22"
        assert body["next_session"] == "2026-09-24"
        nifty, bank = body["underlyings"]
        assert nifty["scan"]["state"] == "NOT_ENTRY_DAY"
        assert nifty["next_entry_date"] == "2026-10-06"
        assert nifty["level"] == {
            "level": "25100.50",
            "close_of": "2026-09-22",
            "live_symbol": "NIFTY 50",
        }
        assert bank["scan"]["state"] == "CANDIDATE"
        assert bank["next_entry_date"] == "2026-09-23"
        assert bank["scan"]["detail"]["credit_points"] == "212.35", "passed through, not re-rounded"
        assert bank["scan"]["detail"]["iv_rv_note"] == "recorded, not used"
        assert bank["level"]["level"] is None, "no BANKNIFTY future in the fixture's file"
        assert bank["level"]["live_symbol"] == "NIFTY BANK"

        assert [p["id"] for p in body["hard_exit_tomorrow"]] == [open_id]
        (structure,) = body["open_structures"]
        assert structure["mark"]["trade_date"] == "2026-09-22"
        assert structure["mark"]["pnl_inr"] == "1196.00"
        assert structure["mark"]["pnl_r"] == "0.060"
        assert [j["sleeve"] for j in body["journal"]] == ["F1B"]

        evidence = body["evidence"]
        assert evidence["tier"] == "2E"
        assert evidence["n"] == 100
        assert evidence["caveat"] == fno_read.TIER_2E_CAVEAT
        assert [(b["family"], b["net_r"]) for b in evidence["backtests"]] == [("B4", "0.0280")]
        tally = {t["sleeve"]: t for t in evidence["tally"]}
        assert tally["F1B"]["closed"] == 1
        assert tally["F1N"]["closed"] == 0
        assert tally["F2"]["rolls"] == 1
        assert tally["F2"]["violations"] is None

        f2 = body["f2"]
        assert sorted(c["symbol"] for c in f2["candidates"]) == ["RELIANCE", "TCS"]
        assert f2["state_counts"] == {"CANDIDATE": 1, "NO_SIGNAL": 1, "REJECTED_SIZE": 1}
        assert f2["open"] == []
        assert [c["symbol"] for c in f2["closed"]] == ["LT"]
        assert "+0.017R" in f2["research_line"]

    async def test_it_serves_f3s_night_beside_f1_and_f2(
        self, scan_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        await _book(screener_session, user_id)
        _clock(monkeypatch)
        async with running_app(scan_on, screener_session) as client:
            response = await client.get(url("/fno/overnight"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        body = response.json()

        f3 = body["f3"]
        assert f3["scan_date"] == "2026-09-22"
        f3n, f3b = f3["underlyings"]
        assert (f3n["sleeve"], f3n["scan"]["state"]) == ("F3N", "NO_SIGNAL")
        assert f3n["next_entry_date"] == "2026-09-23", "the next session the desk may plan"
        assert f3n["level"]["live_symbol"] == "NIFTY 50"
        assert (f3b["sleeve"], f3b["scan"]["state"]) == ("F3B", "CANDIDATE")
        assert f3b["scan"]["detail"]["credit_points"] == "31.45", "passed through, not re-rounded"
        assert f3["open"] == [] and f3["closed"] == []
        assert [b["family"] for b in f3["backtests"]] == [], "no F3 re-test has run in the fixture"
        assert [u["scan"]["sleeve"] for u in body["underlyings"]] == ["F1N", "F1B"], (
            "F3's rows never land on F1's cards"
        )

    async def test_another_users_rows_are_not_served(
        self, scan_on: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_id, _ = await make_user(screener_session, "fno-other@example.com")
        await _book(screener_session, other_id)
        _user_id, public_id = await _tenant(screener_session, monkeypatch)
        _clock(monkeypatch)
        async with running_app(scan_on, screener_session) as client:
            body = (await client.get(url("/fno/overnight"), headers=bearer(public_id))).json()
        assert body["scan_date"] is None
        assert body["open_structures"] == []
        assert body["f2"]["closed"] == []


def _series(symbol: str, day: dt.date, **values: object) -> FoUnderlyingDaily:
    return FoUnderlyingDaily(trade_date=day, symbol=symbol, **values)


class TestTheInformationPage:
    async def test_it_is_04_section_5s_columns(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        # 210 weekday sessions of RELIANCE ending on AS_OF: IV cycles 0.20-0.29, OI steps by 10.
        days: list[dt.date] = []
        day = AS_OF
        while len(days) < 210:
            if day.weekday() < 5:
                days.append(day)
            day -= dt.timedelta(days=1)
        days.reverse()
        for i, d in enumerate(days):
            iv = Decimal("0.245") if d == AS_OF else Decimal("0.20") + Decimal(i % 10) / 100
            screener_session.add(
                _series(
                    "RELIANCE",
                    d,
                    level_c=Decimal("1452.30"),
                    iv_atm=iv,
                    rv20=Decimal("0.200000"),
                    oi_total=1000 + 10 * i,
                    fut_turnover_20d=Decimal("9000000000"),
                    basis_ann=Decimal("0.061000"),
                )
            )
        screener_session.add(
            _series(
                "TCS",
                AS_OF,
                level_c=Decimal("3120.00"),
                fut_turnover_20d=Decimal("100"),
                in_ban=True,
            )
        )
        screener_session.add_all(
            [
                OpExpiry(
                    underlying="RELIANCE",
                    expiry_date=d,
                    kind="MONTHLY",
                    lot_size=500,
                    first_seen=dt.date(2026, 9, 1),
                    seen_on=AS_OF,
                    detail={},
                )
                for d in (dt.date(2026, 9, 29), dt.date(2026, 10, 27))
            ]
        )
        screener_session.add(
            FoContractDaily(
                trade_date=AS_OF,
                instrument="FUTSTK",
                symbol="TCS",
                expiry=dt.date(2026, 9, 29),
                strike=Decimal(0),
                option_type="XX",
                close=Decimal("3120.00"),
                settle=Decimal("3120.00"),
                lot_size=175,
                source_key="test",
            )
        )
        screener_session.add_all(
            [
                FoIngestDay(trade_date=dt.date(2026, 9, 18), status="MISSING", attempts=6),
                FoIngestDay(
                    trade_date=AS_OF,
                    status="INGESTED",
                    attempts=1,
                    ban_for_session=dt.date(2026, 9, 23),
                    ban_symbols=["TCS"],
                ),
            ]
        )
        for d in (dt.date(2026, 9, 21), AS_OF):
            screener_session.add(
                FoSpreadSample(
                    trade_date=d,
                    symbol="RELIANCE",
                    expiry=dt.date(2026, 10, 27),
                    strike=Decimal("1450"),
                    option_type="CE",
                    bid=Decimal("30.10"),
                    ask=Decimal("30.60"),
                    mid=Decimal("30.35"),
                    oi=1000,
                    taken_at=dt.datetime.combine(d, dt.time(15, 0), tzinfo=IST),
                )
            )
        screener_session.add(
            FoBacktestRun(
                user_id=user_id,
                family="C1",
                params={},
                tier="2E",
                caveat=fno_read.TIER_2E_CAVEAT,
                sample_from=dt.date(2022, 1, 3),
                sample_to=dt.date(2026, 9, 30),
                n=500,
                net_r=Decimal("-0.2100"),
                per_year={},
                slippage_source="ASSUMED",
                run_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            )
        )
        await screener_session.flush()
        _clock(monkeypatch)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/fno/info"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["as_of"] == "2026-09-22"
        assert [r["symbol"] for r in body["rows"]] == ["RELIANCE", "TCS"], "turnover order"
        rel, tcs = body["rows"]
        # 209 earlier values cycle 0.20..0.29; those <= 0.245 are 0.20-0.24 (5 of every 10) —
        # 105 of the 209 (indices 0-4 of each ten, 21 tens less the last one's 5-9) — plus today.
        earlier = [Decimal("0.20") + Decimal(i % 10) / 100 for i in range(209)]
        le = sum(1 for v in earlier if v <= Decimal("0.245")) + 1
        assert rel["iv_sessions_1y"] == 210
        assert Decimal(rel["iv_pct_1y"]) == (Decimal(le) * 100 / 210).quantize(Decimal("0.1"))
        # OI today 1000 + 10*209 = 3090; five sessions back 1000 + 10*204 = 3040.
        assert Decimal(rel["oi_change_5d_pct"]) == (Decimal(50) * 100 / 3040).quantize(
            Decimal("0.01")
        )
        assert rel["near_monthly"] == "2026-09-29"
        assert rel["days_to_near_monthly"] == 7
        assert rel["lot_size"] == 500
        assert rel["fut_settle"] == "1452.30"
        assert rel["iv_rv"] == "1.23"
        assert rel["in_ban"] is False
        assert tcs["in_ban"] is True
        assert tcs["lot_size"] == 175, "the bhavcopy's lot when the master has no monthly"
        assert tcs["iv_pct_1y"] is None
        assert tcs["iv_sessions_1y"] == 0
        for column in rel:
            assert "signal" not in column and "score" not in column and "rank" not in column

        families = body["families"]
        assert families[0]["verdict"]["family"].startswith("B4")
        c1 = next(f for f in families if f["verdict"]["family"].startswith("C1"))
        assert c1["latest_retest"]["net_r"] == "-0.2100"
        assert c1["latest_retest"]["caveat"] == fno_read.TIER_2E_CAVEAT
        assert body["spread_sample"] == {
            "sessions": 2,
            "first": "2026-09-21",
            "last": "2026-09-22",
            "symbols": 1,
        }
        assert body["ingest"]["missing_days"] == ["2026-09-18"]
        assert body["ingest"]["latest_status"] == "INGESTED"
        assert body["ingest"]["ban_symbols"] == ["TCS"]


class TestTheSettingsPatch:
    async def test_a_change_is_audited(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/fno/config"),
                headers=bearer(public_id),
                json={"sleeves": {"F1": {"risk_per_trade_pct": "0.80"}}},
            )
        assert response.status_code == 200, response.text
        f1 = next(s for s in response.json()["sleeves"] if s["sleeve"] == "F1")
        assert f1["risk_per_trade_pct"] == "0.80"
        audit = (
            await screener_session.execute(
                select(FoConfigAudit).where(FoConfigAudit.user_id == user_id)
            )
        ).scalars()
        assert [(a.scope, a.key, a.old_value, a.new_value) for a in audit] == [
            ("F1", "risk_per_trade_pct", "1.00", "0.80")
        ]

    async def test_above_a_ceiling_is_refused_whole_and_names_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/fno/config"),
                headers=bearer(public_id),
                # Rs 30 lakh at 1 % is Rs 30,000 a trade, above the Rs 25,000 ceiling.
                json={
                    "book": {"monthly_pause_inr": "50000"},
                    "sleeves": {"F1": {"capital_inr": "3000000"}},
                },
            )
        assert response.status_code == 422, response.text
        assert "BASKFY_FNO_RISK_PER_TRADE_INR_MAX" in response.text
        book = await screener_session.get(FoBookConfig, user_id)
        assert book is not None
        assert book.monthly_pause_inr == 0, "the book part is not written either"
        audit = (
            await screener_session.execute(
                select(FoConfigAudit).where(FoConfigAudit.user_id == user_id)
            )
        ).scalars()
        assert list(audit) == []

    async def test_f1_open_positions_are_bounded_by_its_underlyings(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        async with running_app(settings, screener_session) as client:
            response = await client.patch(
                url("/fno/config"),
                headers=bearer(public_id),
                json={"sleeves": {"F1": {"max_open_positions": 3}}},
            )
        assert response.status_code == 400, response.text

    @pytest.mark.parametrize(
        "body",
        [
            {"sleeves": {"F1": {"paused_until": None}}},
            {"book": {"paused_reason": "x"}},
            {"sleeves": {"F2": {"execution_enabled": True}}},
            {"carry_enabled": True},
        ],
    )
    async def test_no_pause_or_flag_is_a_field(
        self,
        body: dict[str, object],
        settings: Settings,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await _tenant(screener_session, monkeypatch)
        await _config(screener_session, user_id)
        async with running_app(settings, screener_session) as client:
            response = await client.patch(url("/fno/config"), headers=bearer(public_id), json=body)
        assert response.status_code in {400, 422}, response.text
