"""What TW9 writes is what the Backtest tab reads — the other half of the 12 Sep 2026 seam.

THE SAME BUG, ONE ROOM ALONG
----------------------------
An hour after ``GET /twt/today`` was found missing, ``GET /twt/backtest`` was found missing in
exactly the same way: ``apps/web/src/lib/twt/fetch.ts`` has asked for it since TW8,
``readOrNull`` turns its 404 into ``null``, and ``null`` is the card's empty state. TW9 had
already built the writer — ``tools/twt/backtest.py``, ``baskfy_worker.tasks.twt_backtest`` and the
``tw_backtest_run`` table, eight gates green — so the product could produce a settled result and
still render "No completed run has been recorded yet".

**And this half was the quieter one, which is why it needed a test more and not less.**
``tw_backtest_run`` holds **0 rows on the box**, so the empty state has been telling the truth by
accident. It would have gone on saying the same sentence, word for word, the first evening a
backtest actually ran. A reader that does not exist and a writer that has not run are the same
silence; only one of them is a bug, and the page cannot tell you which. That is precisely the
failure a test either side of the wire cannot see:

* ``services/worker/tests/test_twt_backtest_job.py`` proves the job writes the row, with all
  seventeen keys the card reads. It never asks how a page would get at them.
* ``apps/web/src/components/twt/__tests__`` prove the card renders **given a payload**, from
  hand-written fixtures. They never ask whether anything serves one.

WHAT THIS MODULE ASSERTS
------------------------
One claim, in six shapes: **what the job writes is what the reader reads, and it never arrives
without its conditions.**

1. **Nothing serves the numbers at all** (the bug as it happened) — the settled run comes back
   through the path ``fetch.ts`` asks for, with its figures byte-identical to what was stored.
2. **The conditions travel with the numbers.** ``docs/twt/01`` §8 is not commentary on the
   result; it is the terms under which 22 % means anything, and house rule 9 puts disclaimers in
   the payload rather than under it.
3. **TW9's drift flag travels too.** The plant's own run *disagrees with the study* — 22.17 % at
   -26.47 % on 169 trades against 20.92 / -24.7 / 164 — and a route that served the figure
   without the flag would be serving a disputed number as a settled one.
4. **A run in flight or a failed re-run never displaces the last good number** (``03`` §9).
5. **The empty case is a 200 that says which absence it is** — not a 404, and not one sentence
   that is right two times in three.
6. **The tenant.** A sleeve is one person's money; another book's run is not this one's.

WHY THE ROW IS SEEDED RATHER THAN COMPUTED
-------------------------------------------
Its twin, ``test_twt_scan_to_page.py``, runs the real writer because a detection is one session
over a small panel. A backtest is not: TW9's run is nine years over 3.7 million bars and takes
35 seconds against a fully backfilled plant, and a fixture small enough to run here would not
produce the 260-session window ``04`` §2.3 needs — it would raise, and the test would be
asserting the failure path. So the **shape** is the writer's, taken from
``baskfy_worker.tasks.twt_backtest.stats_payload`` and from the numbers ``gates/twt-9.md``
recorded for the run that actually happened, and the seam under test is the one that was broken:
the row in the table, and the route that has to find it.

This module **never commits**: it runs inside the rolled-back ``screener_session`` transaction,
like every other db-marked suite in this directory.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from pathlib import Path
from typing import Final

import api_helpers
import pytest
import sqlalchemy as sa
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import twt as twt_service
from baskfy_api.settings import Settings
from baskfy_core.models import TwBacktestRun, TwConfig

pytestmark = [requires_db, pytest.mark.db]

#: ``services/api/tests`` -> ``services/api`` -> ``services`` -> ``decile-blueprint``.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: The module the Backtest tab reads through. Not edited here — a third agent owns it — but
#: *read*, so the URL this suite drives is the URL the page asks for and not one this file
#: invented. The bug **is** a disagreement between two files; a test that spelt the route out
#: again would be a third place for the same string to drift.
FETCH_TS: Final = REPO_ROOT / "apps" / "web" / "src" / "lib" / "twt" / "fetch.ts"

#: ``fetchBacktest`` is one line: ``return readOrNull<TwtBacktest>("/twt/backtest")``.
_BACKTEST_PATH_RE: Final = re.compile(r"readOrNull<TwtBacktest>\(\s*[\"']([^\"']+)[\"']")

#: ``docs/twt/01`` §8, the document this service serves its caveats out of.
CAVEATS_DOC: Final = REPO_ROOT.parent / "docs" / "twt" / "01-method.md"

#: The keys ``TwtBacktestRun`` declares in ``fetch.ts`` and the card binds to.
RUN_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "source",
    "started_at",
    "finished_at",
    "params",
    "stats",
    "drift",
    "error",
)

#: What TW9's run from the plant's own bars actually measured (``gates/twt-9.md`` G5/G6), as the
#: job stores it: every figure a **decimal string**, because house rule 9 runs all the way to the
#: database and the page rounds exactly once.
PLANT_STATS: Final[dict[str, object]] = {
    "cagr_pct": "22.17",
    "max_drawdown_pct": "-26.47",
    "calmar": "0.84",
    "sharpe": "1.21",
    "trades": 169,
    "win_rate_pct": "41.42",
    "profit_factor": "3.11",
    "avg_hold_sessions": "105.00",
    "exposure_pct": "78.40",
    "in_sample_cagr_pct": "11.04",
    "out_of_sample_cagr_pct": "36.21",
    "in_sample_window": "2017-10-16 → 2022-12-30",
    "out_of_sample_window": "2023-01-02 → 2026-09-11",
    # `05` §3's one number that justifies the gate. Nothing else on the card fills this cell.
    "gate_off_cagr_pct": "14.91",
    "gate_off_max_drawdown_pct": "-48.11",
    "gate_off_trades": 249,
    "yearly": [
        {"year": 2024, "return_pct": "41.30", "trades": 24, "win_rate_pct": "50.00"},
        {"year": 2025, "return_pct": "-3.80", "trades": 19, "win_rate_pct": "31.58"},
    ],
    "equity_curve": [
        {"date": "2017-10-16", "equity_inr": "1000000.00"},
        {"date": "2026-09-11", "equity_inr": "6180422.45"},
    ],
    "clamped_below_stop": 0,
}

#: ``03`` §9's comparison against ``01`` §6, exactly as ``baskfy_core.twt.drift.Drift.to_json``
#: writes it. **Flagged**, because the plant's run really is more than a CAGR point out.
PLANT_DRIFT: Final[dict[str, object]] = {
    "cagr_pct_delta": "1.25",
    "max_dd_pct_delta": "-1.77",
    "trades_delta": 5,
    "flagged": True,
    "published_cagr_pct": "20.92",
    "run_cagr_pct": "22.17",
    "threshold_cagr_points": "1.0",
}

#: ``03` §9: written on the way **in**, and sized against its own parameter. ₹10 lakh — the amount
#: ``01`` §6's numbers were produced at, and deliberately not whatever the live book is funded
#: with, which is the one thing about this table that must never be confused.
PLANT_PARAMS: Final[dict[str, object]] = {
    "start": "2017-10-16",
    "end": "2026-09-11",
    "sleeve_inr": "1000000",
    "cost_bps_per_side": "25",
    "source": "PLANT",
}

STARTED: Final = dt.datetime(2026, 9, 11, 21, 5, tzinfo=dt.UTC)
FINISHED: Final = dt.datetime(2026, 9, 11, 21, 5, 35, tzinfo=dt.UTC)


def page_read_path() -> str:
    """The path ``fetch.ts``'s ``fetchBacktest`` asks the API for, read out of the file."""
    source = FETCH_TS.read_text(encoding="utf-8")
    found = _BACKTEST_PATH_RE.search(source)
    assert found is not None, (
        f"{FETCH_TS} no longer names the backtest path in a form this test can read. That is not "
        "a reason to weaken the test: find the path the page reads and teach the regex, because "
        "this suite is worthless if it drives a URL the page does not use."
    )
    return found.group(1)


