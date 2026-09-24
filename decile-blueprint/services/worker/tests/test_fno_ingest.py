"""FO2: the nightly F&O bhavcopy ingest, the ban list, the backfill and the Beat entry.

``docs/fno/06`` FO2 and ``04`` §4 asserted here:

* ``03`` §1's **retention**: every future; options of the two nearest monthly expiries per
  underlying plus NIFTY/BANKNIFTY weeklies, with non-zero OI or volume; "monthly" read from the
  file (the last expiry of its month), never a weekday rule;
* **round at write time** (half-up, 2 dp) and a future's key is ``strike 0, XX``;
* the ingest **upserts by the key with ``source_key``** and a re-run writes identical rows;
* a day with no file is ``PENDING`` before 23:30 and ``MISSING`` at it, never interpolated;
* the **ban list for the next session** lands on ``fo_ingest_day`` and as ``in_ban`` on tonight's
  ``fo_underlying_daily`` rows; a wrong-date list is refused and writes nothing;
* the **backfill is resumable** (an ``INGESTED`` day is not read again) and ``--seed-archive``
  puts a local zip in the provider's archive so NSE is not asked;
* the task is gated by ``BASKFY_FNO_SCAN_ENABLED`` (default false) before any read, and Beat runs
  it at 18:30 and hourly to 23:30 on weekdays.

The NSE path is the real ``NSEProvider`` over a fake HTTP client and a local archive: no network.
Database tests run in rolled-back transactions against ``BASKFY_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import subprocess
import zipfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import FoContractDaily, FoIngestDay, FoUnderlyingDaily, TradingDay
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.archive import LocalRawArchive, archive_key
from baskfy_providers.errors import UnexpectedPayload
from baskfy_providers.nse import KIND_FO_BHAVCOPY, NSEProvider, NSERuntime
from baskfy_providers.records import FO_BHAVCOPY_SCHEMA, empty_frame
from baskfy_providers.retry import RetryPolicy
from baskfy_providers.settings import ProviderSettings
from baskfy_worker.fno.backfill import backfill_days, seed_archive_day
from baskfy_worker.fno.ingest import (
    contract_rows,
    ingest_day,
    is_final_attempt,
    retain,
    store_ban_list,
)
from baskfy_worker.fno.nightly import run_night
from baskfy_worker.fno.partitions import partition_name
from baskfy_worker.settings import WorkerSettings

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: Sessions beyond the seeded calendar and the migration's partitions, so the fixture's rows
#: never meet real ones and the ingest must create the month's partition itself.
DAY: Final = dt.date(2030, 1, 2)
NEXT: Final = dt.date(2030, 1, 3)
JAN, FEB, MAR = dt.date(2030, 1, 29), dt.date(2030, 2, 26), dt.date(2030, 3, 26)
NIFTY_WEEKLIES: Final = (dt.date(2030, 1, 8), dt.date(2030, 1, 15))
NIFTY_FAR: Final = dt.date(2030, 6, 25)
#: A monthly that moved to the Monday for a holiday: still the month's last listed expiry.
BANKNIFTY_JAN: Final = dt.date(2030, 1, 28)

_UDIFF_HEADER: Final = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)


def _line(  # noqa: PLR0913, PLR0917 - one argument per bhavcopy column the fixture varies
    stamp: dt.date,
    kind: str,
    symbol: str,
    expiry: dt.date,
    strike: str = "",
    opt: str = "",
    *,
    settle: str = "100.00",
    oi: int = 1000,
    volume: int = 10,
    lot: int = 750,
) -> str:
    s, e = stamp.isoformat(), expiry.isoformat()
    return (
        f"{s},{s},FO,NSE,{kind},1,,{symbol},,{e},{e},{strike},{opt},X,"
        f"99,101,98,{settle},100,99,100,{settle},{oi},0,{volume},1000000.555,1,F1,{lot},,,,,\n"
    )


def udiff_zip(stamp: dt.date = DAY) -> bytes:
    rows = [
        # SBIN: two futures (one never traded: futures are kept whatever their OI), options on
        # three monthlies (only the two nearest kept), one option neither traded nor open.
        _line(stamp, "STF", "SBIN", JAN, settle="810.355"),
        _line(stamp, "STF", "SBIN", FEB, oi=0, volume=0),
        _line(stamp, "STO", "SBIN", JAN, "820", "CE"),
        _line(stamp, "STO", "SBIN", FEB, "820", "PE"),
        _line(stamp, "STO", "SBIN", MAR, "820", "CE"),
        _line(stamp, "STO", "SBIN", JAN, "900", "CE", oi=0, volume=0),
        _line(stamp, "STO", "SBIN", JAN, "880", "CE", oi=0, volume=5),
        # NIFTY: weeklies kept, the two nearest monthlies kept, a far half-yearly dropped.
        _line(stamp, "IDF", "NIFTY", JAN, lot=65),
        _line(stamp, "IDO", "NIFTY", NIFTY_WEEKLIES[0], "25000", "CE", lot=65),
        _line(stamp, "IDO", "NIFTY", NIFTY_WEEKLIES[1], "25000", "PE", lot=65),
        _line(stamp, "IDO", "NIFTY", JAN, "25000", "CE", lot=65),
        _line(stamp, "IDO", "NIFTY", FEB, "25000", "CE", lot=65),
        _line(stamp, "IDO", "NIFTY", NIFTY_FAR, "25000", "CE", lot=65),
        # BANKNIFTY: a Monday monthly, and a Jan weekly before it.
        _line(stamp, "IDO", "BANKNIFTY", BANKNIFTY_JAN, "55000", "CE", lot=30),
        _line(stamp, "IDO", "BANKNIFTY", dt.date(2030, 1, 21), "55000", "PE", lot=30),
        # A stock has no weeklies: a non-last January expiry is not a monthly and not kept.
        _line(stamp, "STO", "IDEA", dt.date(2030, 1, 21), "8", "CE", lot=71_475),
        _line(stamp, "STO", "IDEA", JAN, "8", "CE", lot=71_475),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        name = f"BhavCopy_NSE_FO_0_0_0_{stamp.strftime('%Y%m%d')}_F_0000.csv"
        bundle.writestr(name, _UDIFF_HEADER + "".join(rows))
    return buffer.getvalue()


def ban_file(stamp: str, *symbols: str) -> bytes:
    lines = [f"Securities in Ban For Trade Date {stamp}:"]
    lines += [f"{i},{s}" for i, s in enumerate(symbols, start=1)]
    return ("\n".join(lines) + "\n").encode()


class FakeResponse:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content


class FakeHttpClient:
    """Serves canned bodies by URL substring (404 otherwise) and records every request."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self.routes = dict(routes)
        self.requests: list[str] = []

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> FakeResponse:
        del headers
        self.requests.append(url)
        if url.rstrip("/").endswith("nseindia.com"):
            return FakeResponse(200, b"<html></html>")
        for fragment, body in self.routes.items():
            if fragment in url:
                return FakeResponse(200, body)
        return FakeResponse(404, b"")

    def file_requests(self) -> list[str]:
        return [u for u in self.requests if not u.rstrip("/").endswith("nseindia.com")]


