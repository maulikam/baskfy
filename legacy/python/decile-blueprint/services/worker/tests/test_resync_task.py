"""The resync button's repair half (leaf 3.1).

``baskfy_api.resync`` finds the gaps; ``baskfy_worker.tasks.resync`` closes them. The detector's
own four-class tests live in ``services/api/tests/test_admin_resync.py``; what is asserted here is
the part an operator is actually trusting when they press a button from a phone:

* each class of gap is *closed*, not merely noticed;
* pressing twice changes nothing the second time (house rule 7, G3);
* a repair that fixes three of five things says so, rather than reporting success (G7);
* nothing in the path can reach an order.

These run against a real database because every remedy here is a database state change, and a
fake would only prove the fake behaves.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Final

import polars as pl
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from helpers import requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api.metrics import IST
from baskfy_api.resync import RESYNC_COMPLETED_ACTION
from baskfy_api.settings import Settings
from baskfy_core.models.reference import INFERRED_HOLIDAY_NAME
from baskfy_providers.errors import UpstreamUnavailable
from baskfy_providers.settings import ProviderSettings
from baskfy_worker import kite_session_cli
from baskfy_worker.tasks import resync as resync_task

pytestmark = [pytest.mark.db, requires_db]

#: Past docs/11's 20:15 publish deadline, so 2026-08-28 — the Friday M62 is about — is in scope.
NOW: Final = dt.datetime(2026, 8, 28, 21, 0, tzinfo=IST)

#: A window narrow enough that every day in it can be named.
WINDOW: Final = 12

UNIVERSE: Final = 200
HEALTHY: Final = 180

#: Tables a resync may write. Checksummed either side of the second press: house rule 7 says
#: re-running any day's job produces identical rows, and a count would not notice an updated one.
CHECKSUMMED: Final = ("ohlcv_daily", "trading_day")


def settings(url: str) -> Settings:
    return Settings(database_url=url, environment="test")


class FakeNSE:
    """A bhavcopy the tests control, in the schema ``NSEProvider.bhavcopy`` really returns.

    One object serves both jobs the real provider serves here — the publication probe ("did NSE
    publish for this date?") and the re-ingest — because that is how the production stack works
    and a test that split them could pass while the two disagreed.
    """

    def __init__(self, *, serves: Sequence[dt.date], rows: int = HEALTHY) -> None:
        self.serves = set(serves)
        self.rows = rows
        self.asked: list[dt.date] = []

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        self.asked.append(on)
        if on not in self.serves:
            # The shape a missing file really takes: the backfill treats a 404 as "no bhavcopy
            # for this date" rather than as a failure, and the publication probe as "no evidence".
            raise UpstreamUnavailable("404 Not Found", provider="nse")
        return pl.DataFrame(
            [
                {
                    "symbol": f"RSYNC{index:05d}",
                    "series": "EQ",
                    "date": on,
                    "open": Decimal("100.0000"),
                    "high": Decimal("100.0000"),
                    "low": Decimal("100.0000"),
                    "close": Decimal("100.0000"),
                    "prev_close": Decimal("100.0000"),
                    "volume": 1000,
                    "turnover": Decimal("100000.00"),
                    "trades": 10,
                    "upper_circuit": Decimal("110.0000"),
                    "lower_circuit": Decimal("90.0000"),
                }
                for index in range(1, self.rows + 1)
            ]
        )


class RecordingQueue:
    """Stands in for the broker. Records the nightly chains a repair asks for."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


class RefusingQueue:
    def send_task(self, name: str, args: Sequence[object]) -> object:
        raise ConnectionError(f"no broker for {name} {args}")


async def _open_days(session: AsyncSession, start: dt.date, end: dt.date) -> list[dt.date]:
    rows = await session.execute(
        text(
            "SELECT date FROM trading_day WHERE exchange_id = 1 AND is_trading_day "
            "AND date BETWEEN :start AND :end ORDER BY date"
        ),
        {"start": start, "end": end},
    )
    return [row[0] for row in rows]


async def _seed(session: AsyncSession) -> tuple[list[dt.date], int]:
    """A healthy twelve-day window and one staff account to attribute the repair to.

    Runs are published only from the *sixth* trading day onwards, which is deliberate: it stands
    in for a deployment whose pipeline started after its bars did (staging's `pipeline_run` begins
    on 2026-08-18 over bars reaching back to 2024). It also gives the tests a region where a bar
    gap is the *only* finding, which is what makes the idempotence assertion mean something.
    """
    await session.execute(
        text(
            "INSERT INTO instrument (exchange_id, symbol, name, series, instrument_type, "
            "is_active) SELECT 1, 'RSYNC' || lpad(i::text, 5, '0'), 'RSYNC ' || i, 'EQ', 'EQ', "
            f"true FROM generate_series(1, {UNIVERSE}) AS i"
        )
    )
    end = NOW.date()
    days = await _open_days(session, end - dt.timedelta(days=WINDOW), end)
    for day in days:
        await _add_bars(session, day, HEALTHY)
    moment = dt.datetime(2026, 8, 28, 14, 0, tzinfo=dt.UTC)
    for offset, day in enumerate(days[5:]):
        await session.execute(
            text(
                "INSERT INTO pipeline_run (trade_date, status, started_at, finished_at, "
                "data_version) VALUES (:day, 'succeeded', :at, :at, :version)"
            ),
            {"day": day, "at": moment, "version": 100 + offset},
        )
    actor = (
        await session.execute(
            text(
                "INSERT INTO app_user (public_id, email, password_hash, is_staff) "
                "VALUES ('resync00001', 'resync@example.com', 'x', true) RETURNING id"
            )
        )
    ).scalar_one()
    return days, int(actor)


async def _add_bars(session: AsyncSession, day: dt.date, count: int) -> None:
    if count <= 0:
        return
    await session.execute(
        text(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, "
            "close_raw, volume_raw, adj_factor, source) "
            "SELECT id, :day, 100, 100, 100, 100, 1000, 100, 1000, 1, 'nse' FROM ("
            "  SELECT id FROM instrument WHERE symbol LIKE 'RSYNC%' ORDER BY id LIMIT :count"
            ") AS chosen ON CONFLICT DO NOTHING"
        ),
        {"day": day, "count": count},
    )


async def _bars_on(session: AsyncSession, day: dt.date) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM ohlcv_daily WHERE date = :day"), {"day": day}
            )
        ).scalar_one()
    )