def _normalised(text: str) -> str:
    """Whitespace collapsed to single spaces, so a paragraph's line wrapping is not its identity.

    The document wraps at 96 columns and the constant does not; everything else — the numbers, the
    em-dashes, the rupee signs, the markdown emphasis — has to match character for character, so
    only whitespace is touched. NFC first, because the arrow and the en dash must compare equal
    whichever normalisation form the two files were saved in.
    """
    return " ".join(unicodedata.normalize("NFC", text).split())


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, email: str = "twt-bt-sole@example.com"
) -> tuple[int, str]:
    """The one person whose book this is, and a ``tw_config`` row for them."""
    user_id, public_id = await make_user(session, email)
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(TwConfig(user_id=user_id, updated_by="test"))
    await session.flush()
    return user_id, public_id


async def _seed_run(  # noqa: PLR0913 - one keyword per column the stored row records
    session: AsyncSession,
    *,
    user_id: int,
    source: str = "PLANT",
    started_at: dt.datetime = STARTED,
    finished_at: dt.datetime | None = FINISHED,
    stats: dict[str, object] | None = None,
    drift: dict[str, object] | None = None,
    error: str | None = None,
) -> int:
    """One ``tw_backtest_run`` row, in the shape TW9's job writes.

    Append-only, so nothing here ever edits one: a test that needed to is testing a path the
    product does not have.

    **A column with no result is left unset, never assigned ``None``**, and that is not fussiness:
    SQLAlchemy's JSON types store a Python ``None`` as the JSON value ``null`` rather than as SQL
    ``NULL``, and a JSON ``null`` satisfies ``stats IS NOT NULL``. TW9's ``_open_run`` leaves the
    column off the constructor entirely, so the rows the product actually writes are SQL ``NULL``
    and "finished **and** carrying stats" means what it says. A fixture that assigned ``None``
    would be seeding a row the writer cannot produce, and failing the route for it.
    """
    optional: dict[str, object] = {}
    if stats is not None:
        optional["stats"] = dict(stats)
    if drift is not None:
        optional["drift"] = dict(drift)
    row = TwBacktestRun(
        user_id=user_id,
        source=source,
        params=dict(PLANT_PARAMS),
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        **optional,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _read_backtest(
    settings: Settings, session: AsyncSession, public_id: str, wrote: str = ""
) -> dict[str, object]:
    """Ask for the page's own path, and insist on an answer.

    ``fetch.ts`` swallows every failure into ``null`` so the card can render before the data
    exists. **That swallowing is what made the incident silent**, so this asserts the status
    rather than mirroring the tolerance: a reader that cannot answer is the bug, not a state.
    """
    async with running_app(settings, session) as client:
        response = await client.get(url(page_read_path()), headers=bearer(public_id))
    assert response.status_code == 200, (
        f"THE WRITER AND THE READER DISAGREE. {wrote}GET {url(page_read_path())} answered "
        f"{response.status_code}. This is the 12 Sep 2026 incident in its second room: TW9 wrote "
        f"a settled result and nothing serves it, so `readOrNull` turns this into null and the "
        f"card says no run has been recorded. Body: {response.text[:300]}"
    )
    body = response.json()
    assert isinstance(body, dict), f"the payload is an object in `TwtBacktest`: {body!r}"
    return body


def _runs(body: dict[str, object]) -> list[dict[str, object]]:
    runs = body.get("runs")
    assert isinstance(runs, list), f"`TwtBacktest.runs` is a list: {runs!r}"
    for run in runs:
        assert isinstance(run, dict), f"every entry of `runs` is a `TwtBacktestRun`: {run!r}"
    return [run for run in runs if isinstance(run, dict)]


def _by_source(body: dict[str, object], source: str) -> dict[str, object] | None:
    found = [run for run in _runs(body) if run.get("source") == source]
    assert len(found) <= 1, f"more than one settled {source} run came back: {found!r}"
    return found[0] if found else None


async def _count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(TwBacktestRun)
                .where(TwBacktestRun.user_id == user_id)
            )
        ).scalar_one()
    )