class InertLimiter:
    def acquire(self, tokens: float = 1.0) -> float:
        del tokens
        return 0.0


def nse(archive: LocalRawArchive, client: FakeHttpClient) -> NSEProvider:
    return NSEProvider(
        ProviderSettings(_env_file=None),
        archive,
        NSERuntime(
            client=client,
            rate_limiter=InertLimiter(),
            retry_policy=RetryPolicy(max_attempts=1, base_seconds=0.01),
        ),
    )


# --- pure ---------------------------------------------------------------------------------------


@pytest.fixture
def day_frame(tmp_path: Path) -> pl.DataFrame:
    client = FakeHttpClient({"BhavCopy_NSE_FO": udiff_zip()})
    return nse(LocalRawArchive(tmp_path / "archive"), client).fo_bhavcopy(DAY)


def _kept(frame: pl.DataFrame) -> set[tuple[str, dt.date, str | None, float]]:
    return {
        (r["symbol"], r["expiry"], r["option_type"], float(r["strike"] or 0))
        for r in retain(frame).iter_rows(named=True)
    }


class TestRetention:
    def test_every_future_is_kept_even_untraded(self, day_frame: pl.DataFrame) -> None:
        kept = _kept(day_frame)
        assert ("SBIN", JAN, None, 0.0) in kept
        assert ("SBIN", FEB, None, 0.0) in kept, "03 §1: everything for futures"
        assert ("NIFTY", JAN, None, 0.0) in kept

    def test_options_of_the_two_nearest_monthlies_only(self, day_frame: pl.DataFrame) -> None:
        kept = _kept(day_frame)
        assert ("SBIN", JAN, "CE", 820.0) in kept
        assert ("SBIN", FEB, "PE", 820.0) in kept
        assert ("SBIN", MAR, "CE", 820.0) not in kept, "the third monthly is trimmed"
        assert ("NIFTY", NIFTY_FAR, "CE", 25000.0) not in kept, "a far half-yearly is trimmed"

    def test_nifty_and_banknifty_weeklies_are_kept_a_stocks_non_monthly_is_not(
        self, day_frame: pl.DataFrame
    ) -> None:
        kept = _kept(day_frame)
        for weekly in NIFTY_WEEKLIES:
            assert any(k[0] == "NIFTY" and k[1] == weekly for k in kept)
        assert ("BANKNIFTY", dt.date(2030, 1, 21), "PE", 55000.0) in kept
        assert ("IDEA", dt.date(2030, 1, 21), "CE", 8.0) not in kept

    def test_monthly_is_the_months_last_listed_expiry_not_a_weekday(
        self, day_frame: pl.DataFrame
    ) -> None:
        """BANKNIFTY's January monthly is a Monday (a holiday moved it): still kept."""
        assert BANKNIFTY_JAN.weekday() == 0
        assert ("BANKNIFTY", BANKNIFTY_JAN, "CE", 55000.0) in _kept(day_frame)

    def test_an_option_neither_traded_nor_open_is_dropped(self, day_frame: pl.DataFrame) -> None:
        kept = _kept(day_frame)
        assert ("SBIN", JAN, "CE", 900.0) not in kept, "zero OI and zero volume"
        assert ("SBIN", JAN, "CE", 880.0) in kept, "zero OI but traded"

    def test_an_empty_frame_retains_nothing(self) -> None:
        empty = empty_frame(FO_BHAVCOPY_SCHEMA)
        assert retain(empty).is_empty()