async def _checksums(session: AsyncSession) -> dict[str, str | None]:
    """One md5 per table, over every row. An updated row moves it; a count would not."""
    out: dict[str, str | None] = {}
    for table in CHECKSUMMED:
        value = (
            await session.execute(
                text(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t")
            )
        ).scalar_one()
        out[table] = None if value is None else str(value)
    return out


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def committed(
    engine: AsyncEngine, clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncEngine]:
    """A seeded database the repair can commit into, with no Kite store on the host.

    Not the module's usual rolled-back ``session`` fixture: ``run_resync`` opens its own
    transactional sessions (it has to — ``backfill_bars_from_bhavcopy`` commits per day), so a
    test holding an open transaction on the same rows would be waiting on itself.

    ``ProviderSettings`` reads the repository's own ``.env``, so on a developer's machine the
    class-(d) check would otherwise find their real Kite token and every assertion in this file
    would depend on whether they had logged in that morning. The two Kite tests re-patch it.

    The teardown matters: ``conftest``'s ``clean_db`` deletes ``app_user`` rows and does not
    truncate ``admin_action``, so a completion row left behind by one test makes the *next*
    test's clean-up a foreign-key violation.
    """
    del clean_db
    _no_token_store(monkeypatch)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM admin_action WHERE actor_user_id IN "
                    "(SELECT id FROM app_user WHERE email LIKE '%@example.com')"
                )
            )