class TestTheRunReachesThePage:
    async def test_a_settled_run_is_a_page_with_numbers_on_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The regression.** Write the row TW9 writes, then read the page's own path.

        The assertion is not "the route exists": it is that the *same figures* come back, as the
        same strings. Every number in ``stats`` is a decimal string all the way to the database
        (house rule 9) precisely so the page can round exactly once; a route that parsed and
        re-rendered them would round a second time and nothing downstream would notice.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        run_id = await _seed_run(
            screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT
        )

        body = await _read_backtest(
            settings,
            screener_session,
            public_id,
            f"TW9 wrote one settled PLANT run (id {run_id}) at 22.17% CAGR for user {user_id}. ",
        )

        run = _by_source(body, "PLANT")
        assert run is not None, (
            "the settled run is in the table and did not come back. This is the bug: the card "
            f"renders its empty state over a stored result. Payload: {body!r}"
        )
        missing = [field for field in RUN_FIELDS if field not in run]
        assert missing == [], f"`TwtBacktestRun` is missing {missing}; the card binds to all of it"
        assert run["id"] == run_id
        stats = run["stats"]
        assert isinstance(stats, dict)
        assert stats["cagr_pct"] == "22.17", "the headline figure changed on the way out"
        assert stats["max_drawdown_pct"] == "-26.47"
        assert stats["trades"] == 169
        assert stats["gate_off_cagr_pct"] == "14.91", (
            "`05` §3's one argument for the gate did not survive the route; nothing else on the "
            "card fills that cell, so it would render as the reason a figure is missing"
        )
        assert stats["yearly"] == PLANT_STATS["yearly"]
        assert stats["equity_curve"] == PLANT_STATS["equity_curve"]
        assert run["params"] == PLANT_PARAMS, "the terms the run was asked for did not come back"

    async def test_the_drift_flag_travels_with_the_figure_it_disputes(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TW9's own run **is** flagged, and the flag is not optional freight.

        22.17 % against the study's 20.92 % is 1.25 points, over ``03`` §9's threshold of one. The
        card's warning names both numbers, so both have to arrive; a route that served the figure
        and dropped the dispute would turn a contested result into a settled one on the way
        through, which is the exact silence the drift comparison was built to prevent.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT)

        run = _by_source(await _read_backtest(settings, screener_session, public_id), "PLANT")
        assert run is not None
        drift = run["drift"]
        assert isinstance(drift, dict), f"`drift` did not come back as an object: {drift!r}"
        assert drift["flagged"] is True, "the run disagrees with the study and the page is not told"
        assert drift["run_cagr_pct"] == "22.17"
        assert drift["published_cagr_pct"] == "20.92"
        assert drift["cagr_pct_delta"] == "1.25"
        assert drift["trades_delta"] == 5

    async def test_the_caveats_arrive_with_the_numbers_and_are_the_documents_own_words(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``01`` §8, verbatim, in the same payload as the result it is about.

        House rule 9 — "disclaimers are components, not footers" — and ``05`` §3 restates it for
        this page by name: these are not fine print about a result, they are the conditions under
        which the result means anything, and a reader who has seen 22 % before reading them has
        already formed the belief they exist to prevent.

        Verbatim is checked against the document rather than against a second copy of the prose,
        so a caveat softened on its way to a reader fails here instead of passing a review. Only
        whitespace is normalised; every number, dash and rupee sign has to match.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT)

        body = await _read_backtest(settings, screener_session, public_id)
        caveats = body.get("caveats")
        assert isinstance(caveats, list) and caveats, (
            f"the numbers came back without the terms on reading them: {body!r}"
        )
        served = {str(item["id"]): str(item["text"]) for item in caveats if isinstance(item, dict)}
        assert set(served) == {"trades", "giveback", "history", "capital"}, (
            f"`01` §8 has four paragraphs and the payload carries {sorted(served)}"
        )

        document = _normalised(CAVEATS_DOC.read_text(encoding="utf-8"))
        for key, text in served.items():
            assert _normalised(text) in document, (
                f"the {key!r} caveat is not `docs/twt/01` §8's own wording. Either the document "
                "moved on and the constant did not, or a paragraph was softened on its way to a "
                "reader. Both are worth a red line: the numbers on this page are only readable "
                "under these terms."
            )

        # The quantities `05` §3 requires on the page, unrounded and unsoftened.
        joined = " ".join(served.values())
        for quantity in ("164 trades", "53 %", "601", "20 %", "₹10 lakh", "₹25 lakh"):
            assert quantity in joined, f"§8's {quantity!r} did not survive into the payload"

    async def test_a_failed_or_unfinished_rerun_never_displaces_the_last_good_number(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``03`` §9's rule, at the route rather than only in the page's ``latestFinished``.

        A failed run **has** a ``finished_at`` — the job sets one on failure too, so "still
        running" and "failed" are different states rather than the same silence — so "the latest
        finished" alone is the wrong query and would serve the failure. Finished *and* carrying
        stats is the pair that is right, and the worker's ``latest_finished``, this route and the
        page all filter on the same two facts so no two of them can disagree.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        good = await _seed_run(
            screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT
        )
        await _seed_run(
            screener_session,
            user_id=user_id,
            started_at=STARTED + dt.timedelta(days=1),
            finished_at=FINISHED + dt.timedelta(days=1),
            error="RuntimeError: the plant fell over\nTraceback (most recent call last): ...",
        )
        await _seed_run(
            screener_session,
            user_id=user_id,
            started_at=STARTED + dt.timedelta(days=2),
            finished_at=None,
        )

        run = _by_source(await _read_backtest(settings, screener_session, public_id), "PLANT")
        assert run is not None, "a failed re-run erased the settled result the card was showing"
        assert run["id"] == good
        assert run["error"] is None
        stats = run["stats"]
        assert isinstance(stats, dict) and stats["cagr_pct"] == "22.17"

    async def test_the_two_kinds_of_run_come_back_separately_and_are_never_mixed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``05`` §3: never mixed, never averaged.

        The plant's run and the research reproduction answer different questions — "does our data
        produce the study's result" and "does the study reproduce at all" — and an average of the
        two answers neither. The card gives them a column each, so the payload has to keep them
        apart by source rather than by position.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT)
        research = await _seed_run(
            screener_session,
            user_id=user_id,
            source="RESEARCH_EXPORT",
            stats={**PLANT_STATS, "cagr_pct": "20.92", "trades": 164},
        )

        body = await _read_backtest(settings, screener_session, public_id)
        plant = _by_source(body, "PLANT")
        export = _by_source(body, "RESEARCH_EXPORT")
        assert plant is not None and export is not None
        assert export["id"] == research
        assert isinstance(plant["stats"], dict) and plant["stats"]["cagr_pct"] == "22.17"
        assert isinstance(export["stats"], dict) and export["stats"]["cagr_pct"] == "20.92"

    async def test_another_books_run_is_not_this_books_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sleeve is one person's money. ``tw_backtest_run`` is scoped by ``user_id`` and a
        reader that forgot it would serve somebody else's result as this one's."""
        other_id, _ = await make_user(screener_session, "twt-bt-other@example.com")
        screener_session.add(TwConfig(user_id=other_id, updated_by="test"))
        await screener_session.flush()
        await _seed_run(screener_session, user_id=other_id, stats=PLANT_STATS, drift=PLANT_DRIFT)
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        body = await _read_backtest(settings, screener_session, public_id)
        assert _runs(body) == [], f"another book's run was served as this one's: {body!r}"


class TestTheEmptyCaseSaysWhichAbsenceItIs:
    async def test_no_run_at_all_is_a_two_hundred_that_says_so(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The state the box is actually in**: ``tw_backtest_run`` holds 0 rows.

        So a correct route answers an empty list there, and "the tab is still empty" is not
        evidence the route is broken. What makes the difference visible is that the answer *says
        why*: a 200 with a reason is a reader that ran and found nothing, where a 404 turned into
        ``null`` was a reader that was never built. The card cannot tell those apart on its own
        and spent a month not telling anyone.
        """
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        body = await _read_backtest(settings, screener_session, public_id)
        assert _runs(body) == []
        reason = body.get("reason")
        assert isinstance(reason, str) and reason, f"an empty answer with no reason: {body!r}"
        assert reason == twt_service.NO_RUN_AT_ALL
        assert "has been run" in reason and "not the same as a result of zero" in reason, (
            "the empty state must say the rule has never been replayed, not imply it was "
            "replayed and produced nothing"
        )
        assert body.get("caveats"), (
            "the conditions are part of the page whether or not there is a number on it — the "
            "card renders them above the empty state for the same reason"
        )

    async def test_a_run_in_flight_says_it_is_running_rather_than_that_none_exists(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "No settled run" is three different facts and only one of them is "nobody asked"."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, finished_at=None)

        body = await _read_backtest(settings, screener_session, public_id)
        assert _runs(body) == []
        assert body.get("reason") == twt_service.RUN_IN_FLIGHT

    async def test_a_failed_run_with_nothing_before_it_says_it_failed(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third absence. The figure is still withheld — a failed run must never be shown as
        a result — but the reader is told that is what happened rather than being told no
        backtest has ever been run, which would be false."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(
            screener_session,
            user_id=user_id,
            error="ValueError: the plant holds no bars for the three-weeks-tight universe",
        )

        body = await _read_backtest(settings, screener_session, public_id)
        assert _runs(body) == []
        assert body.get("reason") == twt_service.RUN_FAILED

    async def test_a_settled_run_carries_no_reason_at_all(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reason beside a number would be a page explaining an absence that is not there."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT)

        body = await _read_backtest(settings, screener_session, public_id)
        assert body.get("reason") is None


class TestTheRouteWritesNothing:
    async def test_reading_the_page_appends_no_row_and_starts_no_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``02`` Track C §4 at the seam rather than only over the source.

        ``test_twt_readonly.py`` asserts the verb and the decorators; this asserts the effect. A
        route that "helpfully" queued a backtest when it found none would be a second write on a
        surface whose one write is a decision recorded in DECISIONS-TW — and on this table it
        would be worse than that, because ``tw_backtest_run`` is append-only and a read that
        appended would rewrite its own history every time the page was opened.
        """
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        await _seed_run(screener_session, user_id=user_id, stats=PLANT_STATS, drift=PLANT_DRIFT)
        before = await _count(screener_session, user_id)

        for _ in range(3):
            await _read_backtest(settings, screener_session, public_id)

        assert await _count(screener_session, user_id) == before, (
            "reading the backtest page changed the table it reads"
        )