class TestTheRowShape:
    def test_prices_are_rounded_half_up_at_write_time(self, day_frame: pl.DataFrame) -> None:
        rows = contract_rows(retain(day_frame), "nse/fo-bhavcopy/2030-01-02.zip")
        future = next(
            r
            for r in rows
            if r["symbol"] == "SBIN" and r["expiry"] == JAN and r["option_type"] == "XX"
        )
        assert future["settle"] == Decimal("810.36"), "810.355 half-up to 2 dp (house rule 8)"
        assert future["turnover"] == Decimal("1000000.56")

    def test_a_future_is_keyed_strike_zero_xx_and_every_row_names_its_file(
        self, day_frame: pl.DataFrame
    ) -> None:
        key = "nse/fo-bhavcopy/2030-01-02.zip"
        rows = contract_rows(retain(day_frame), key)
        futures = [r for r in rows if r["instrument"] in ("FUTSTK", "FUTIDX")]
        assert futures and all(r["strike"] == 0 and r["option_type"] == "XX" for r in futures)
        assert all(r["source_key"] == key for r in rows)
        idea = next(r for r in rows if r["symbol"] == "IDEA")
        assert idea["lot_size"] == 71_475


class TestTheLastAttempt:
    @pytest.mark.parametrize(
        ("now", "final"),
        [
            (dt.datetime(2030, 1, 2, 18, 30, tzinfo=IST), False),
            (dt.datetime(2030, 1, 2, 22, 30, tzinfo=IST), False),
            (dt.datetime(2030, 1, 2, 23, 30, tzinfo=IST), True),
            (dt.datetime(2030, 1, 3, 9, 0, tzinfo=IST), True),
        ],
    )
    def test_missing_only_from_2330(self, now: dt.datetime, final: bool) -> None:
        assert is_final_attempt(DAY, now) is final


# --- the gate and the clock ---------------------------------------------------------------------