class TestItClosesEachClassOfGap:
    async def test_a_thin_day_is_re_ingested_to_full_depth(
        self, committed: AsyncEngine, migrated_url: str
    ) -> None:
        """The 2026-02-01 shape: bars present, 87% of them missing, factors quietly wrong."""
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            thin = days[2]
            await session.execute(text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin})
            await _add_bars(session, thin, 25)

        provider = FakeNSE(serves=[thin])
        queue = RecordingQueue()
        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=provider,
            enqueue=queue,
            now=NOW,
        )

        async with _maker(committed)() as session:
            assert await _bars_on(session, thin) == HEALTHY
        assert any(f"{thin.isoformat()}: re-ingested" in line for line in outcome.repaired)
        # The bars are repaired but the factor windows that cross them are not, so a nightly is
        # queued for the date as well. A repair that stopped at the bars would leave every 9M and
        # 12M window that crosses the day still computed from the thin data.
        assert (resync_task.NIGHTLY, [thin.isoformat()]) in _nightlies(queue)

    async def test_a_wrongly_inferred_holiday_is_corrected_and_then_ingested(
        self, committed: AsyncEngine, migrated_url: str
    ) -> None:
        """M62's 2026-08-28. The order matters: while the day is marked shut every backfill skips
        it, so correcting the calendar is what makes the re-ingest possible at all."""
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            friday = days[2]
            await session.execute(
                text(
                    "UPDATE trading_day SET is_trading_day = false, source = 'bhavcopy', "
                    "holiday_name = :name WHERE exchange_id = 1 AND date = :day"
                ),
                {"day": friday, "name": INFERRED_HOLIDAY_NAME},
            )
            await session.execute(
                text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": friday}
            )

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[friday]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        async with _maker(committed)() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT is_trading_day, holiday_name FROM trading_day "
                        "WHERE exchange_id = 1 AND date = :day"
                    ),
                    {"day": friday},
                )
            ).one()
            assert row[0] is True
            assert row[1] is None
            assert await _bars_on(session, friday) == HEALTHY
        assert any("corrected trading_day" in line for line in outcome.repaired)

    async def test_a_day_with_no_published_run_gets_its_nightly_queued(
        self, committed: AsyncEngine, migrated_url: str
    ) -> None:
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            orphan = days[-1]
            await session.execute(
                text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": orphan}
            )

        queue = RecordingQueue()
        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[]),
            enqueue=queue,
            now=NOW,
        )

        assert (resync_task.NIGHTLY, [orphan.isoformat()]) in _nightlies(queue)
        assert any(orphan.isoformat() in line for line in outcome.queued)
        # A chain sitting in the queue is a promise, not a repair, and must not be reported as one.
        assert outcome.repaired == []

    async def test_a_stale_kite_session_is_pulled_from_the_desk(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _maker(committed)() as session, session.begin():
            _, actor = await _seed(session)
        pulls: list[bool] = []

        def pull() -> bool:
            pulls.append(True)
            return True

        monkeypatch.setattr(kite_session_cli, "refresh_quietly", pull)
        _absent_token(monkeypatch)

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        assert pulls == [True]
        assert any("Kite session" in line for line in outcome.repaired)


class TestItIsIdempotent:
    """G3, and house rule 7: re-running any day's job produces identical rows."""

    async def test_the_second_press_changes_nothing(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            # Before the first published run, so the repaired day carries no class-(a) finding
            # afterwards and "nothing is pending" is reachable.
            thin = days[2]
            await session.execute(text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin})
            await _add_bars(session, thin, 25)

        async def press() -> resync_task.ResyncOutcome:
            return await resync_task.run_resync(
                actor,
                days=WINDOW,
                settings=settings(migrated_url),
                database_url=migrated_url,
                provider=FakeNSE(serves=[thin]),
                enqueue=RecordingQueue(),
                now=NOW,
            )

        first = await press()
        async with _maker(committed)() as session:
            after_first = await _checksums(session)

        second = await press()
        async with _maker(committed)() as session:
            after_second = await _checksums(session)

        assert first.pending_at_start == 1
        assert first.repaired
        assert second.pending_at_start == 0
        assert second.repaired == []
        assert second.queued == []
        assert second.complete
        assert after_second == after_first

    async def test_the_inspection_alone_writes_nothing(
        self, committed: AsyncEngine, migrated_url: str
    ) -> None:
        """The dry half of G1, asserted from the worker's side of the same detector."""
        async with _maker(committed)() as session, session.begin():
            _, actor = await _seed(session)
        async with _maker(committed)() as session:
            before = await _checksums(session)

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        async with _maker(committed)() as session:
            assert await _checksums(session) == before
        assert outcome.pending_at_start == 0
        assert outcome.complete
        # "I could not look" is reported on its own channel, and never as "I looked and it
        # was fine" — this host has no Kite token store, so the check could not run.
        assert any("No Kite token store is configured" in note for note in outcome.unresolved)


class TestItReportsWhatItCouldNotFix:
    """G7. A resync that fixes three of five gaps and reports success has lied."""

    async def test_a_day_nse_never_published_is_reported_as_a_failure_not_a_repair(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bhavcopy archive's UDiFF layout begins in 2024; some gaps simply cannot be closed
        from it. Saying so is the whole value — the alternative is an operator who believes the
        button worked."""
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            fixable, unfixable = days[1], days[2]
            for day in (fixable, unfixable):
                await session.execute(
                    text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": day}
                )
                await _add_bars(session, day, 25)

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[fixable]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        assert any(fixable.isoformat() in line for line in outcome.repaired)
        assert any(
            unfixable.isoformat() in line and "published no bhavcopy" in line
            for line in outcome.failed
        )
        assert any(unfixable.isoformat() in line for line in outcome.still_pending)
        assert outcome.complete is False
        async with _maker(committed)() as session:
            assert await _bars_on(session, fixable) == HEALTHY
            assert await _bars_on(session, unfixable) == 25

    async def test_an_unreachable_desk_does_not_stop_the_bar_repairs(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`kite_session_cli`: the day's bars come from the bhavcopy and need no credential, so a
        failed pull is a warning. Trading a working data plant for a broker login is the wrong way
        round — but it still has to be *reported*."""
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            thin = days[2]
            await session.execute(text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin})
            await _add_bars(session, thin, 25)
        monkeypatch.setattr(kite_session_cli, "refresh_quietly", lambda: False)
        _absent_token(monkeypatch)

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[thin]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        assert any(
            "Kite session" in line and "could not be reached" in line for line in outcome.failed
        )
        assert any(thin.isoformat() in line for line in outcome.repaired)
        assert outcome.complete is False
        async with _maker(committed)() as session:
            assert await _bars_on(session, thin) == HEALTHY

    async def test_a_broker_outage_is_one_line_rather_than_a_lost_repair(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            orphan = days[-1]
            await session.execute(
                text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": orphan}
            )

        outcome = await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[]),
            enqueue=RefusingQueue(),
            now=NOW,
        )

        assert any("no task broker is reachable" in line for line in outcome.failed)
        assert outcome.queued == []
        # A day the broker refused was never queued, so it must still read as pending. Excusing
        # it (as "one of the first MAX_RERUNS dates") would hide a broker outage behind an empty
        # pending list — the precise failure this report exists to prevent.
        assert any(orphan.isoformat() in line for line in outcome.still_pending)
        assert outcome.complete is False

    async def test_every_press_leaves_its_own_report_in_the_audit_trail(
        self, committed: AsyncEngine, migrated_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The page reads this row back. Without it, "what did the last resync manage" has no
        answer once the 202 has scrolled off the screen."""
        async with _maker(committed)() as session, session.begin():
            days, actor = await _seed(session)
            thin = days[2]
            await session.execute(text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin})
            await _add_bars(session, thin, 25)

        await resync_task.run_resync(
            actor,
            days=WINDOW,
            settings=settings(migrated_url),
            database_url=migrated_url,
            provider=FakeNSE(serves=[thin]),
            enqueue=RecordingQueue(),
            now=NOW,
        )

        async with _maker(committed)() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT actor_user_id, target, detail FROM admin_action "
                        "WHERE action = :action"
                    ),
                    {"action": RESYNC_COMPLETED_ACTION},
                )
            ).one()
        assert row[0] == actor
        assert ".." in row[1]
        detail = row[2]
        assert detail["pending_at_start"] == 1
        assert set(detail) >= {"repaired", "queued", "failed", "deferred", "still_pending"}


def _nightlies(queue: RecordingQueue) -> list[tuple[str, list[object]]]:
    return [entry for entry in queue.sent if entry[0] == resync_task.NIGHTLY]


def _no_token_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Kite store configured — so class (d) never fires and the assertion is about the rest."""
    monkeypatch.setattr(
        "baskfy_providers.settings.get_provider_settings",
        lambda: ProviderSettings(_env_file=None, kite_token_path="", kite_token_encryption_key=""),
    )


def _absent_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured store whose file does not exist — class (d)'s "no session at all" shape."""
    monkeypatch.setattr(
        "baskfy_providers.settings.get_provider_settings",
        lambda: ProviderSettings(
            _env_file=None,
            kite_token_path="/nonexistent/resync-test/kite-token.enc",
            kite_token_encryption_key=Fernet.generate_key().decode(),
        ),
    )
