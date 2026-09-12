"""What the scan wrote is what the page reads — the seam nobody was testing (12 Sep 2026).

WHAT HAPPENED
-------------
"Scan now" shipped with fifteen UI gates and twenty-four backend gates, every one of them green.
The button was pressed. The detector ran in nineteen seconds and wrote **58 tight names, two
signals and a breadth reading** to the production database. The page then said:

    "Nothing has been read for this strategy yet."

Thirty-nine green gates, and not one of them could have caught it, because **every one of them
stopped at its own boundary**:

* ``test_api_twt_scan.py`` proves the button writes a ``tw_scan_run`` row, publishes the task and
  moves no money. It never reads a name back.
* ``test_twt_detect.py`` and ``packages/core/tests/test_twt_*.py`` prove the detector writes
  ``tw_state_daily``, ``tw_signal_daily`` and ``tw_breadth_daily`` correctly. They never ask how
  a page would get at them.
* ``apps/web/src/app/(app)/twt/__tests__`` prove the page renders **given a payload**, and its
  fixtures are hand-written to ``docs/twt/03``. They never ask whether anything serves one.

The bug lived in the gap between them: ``apps/web/src/lib/twt/fetch.ts`` reads ``/twt/today`` and
**no such route exists** — ``routers/twt.py`` serves ``POST /twt/scan`` and ``GET
/twt/scan/{run_id}`` and nothing else. ``readOrNull`` turns the 404 into ``null``, ``null`` is the
empty state, and the empty state is a sentence that says the strategy has never been read. A
writer and a reader that never meet in one test can disagree for as long as they like.

WHAT THIS MODULE ASSERTS
------------------------
One claim, in four shapes: **what the writer writes is what the reader reads.**

It drives the *real* chain end to end in one transaction — ``POST /twt/scan`` over HTTP, then the
worker's own ``run_twt_scan`` (which picks the session itself, from ``pipeline_run``, exactly as
it does on the box), then the detector that call makes, then the **read path the page asks for**,
discovered by reading ``fetch.ts`` rather than by hard-coding a string here. The middle of that
chain is real code; only the Celery hop is collapsed, because a test that needed a live worker
would not be run.

Three ways a writer and a reader can disagree, one test each:

1. **Nothing serves the names at all** (the bug as it happened) — T1.
2. **They disagree about the session.** The reader must serve the session the run detected, and
   only that session's rows: not every row in the table, and not a date of its own choosing — T2.
3. **They disagree about the tenant.** Detection is scoped by ``user_id`` on all three tables; a
   reader that forgets it serves another book's names as this one's — T3.

And a fourth that keeps this module honest rather than the product: T4 asserts that the path
these tests drive is still the path ``fetch.ts`` asks for, so a rename cannot leave this suite
passing against a route the page has stopped using.

WHY THIS IS A PYTHON INTEGRATION TEST AND NOT A PLAYWRIGHT ONE
--------------------------------------------------------------
The disagreement is between a job that writes rows and an HTTP route that reads them. Both are
Python, both are exercisable against a real Postgres in a few seconds, and the whole of the
ambiguity — which session, which tenant, which table — is on that side of the wire. A browser
test would add a Next.js server, a login and thirty seconds, and would still be asserting the
same payload one layer further away, where a failure says "the page is empty" rather than "the
route is not there". ``fetch.ts`` itself is thin by design (a URL, a bearer token, a timeout,
``readOrNull``) and is covered by its own unit tests; what it cannot cover, and what this does, is
whether anything answers the URL.

This module **never commits**: it runs inside the rolled-back ``screener_session`` transaction,
like every other db-marked suite in this directory.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import api_helpers
import pytest
import sqlalchemy as sa
import test_twt_detect as detector_fixtures
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import AS_OF as PUBLISHED_SESSION
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import (
    AppUser,
    Instrument,
    TwBreadthDaily,
    TwConfig,
    TwScanRun,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.tasks.twt_scan import run_twt_scan

pytestmark = [requires_db, pytest.mark.db]

#: ``services/api/tests`` -> ``services/api`` -> ``services`` -> ``decile-blueprint``.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: The module the ``/twt`` page reads through. Not edited here — Agent R owns it — but *read*,
#: so the URL this suite drives is the URL the page asks for and not one this file invented.
FETCH_TS: Final = REPO_ROOT / "apps" / "web" / "src" / "lib" / "twt" / "fetch.ts"

#: ``fetchToday`` is one line: ``return readOrNull<TwtToday>("/twt/today", ...)``.
_TODAY_PATH_RE: Final = re.compile(r"readOrNull<TwtToday>\(\s*[\"']([^\"']+)[\"']")

#: The keys ``TwtToday`` declares and the page binds to. ``last_scan``/``last_scan_id`` are
#: deliberately absent: ``fetchLastScan`` documents both as optional and reads either.
PAGE_FIELDS: Final[tuple[str, ...]] = ("as_of", "gate", "tight", "positions", "half_size")

#: A prior session, for T2's "and only that session's rows".
EARLIER_SESSION: Final = PUBLISHED_SESSION - dt.timedelta(days=7)


def page_read_path() -> str:
    """The path ``fetch.ts``'s ``fetchToday`` asks the API for, read out of the file.

    Derived rather than written down, because the bug this module exists for **is** a hard-coded
    disagreement between two files. A test that spelt the route out again would be a third place
    for the same string to drift.
    """
    source = FETCH_TS.read_text(encoding="utf-8")
    found = _TODAY_PATH_RE.search(source)
    assert found is not None, (
        f"{FETCH_TS} no longer names the day's path in a form this test can read. That is not a "
        "reason to weaken the test: find the path the page reads and teach the regex, because "
        "this suite is worthless if it drives a URL the page does not use."
    )
    return found.group(1)


class RecordingQueue:
    """The Celery producer, recording. The worker is called directly below instead."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """The one person whose book this is, and a ``tw_config`` row for them."""
    user_id, public_id = await make_user(session, "twt-e2e-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(TwConfig(user_id=user_id, updated_by="test"))
    await session.flush()
    return user_id, public_id


async def _panel(session: AsyncSession) -> dict[str, str]:
    """Bars that make the detector find something, ending on the **published** session.

    The shape is ``test_twt_detect``'s, imported rather than copied: it is the panel TW4's own
    acceptance suite proves produces a tight state, a signal and a liquidity reject, and a second
    hand-drawn copy of it here would drift from the detector the first time a threshold moved.
    The one difference is the as-of — this panel ends on the session ``pipeline_run`` has
    published, because that is the session ``run_twt_scan`` will choose for itself.

    Returns the fixture's name -> the exchange symbol it was given.
    """
    panel = await detector_fixtures.build_panel(
        session, as_of=PUBLISHED_SESSION, names=detector_fixtures.STANDARD
    )
    rows = await session.execute(
        sa.select(Instrument.id, Instrument.symbol).where(
            Instrument.id.in_(list(panel.ids.values()))
        )
    )
    by_id = {int(instrument_id): symbol for instrument_id, symbol in rows}
    return {name: by_id[instrument_id] for name, instrument_id in panel.ids.items()}


async def _scan(settings: Settings, session: AsyncSession, public_id: str) -> dt.date:
    """Press the button, run the worker the press asked for, and return the session it detected.

    **The session is the worker's, never this file's.** ``run_twt_scan`` resolves it from
    ``pipeline_run`` — the product's own answer to "what is the latest session" — and every
    assertion below is made against what it chose. A test that told the chain which day to work
    on would be unable to catch a reader that guessed a different one.

    The Celery hop is the one link collapsed — ``POST /twt/scan`` publishes
    ``baskfy.twt.scan(run_id)`` to a recorder, and this calls what that task calls, with the
    run id the route issued. Everything either side of the message is the real path.
    """
    queue = RecordingQueue()
    async with running_app(settings, session, task_queue=queue) as client:
        posted = await client.post(url("/twt/scan"), headers=bearer(public_id))
    assert posted.status_code == 202, posted.text
    run_id = int(posted.json()["run_id"])
    assert queue.sent == [("baskfy.twt.scan", [run_id])]

    await run_twt_scan(session, run_id)
    row = (await session.execute(sa.select(TwScanRun).where(TwScanRun.id == run_id))).scalar_one()
    assert row.status == "DONE", f"the scan itself failed, before any reader: {row.error}"
    assert row.session_date is not None, "a DONE run names the session it detected"
    return row.session_date


async def _state_symbols(session: AsyncSession, *, user_id: int, day: dt.date) -> set[str]:
    """What the writer put in ``tw_state_daily`` for that book on that session."""
    rows = await session.execute(
        sa.select(Instrument.symbol)
        .join(TwStateDaily, TwStateDaily.instrument_id == Instrument.id)
        .where(TwStateDaily.user_id == user_id, TwStateDaily.date == day)
    )
    return {symbol for (symbol,) in rows}


async def _read_today(
    settings: Settings, session: AsyncSession, public_id: str, wrote: str = ""
) -> object:
    """Ask for the day exactly as the page's server-side read does, and insist on an answer.

    ``fetch.ts`` swallows every failure into ``null`` so the page can render an empty state before
    the data exists (``readOrNull``). **That swallowing is what made the incident silent**, so
    this asserts the status rather than mirroring the tolerance: a reader that cannot answer is
    the bug, not a state.

    ``wrote`` is what the writer had just put in the database, quoted back in the failure. A red
    line that says only "404" reads as a missing route; one that says "the scan wrote 3 names and
    3 signals on 2026-08-18 and the page's path answered 404" is the incident, in one line.
    """
    async with running_app(settings, session) as client:
        response = await client.get(url(page_read_path()), headers=bearer(public_id))
    assert response.status_code == 200, (
        f"THE WRITER AND THE READER DISAGREE. {wrote}GET {url(page_read_path())} answered "
        f"{response.status_code}. This is the incident of 12 Sep 2026: the scan wrote its rows "
        f"and nothing serves them, so `readOrNull` turns this into null and the page says the "
        f"strategy has never been read. Body: {response.text[:300]}"
    )
    return response.json()


async def _wrote(session: AsyncSession, *, user_id: int, day: dt.date) -> str:
    """One sentence naming what the detector left behind, for the failure message."""
    names = len(await _state_symbols(session, user_id=user_id, day=day))
    signals = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(TwSignalDaily)
            .where(TwSignalDaily.user_id == user_id, TwSignalDaily.date == day)
        )
    ).scalar_one()
    breadth = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(TwBreadthDaily)
            .where(TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == day)
        )
    ).scalar_one()
    return (
        f"The scan wrote {names} tight names, {signals} signals and {breadth} breadth reading(s) "
        f"for user {user_id} on {day.isoformat()}. "
    )


def _tight_symbols(body: object) -> set[str]:
    assert isinstance(body, dict), f"the day's payload is an object in `TwtToday`: {body!r}"
    tight = body.get("tight")
    assert isinstance(tight, list), f"`TwtToday.tight` is a list of names: {tight!r}"
    symbols: set[str] = set()
    for row in tight:
        assert isinstance(row, dict) and "symbol" in row, (
            f"every `TwtTightName` carries a `symbol` — the page renders it: {row!r}"
        )
        symbols.add(str(row["symbol"]))
    return symbols


class TestTheScanReachesThePage:
    async def test_a_scan_that_wrote_names_is_a_page_with_names_on_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The regression.** Press the button, then read the page's own path.

        The assertion is not "the route exists": it is that the *same names* come back. The
        detector's answer is read out of ``tw_state_daily`` first, so the expected set is what the
        writer actually wrote on the day rather than a list this file believes it should have
        written — which is the only form of the assertion that stays true when the fixture, the
        thresholds or the universe change.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        symbols = await _panel(screener_session)
        day = await _scan(settings, screener_session, public_id)

        assert day == PUBLISHED_SESSION, (
            "the worker detected a session other than the published one; the fixture and the "
            "scan are no longer talking about the same day"
        )
        written = await _state_symbols(screener_session, user_id=user_id, day=day)
        assert symbols["TIGHTLIQ"] in written, (
            "the writer wrote nothing to assert about — this is a fixture failure, not the "
            f"regression: {sorted(written)}"
        )
        signals = (
            await screener_session.execute(
                sa.select(sa.func.count())
                .select_from(TwSignalDaily)
                .where(TwSignalDaily.user_id == user_id, TwSignalDaily.date == day)
            )
        ).scalar_one()
        assert signals, "the panel produced no signal row; the fixture is not exercising the seam"

        body = await _read_today(
            settings,
            screener_session,
            public_id,
            await _wrote(screener_session, user_id=user_id, day=day),
        )
        assert isinstance(body, dict)
        missing = [field for field in PAGE_FIELDS if field not in body]
        assert not missing, f"`TwtToday` fields the page binds to are absent: {missing}"
        assert body["as_of"] == day.isoformat(), (
            "the page would stamp a different day on the names than the scan detected"
        )
        gate = body["gate"]
        assert isinstance(gate, dict) and gate.get("gate") in ("OPEN", "SHUT"), (
            "`gate.gate` is null, which is the exact condition the page renders "
            '"Nothing has been read for this strategy yet" on, after a scan that read '
            f"{len(written)} names: {gate!r}"
        )
        assert _tight_symbols(body) >= written, (
            "the scan wrote these names and the page's read path does not serve them: "
            f"{sorted(written - _tight_symbols(body))}"
        )

    async def test_the_reader_serves_the_run_s_session_and_only_that_session(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which session. A name this book was tight on a week ago must not be on today's screen.

        A reader that asks the table for "every row", or for a date of its own choosing, answers
        with a mixture — and a mixture of two sessions' tight names is a screen nobody can audit
        (house rule 5's sibling: point-in-time on the way out as well as in).

        **The planted row is on an instrument the session's own rows do not name**, which is what
        makes the assertion able to fail: a copy of the same names onto another date would come
        back as the same set of symbols and a reader reading every session would look correct.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _panel(screener_session)
        day = await _scan(settings, screener_session, public_id)
        stale = await _plant_a_tight_name(
            screener_session, user_id=user_id, day=day, onto_day=EARLIER_SESSION
        )

        body = await _read_today(
            settings,
            screener_session,
            public_id,
            await _wrote(screener_session, user_id=user_id, day=day),
        )
        assert isinstance(body, dict)
        assert body["as_of"] == day.isoformat()
        served = _tight_symbols(body)
        on_the_day = await _state_symbols(screener_session, user_id=user_id, day=day)
        assert stale not in served, (
            f"{stale} was tight on {EARLIER_SESSION} and not on {day}, and the page served it "
            "anyway: the reader is not reading the session the run detected"
        )
        assert served == on_the_day, (
            "the page's names are not this session's names. Served but not written on "
            f"{day}: {sorted(served - on_the_day)}; written but not served: "
            f"{sorted(on_the_day - served)}"
        )

    async def test_another_tenant_s_names_are_not_served_as_this_one_s(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which user. All three detection tables are scoped by ``user_id``; the reader must be.

        The foreign row sits on **the same session** and on an instrument this book has no row
        for, so the only thing keeping it off the page is the ``user_id`` filter. A reader that
        filters on the date alone serves it, and the page then shows a name that was never
        detected for the person reading it.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _panel(screener_session)
        day = await _scan(settings, screener_session, public_id)

        stranger = AppUser(public_id="twt-e2e-other", email="twt-e2e-other@example.com")
        screener_session.add(stranger)
        await screener_session.flush()
        theirs = await _plant_a_tight_name(
            screener_session, user_id=user_id, day=day, onto_user=int(stranger.id)
        )

        body = await _read_today(
            settings,
            screener_session,
            public_id,
            await _wrote(screener_session, user_id=user_id, day=day),
        )
        mine = await _state_symbols(screener_session, user_id=user_id, day=day)
        served = _tight_symbols(body)
        assert theirs not in served, (
            f"{theirs} is another tenant's tight name on the same session and this page served "
            "it: the reader is not scoped to the book that asked"
        )
        assert served == mine, (
            "the day's names are not the ones detected for the book that asked. Served but not "
            f"detected for user {user_id}: {sorted(served - mine)}; detected but not served: "
            f"{sorted(mine - served)}"
        )

    async def test_the_path_this_suite_drives_is_the_path_the_page_asks_for(self) -> None:
        """Keeps the module honest rather than the product.

        If ``fetch.ts`` is given a different path and this file is not, every test above would go
        on passing against a route nobody reads. The page's own module is the source of truth for
        the string; this only asserts the reading of it produced one API path.
        """
        path = page_read_path()
        assert path.startswith("/twt/"), path
        assert url(path) == f"/api/v1{path}"


async def _plant_a_tight_name(
    session: AsyncSession,
    *,
    user_id: int,
    day: dt.date,
    onto_day: dt.date | None = None,
    onto_user: int | None = None,
) -> str:
    """Copy one detected state row onto another session, another book, or both — **and onto a
    new instrument** — and return the symbol it now names.

    Three things make this the right shape for T2 and T3:

    * **It is a real row.** ``tw_state_daily`` carries twenty-odd columns and six check
      constraints; a hand-filled fixture would be asserting the schema, and would need editing
      every time the detector gained a column. The projection is driven off the table's own
      column list, so it copies whatever is there.
    * **It lands on an instrument the real session has no row for.** A copy that reused the
      session's own instruments would come back as the same *set of symbols*, and a reader that
      ignored the date or the tenant would still look correct. The marker is the whole assertion.
    * **The instrument has no bars**, so the detector could never have found it — the only way it
      can reach a page is a reader reading rows it was not asked for.
    """
    marker = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=f"PLANTED{uuid.uuid4().hex[:6].upper()}",
        name="PLANTED LIMITED",
        series="EQ",
        instrument_type="EQ",
        is_active=True,
    )
    session.add(marker)
    await session.flush()

    columns = [column.name for column in TwStateDaily.__table__.columns]
    overrides = {"date": ":onto_day", "user_id": ":onto_user", "instrument_id": ":marker"}
    projection = ", ".join(overrides.get(name, f"t.{name}") for name in columns)
    await session.execute(
        sa.text(
            f"INSERT INTO tw_state_daily ({', '.join(columns)}) "
            f"SELECT {projection} FROM tw_state_daily AS t "
            "WHERE t.user_id = :user_id AND t.date = :day "
            "ORDER BY t.instrument_id LIMIT 1"
        ),
        {
            "user_id": user_id,
            "day": day,
            "onto_day": onto_day if onto_day is not None else day,
            "onto_user": onto_user if onto_user is not None else user_id,
            "marker": int(marker.id),
        },
    )
    await session.flush()
    planted = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(TwStateDaily)
            .where(TwStateDaily.instrument_id == marker.id)
        )
    ).scalar_one()
    assert planted == 1, "nothing was planted, so the assertion that follows proves nothing"
    return str(marker.symbol)