def test_the_flag_defaults_false() -> None:
    assert WorkerSettings(_env_file=None).fno_scan_enabled is False


def test_the_task_refuses_before_any_read_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baskfy_worker.tasks import celery_tasks  # noqa: PLC0415 - heavy import, one test

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("an NSE provider was built with the flag off")

    monkeypatch.setattr(celery_tasks, "build_nse_provider", explode)
    monkeypatch.setattr(celery_tasks, "get_worker_settings", lambda: WorkerSettings(_env_file=None))
    out = celery_tasks.fno_ingest_bhavcopy_task(trade_date=DAY.isoformat())
    assert out == {"trade_date": DAY.isoformat(), "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}


def test_beat_runs_it_at_1830_and_hourly_to_2330_on_weekdays() -> None:
    from baskfy_worker.celery_app import BEAT_SCHEDULE, build_celery  # noqa: PLC0415

    entry = BEAT_SCHEDULE["fno-bhavcopy"]
    assert entry["task"] == "baskfy.fno.ingest_bhavcopy"
    schedule = entry["schedule"]
    assert getattr(schedule, "minute") == {30}  # noqa: B009 - crontab's typed attribute
    assert getattr(schedule, "hour") == {18, 19, 20, 21, 22, 23}  # noqa: B009
    assert getattr(schedule, "day_of_week") == {1, 2, 3, 4, 5}  # noqa: B009
    assert "baskfy.fno.ingest_bhavcopy" in build_celery().tasks


# --- database -----------------------------------------------------------------------------------


def _database_url() -> str | None:
    return os.environ.get(ENV_VAR)


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set")
)


@pytest.fixture(scope="module")
def fo_url() -> str:
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return url


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _calendar(session: AsyncSession, *days: dt.date) -> None:
    stmt = insert(TradingDay).values(
        [
            {"exchange_id": NSE_EXCHANGE_ID, "date": d, "is_trading_day": True, "source": "derived"}
            for d in days
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["exchange_id", "date"], set_={"is_trading_day": True}
        )
    )


async def _contracts(session: AsyncSession, day: dt.date) -> list[tuple[object, ...]]:
    rows = await session.execute(
        sa.select(FoContractDaily)
        .where(FoContractDaily.trade_date == day)
        .order_by(
            FoContractDaily.symbol,
            FoContractDaily.expiry,
            FoContractDaily.strike,
            FoContractDaily.option_type,
        )
    )
    return [
        tuple(getattr(r, c.name) for c in FoContractDaily.__table__.columns) for r in rows.scalars()
    ]


async def _ingest_row(session: AsyncSession, day: dt.date) -> FoIngestDay | None:
    session.expire_all()
    return await session.get(FoIngestDay, day)


class MissingReader:
    """No file yet: what NSE's 404 becomes after the provider's retries."""

    def fo_bhavcopy(self, on: dt.date) -> pl.DataFrame:
        raise UnexpectedPayload(f"GET .../{on.isoformat()} returned 404", provider="nse")

    def fo_ban_list(self, for_session: dt.date) -> list[str]:
        raise UnexpectedPayload("no ban file", provider="nse")


@requires_db
class TestTheIngest:
    async def test_a_day_lands_by_its_key_with_its_source_and_a_rerun_is_identical(
        self, fo_url: str, tmp_path: Path
    ) -> None:
        client = FakeHttpClient({"BhavCopy_NSE_FO": udiff_zip()})
        provider = nse(LocalRawArchive(tmp_path / "archive"), client)
        async with _rolled_back(fo_url) as session:
            report = await ingest_day(session, provider, DAY, final_attempt=False)
            assert report.status == "INGESTED"
            assert report.rows_in_file == 17
            first = await _contracts(session, DAY)
            assert len(first) == report.rows_kept
            key = archive_key(KIND_FO_BHAVCOPY, DAY, "zip")
            assert {r[-1] for r in first} == {key}
            # The month's partition was created by the ingest, not the migration.
            assert (
                await session.execute(
                    sa.text("SELECT to_regclass(:n)::text"), {"n": partition_name(DAY)}
                )
            ).scalar_one() == partition_name(DAY)
            # Re-running the night answers from the database (no read) and changes nothing.
            asked = len(client.file_requests())
            again = await ingest_day(session, provider, DAY, final_attempt=False)
            assert again.skipped == "already ingested"
            assert len(client.file_requests()) == asked
            # Forcing the rows through the upsert again writes identical rows (house rule 7).
            from baskfy_worker.fno.ingest import upsert_contracts  # noqa: PLC0415

            await upsert_contracts(session, contract_rows(retain(provider.fo_bhavcopy(DAY)), key))
            session.expire_all()
            assert await _contracts(session, DAY) == first
            row = await _ingest_row(session, DAY)
            assert row is not None and row.status == "INGESTED" and row.source_key == key

    async def test_no_file_is_pending_then_missing_at_2330_and_never_interpolated(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            early = await ingest_day(session, MissingReader(), DAY, final_attempt=False)
            assert early.status == "PENDING" and "404" in (early.error or "")
            late = await ingest_day(session, MissingReader(), DAY, final_attempt=True)
            assert late.status == "MISSING"
            row = await _ingest_row(session, DAY)
            assert row is not None and (row.status, row.attempts) == ("MISSING", 2)
            assert await _contracts(session, DAY) == [], "a missing day has no rows at all"

    async def test_a_file_stamped_with_another_session_writes_nothing(
        self, fo_url: str, tmp_path: Path
    ) -> None:
        client = FakeHttpClient({"BhavCopy_NSE_FO": udiff_zip(stamp=NEXT)})
        provider = nse(LocalRawArchive(tmp_path / "archive"), client)
        async with _rolled_back(fo_url) as session:
            report = await ingest_day(session, provider, DAY, final_attempt=False)
            assert report.status == "PENDING" and "wrong file" in (report.error or "")
            assert await _contracts(session, DAY) == []


@requires_db
class TestTheBanList:
    async def test_the_next_sessions_list_lands_on_tonights_rows(self, fo_url: str) -> None:
        class Reader(MissingReader):
            def fo_ban_list(self, for_session: dt.date) -> list[str]:
                assert for_session == NEXT
                return ["IDEA", "SAIL"]

        async with _rolled_back(fo_url) as session:
            session.add(FoUnderlyingDaily(trade_date=DAY, symbol="SBIN", in_ban=True))
            session.add(FoUnderlyingDaily(trade_date=DAY, symbol="IDEA"))
            await session.flush()
            report = await store_ban_list(session, Reader(), DAY, NEXT)
            assert report.symbols == ["IDEA", "SAIL"]
            session.expire_all()
            rows = (
                await session.execute(
                    sa.select(FoUnderlyingDaily.symbol, FoUnderlyingDaily.in_ban).where(
                        FoUnderlyingDaily.trade_date == DAY
                    )
                )
            ).all()
            flags = {symbol: banned for symbol, banned in rows}
            assert flags == {"IDEA": True, "SAIL": True, "SBIN": False}
            row = await _ingest_row(session, DAY)
            assert row is not None
            assert (row.ban_for_session, row.ban_symbols) == (NEXT, ["IDEA", "SAIL"])
            again = await store_ban_list(session, Reader(), DAY, NEXT)
            assert again.skipped == "already stored"

    async def test_todays_list_served_for_tomorrow_writes_nothing(
        self, fo_url: str, tmp_path: Path
    ) -> None:
        client = FakeHttpClient({"fo_secban": ban_file("02-JAN-2030", "IDEA")})
        provider = nse(LocalRawArchive(tmp_path / "archive"), client)
        async with _rolled_back(fo_url) as session:
            report = await store_ban_list(session, provider, DAY, NEXT)
            assert report.symbols is None and "wrong file" in (report.error or "")
            assert await _ingest_row(session, DAY) is None
            count = (
                await session.execute(
                    sa.select(sa.func.count()).where(FoUnderlyingDaily.trade_date == DAY)
                )
            ).scalar_one()
            assert count == 0


@requires_db
class TestTheNight:
    async def test_one_night_ingests_and_stores_the_next_sessions_ban_list(
        self, fo_url: str, tmp_path: Path
    ) -> None:
        client = FakeHttpClient(
            {"BhavCopy_NSE_FO": udiff_zip(), "fo_secban": ban_file("03-JAN-2030", "KAYNES")}
        )
        provider = nse(LocalRawArchive(tmp_path / "archive"), client)
        async with _rolled_back(fo_url) as session:
            await _calendar(session, DAY, NEXT)
            now = dt.datetime(2030, 1, 2, 18, 30, tzinfo=IST)
            out = await run_night(session, provider, DAY, now_ist=now)
            ingest = out["ingest"]
            ban = out["ban_list"]
            assert isinstance(ingest, dict) and ingest["status"] == "INGESTED"
            assert isinstance(ban, dict) and ban["symbols"] == ["KAYNES"]
            assert out["underlying_rows"] is None, "TODO(FO1-wire): derivation not wired yet"
            asked = len(client.file_requests())
            second = await run_night(session, provider, DAY, now_ist=now)
            assert len(client.file_requests()) == asked, "a finished night asks NSE nothing"
            assert isinstance(second["ban_list"], dict)
            assert second["ban_list"]["skipped"] == "already stored"

    async def test_a_holiday_is_skipped_before_any_read(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await session.execute(
                insert(TradingDay)
                .values(
                    exchange_id=NSE_EXCHANGE_ID, date=DAY, is_trading_day=False, source="holiday"
                )
                .on_conflict_do_update(
                    index_elements=["exchange_id", "date"], set_={"is_trading_day": False}
                )
            )
            out = await run_night(
                session, MissingReader(), DAY, now_ist=dt.datetime(2030, 1, 2, 23, 30, tzinfo=IST)
            )
            assert out == {"trade_date": DAY.isoformat(), "skipped": "not an NSE trading day"}
            assert await _ingest_row(session, DAY) is None


@requires_db
class TestTheBackfill:
    async def test_it_resumes_seeds_from_a_local_archive_and_records_a_missing_day(
        self, fo_url: str, tmp_path: Path
    ) -> None:
        seed_dir = tmp_path / "research" / "archive"
        seeded_key = archive_key(KIND_FO_BHAVCOPY, DAY, "zip")
        (seed_dir / seeded_key).parent.mkdir(parents=True)
        (seed_dir / seeded_key).write_bytes(udiff_zip(DAY))
        archive = LocalRawArchive(tmp_path / "archive")
        client = FakeHttpClient({})  # NSE has nothing: every file must come from the seed
        provider = nse(archive, client)
        commits: list[int] = []

        async def checkpoint() -> None:
            commits.append(1)

        after = dt.datetime(2030, 1, 10, 9, 0, tzinfo=IST)
        async with _rolled_back(fo_url) as session:
            report = await backfill_days(
                session,
                provider,
                [DAY, NEXT],
                archive=archive,
                seed_dir=seed_dir,
                checkpoint=checkpoint,
                now_ist=after,
            )
            assert (report.ingested, report.seeded_from_archive) == (1, 1)
            assert report.missing == [NEXT], "no seed and no file: MISSING, not a gap"
            assert len(commits) == 2, "each session is its own checkpoint"
            assert not [u for u in client.file_requests() if "20300102" in u]
            assert archive.exists(seeded_key)
            # Resumed: the ingested day is not read again; the missing one is retried.
            again = await backfill_days(
                session, provider, [DAY, NEXT], archive=archive, seed_dir=seed_dir, now_ist=after
            )
            assert (again.already_present, again.ingested, again.missing) == (1, 0, [NEXT])

    def test_the_seed_never_overwrites_the_archive(self, tmp_path: Path) -> None:
        archive = LocalRawArchive(tmp_path / "archive")
        key = archive_key(KIND_FO_BHAVCOPY, DAY, "zip")
        archive.put(key, b"the record", content_type="application/zip")
        seed_dir = tmp_path / "seed"
        (seed_dir / "fo-bhavcopy").mkdir(parents=True)
        (seed_dir / "fo-bhavcopy" / f"{DAY.isoformat()}.zip").write_bytes(b"other bytes")
        assert seed_archive_day(archive, seed_dir, DAY) is False
        assert archive.get(key) == b"the record"
        assert seed_archive_day(archive, seed_dir, NEXT) is False, "nothing to seed for NEXT"
