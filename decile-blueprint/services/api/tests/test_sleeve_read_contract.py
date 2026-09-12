"""Agent C's audit: **do the three sleeves' writers and readers agree about which session and
which user?**

A writer and a reader that disagree about the session, or about the tenant, is a *silent*
blindness. Nothing raises. The page renders its calm empty state — "Nothing has been read for
this strategy yet" — and a person concludes the job did not run when in fact it ran and wrote
somewhere the reader never looks. That is what happened on `/twt` for user 1 on 2026-09-11: 58
tight names, 2 signals and a breadth row in the database, and an empty page above them.

This module is the **general case**. Each test below names one pair whose date rule or user rule
is not the same function on both sides. They are written to fail on the disagreement, because a
named, tested latent bug is worth more than a rushed fix — see `gates/sleeve-read-contract.md`
for the table and the evidence behind each one.

**None of these tests asserts current behaviour.** House rule 2: a test that only locked in what
the code happens to do today would not be worth writing. Each asserts the contract the two halves
would have to share for the page not to be able to lie.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from decimal import Decimal

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_seed import resolve_sole_user_id
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.settings import Settings
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    PipelineRun,
    SwConfig,
    SwMarketDaily,
    SwSetupDaily,
    VbBreadthDaily,
    VbConfig,
    VbSignalDaily,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.config import Gate, SignalState
from baskfy_worker.providers import _sole_user_id as worker_rule
from baskfy_worker.tasks.swing_scan_now import last_published_session as swing_rule
from baskfy_worker.tasks.twt_scan import latest_published_session as twt_rule
from baskfy_worker.tasks.vbt_rescan import latest_published_session as vbt_rule

pytestmark = [requires_db, pytest.mark.db]

#: The session the detector ran for. Everything below is about whether a reader agrees.
DETECTED = dt.date(2026, 8, 18)
#: The session before it — the last one that happened to produce a candidate.
PREVIOUS = dt.date(2026, 8, 17)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DESK_ROOT = REPO_ROOT.parent / "kite-momentum-rebalancer"


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


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


# --- C1 / C2: which session does the reader call "as of"? --------------------


class TestTheReadersDate:
    """The reader's `as_of` must be the session the **detector ran**, not the newest session that
    happened to produce a row in whichever table the reader picked."""

    async def test_swing_as_of_is_the_session_the_detector_ran(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C1 — DISAGREE.** `baskfy_api.swing.latest_setup_date` is
        `max(sw_setup_daily.date)`, so the swing page's whole clock hangs off a table that is
        *empty on any session with no candidate*.

        `baskfy_worker.tasks.swing._detect_swing` calls `write_market_row` unconditionally — a
        session with zero candidates still gets its `sw_market_daily` row, because "no flags
        today" is a fact worth writing. So on such a session the writer wrote 2026-08-18 and the
        reader answers 2026-08-17: yesterday's triggers and yesterday's gate, stamped with
        yesterday's date, on a page that looks perfectly current.

        The box makes this reachable rather than theoretical: `sw_setup_daily` for user 1 has run
        16, 15, 13, 14, 21, 22, 16, 19, 12, **9** candidates over ten sessions with the gate RED
        since 2026-09-04. Nine is not far from nought.

        VBT does not have this bug, and the next test is the control: it keys its clock on
        `vb_breadth_daily`, which the detector writes every session whatever the tape did.
        """
        user_id, public_id = await make_user(screener_session, "swing-clock@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        screener_session.add(SwConfig(user_id=user_id, updated_by="agent-c"))
        instrument_id = await _instrument(screener_session, "FLAGCO")
        # The previous session had a candidate.
        screener_session.add(
            SwSetupDaily(
                user_id=user_id,
                date=PREVIOUS,
                instrument_id=instrument_id,
                setup="FLAG",
                status="SETTING_UP",
                score=Decimal("62.06"),
                close=Decimal("144.75"),
                trigger=Decimal("149.60"),
                stop_ref=Decimal("141.86"),
                pivot_high=Decimal("149.60"),
                adj_factor=Decimal(1),
                adr_pct=Decimal("4.08"),
                turnover_avg=112_734_212,
                base_bars=35,
                locked_upper_circuit=False,
                sector_slug=None,
                listed_within_2y=False,
            )
        )
        # The session the detector actually ran: a market row, and no candidate met the bar.
        for on, gate in ((PREVIOUS, "AMBER"), (DETECTED, "RED")):
            screener_session.add(
                SwMarketDaily(
                    user_id=user_id,
                    date=on,
                    constituent_count=41,
                    pct_up_strong_1m=Decimal("3.8000"),
                    pct_new_52w_high=Decimal("1.2000"),
                    pct_above_ma_slow=Decimal("55.0000"),
                    index_slug="nifty-mid-small-400",
                    index_close=Decimal("20240.00"),
                    index_ma_fast=Decimal("20200.00"),
                    index_ma_slow=Decimal("20150.00"),
                    gate=gate,
                    exposure_level=0,
                    max_open_positions=2,
                    max_exposure_pct=Decimal("25.00"),
                    new_entries_allowed=gate != "RED",
                    parabolic_count=0,
                    detail={"funnel": {"instruments": 180, "liquid": 41, "candidates": {}}},
                )
            )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(public_id))

        assert response.status_code == 200
        body = response.json()
        assert body["as_of"] == DETECTED.isoformat(), (
            "the swing page is stamped with the last session that produced a candidate, not the "
            "last session the detector ran — so a session with no flags serves yesterday's "
            "triggers and yesterday's gate as if they were today's"
        )
        assert body["gate"] == "RED", "and the gate it shows is yesterday's too"

    async def test_vbt_as_of_is_the_session_the_detector_ran(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C2 — AGREE, and it is the control.** Same shape as C1; VBT passes because
        `baskfy_api.vbt._latest_breadth` reads `vb_breadth_daily`, which `run_detect_vbt` writes
        on every session it runs, signals or none.

        Keeping this test beside C1 is the point: the fix for C1 is not an invention, it is the
        rule the neighbouring sleeve already follows.
        """
        user_id, public_id = await make_user(screener_session, "vbt-clock@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        screener_session.add(
            VbConfig(
                user_id=user_id,
                sleeve_capital_inr=Decimal("1000000.00"),
                updated_by="agent-c",
            )
        )
        instrument_id = await _instrument(screener_session, "VOLCO")
        screener_session.add(
            VbSignalDaily(
                user_id=user_id,
                date=PREVIOUS,
                instrument_id=instrument_id,
                state=SignalState.SIGNAL.value,
                failed_filters=[],
                close=Decimal("149.60"),
                close_raw=Decimal("149.60"),
                limit_price=Decimal("152.59"),
                stop_price=Decimal("141.86"),
                rank_key=Decimal("1.0000"),
                locked_upper_circuit=False,
            )
        )
        for on, gate in ((PREVIOUS, Gate.OPEN), (DETECTED, Gate.SHUT)):
            screener_session.add(
                VbBreadthDaily(
                    user_id=user_id,
                    date=on,
                    universe_count=4_186,
                    measured_count=1_412,
                    above_count=881,
                    pct_above_dma=Decimal("62.4000"),
                    gate=gate.value,
                    dma_bars=200,
                    thin_session=False,
                    detail={"funnel": {"universe": 4_186, "signals": 0}},
                )
            )
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert response.status_code == 200
        assert response.json()["as_of"] == DETECTED.isoformat()


# --- C3: the three "Scan now" writers do not share one idea of "published" ----


class TestTheScanWritersSession:
    async def test_all_three_scans_agree_on_the_last_published_session(
        self, screener_session: AsyncSession
    ) -> None:
        """**C3 — DISAGREE.** "The last published session" is computed by two different queries:

        * swing (`baskfy_worker.tasks.swing_scan_now.last_published_session`) —
          `max(ohlcv_daily.date)`, "the newest date with a published bar";
        * VBT and TWT (`vbt_rescan.latest_published_session`, `twt_scan.latest_published_session`)
          — `max(pipeline_run.trade_date) WHERE data_version IS NOT NULL`, and their docstrings
          say why: the pipeline's date, not the calendar's.

        Bars exist before the run is published. They diverge precisely when the **data quality
        gate refuses a day** — which is not hypothetical: `pipeline_run` 48 on the box, trade_date
        2026-09-11, has `fetch_daily_bars succeeded rows_out=4358` followed by
        `data_quality_gate failed`, and `data_version` stayed NULL until the 22:49 re-run. In that
        four-hour window a swing "Scan now" would have re-detected a session the gate had refused,
        while VBT's and TWT's would correctly have answered 2026-09-10.
        """
        instrument_id = await _instrument(screener_session, "GATEDCO")
        # The chain published PREVIOUS.
        screener_session.add(
            PipelineRun(
                trade_date=PREVIOUS,
                status="succeeded",
                started_at=dt.datetime(2026, 8, 17, 13, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 8, 17, 13, 5, tzinfo=dt.UTC),
                data_version=101,
            )
        )
        # DETECTED's bars landed; the quality gate refused the day, so it was never published.
        screener_session.add(
            PipelineRun(
                trade_date=DETECTED,
                status="failed",
                started_at=dt.datetime(2026, 8, 18, 13, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 8, 18, 13, 5, tzinfo=dt.UTC),
                data_version=None,
            )
        )
        for on in (PREVIOUS, DETECTED):
            screener_session.add(
                OhlcvDaily(
                    instrument_id=instrument_id,
                    date=on,
                    open=Decimal("100.00"),
                    high=Decimal("110.00"),
                    low=Decimal("99.00"),
                    close=Decimal("105.00"),
                    close_raw=Decimal("105.00"),
                    volume=1_000_000,
                    volume_raw=1_000_000,
                    adj_factor=Decimal(1),
                    source="nse",
                )
            )
        await screener_session.flush()

        on_or_before = DETECTED
        answers = {
            "swing": await swing_rule(screener_session),
            "vbt": await vbt_rule(screener_session, on_or_before),
            "twt": await twt_rule(screener_session, on_or_before),
        }
        assert len(set(answers.values())) == 1, (
            "the three sleeves' scans do not share one answer to 'what is the last published "
            f"session': {answers}. The swing scan reads max(ohlcv_daily.date) and so would "
            "re-detect a day the data quality gate refused."
        )


# --- C4 / C5: the tenant, when BASKFY_SOLE_USER_ID is not a plain number -----


def _sole_user_readers() -> dict[str, pathlib.Path]:
    """Every place in the product that decides "who is the tenant" from the environment."""
    return {
        "worker": REPO_ROOT / "services/worker/src/baskfy_worker/providers.py",
        "api": REPO_ROOT / "services/api/src/baskfy_api/curated_seed.py",
        "seed": REPO_ROOT / "services/api/src/baskfy_api/seed.py",
        "desk": DESK_ROOT / "app/config.py",
        "monitor": DESK_ROOT / "app/swing_monitor.py",
    }


class TestTheTenant:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "C4 IS NOT FIXED, AND THIS IS THE RECORD OF THAT — Agent F, 12 Sep 2026. "
            "`kite-momentum-rebalancer/app/config.py:15` still reads "
            '`SOLE_USER_ID = int(os.getenv("BASKFY_SOLE_USER_ID", "1"))`, so a desk that lost the '
            "variable would keep confirming plan lines and placing orders as user 1 while the "
            "worker beside it wrote nothing. Latent, not live: the box sets it to 1 (C10). "
            "NOT FIXED BECAUSE THE SAFE FIX IS NOT A ONE-LINE FIX. `C.SOLE_USER_ID` has seven "
            "call sites in the desk — `core/gateway.py:54`, `swing_desk.py:1481`, "
            "`vbt_desk.py:827`, `twt_desk.py:1018`, and the three `*_execute.py` TenantIds "
            "builders — and it is read at *import* time, so turning the default into a refusal "
            "breaks every desk process and test that does not set the variable. Refusing lazily "
            "instead means changing those call sites, and `tests/test_swing_track_c.py:392` "
            "pins their exact spelling (`user_id=C.SOLE_USER_ID`) by scanning the source. That "
            "is a cross-tree change on the order path, in the tree this agent was told to stay "
            "out of, on a desk with swing auto-execute armed — and root CLAUDE.md's rails say a "
            "module never ends with the desk's tree broken. "
            "TO FIX: add `sole_user_id()` to `app/config.py` raising a named error when the "
            "variable is unset, move the seven call sites onto it, update the source-scanning "
            "assertion in `test_swing_track_c.py`, and run the desk suite green before "
            "committing. The moment that lands this test XPASSes and the marker must come off. "
            "Never make the desk's fallback anything but an explicit refusal."
        ),
    )
    async def test_no_module_invents_a_tenant_when_the_variable_is_unset(self) -> None:
        """**C4 — DISAGREE, and this is the dangerous half.**

        `BASKFY_SOLE_USER_ID` is read in five places and every one of them answers a *different*
        thing when it is unset:

        | module | unset answers |
        |---|---|
        | `baskfy_worker.providers._sole_user_id` | `None` — every nightly detector skips |
        | `baskfy_api.curated_seed.resolve_sole_user_id` | the e2e account, else 503 |
        | `baskfy_api.seed._sole_user_id` | `min(app_user.id)` |
        | `kite-momentum-rebalancer/app/config.py` | **the literal `1`** |
        | `kite-momentum-rebalancer/app/swing_monitor.py` | `0`, then refuses |

        The worker's docstring states the rule the whole product should keep — *"``None`` rather
        than a default of 1: the sleeve is keyed by user, and inventing a tenant for a deployment
        that never declared one would write another account's book."* The desk does exactly the
        thing that docstring forbids, and the desk is the half that reaches the gateway.

        This is not abstract on this deployment: `app_user` on the box holds six accounts and
        portfolios exist for **users 1 and 6**. A desk process that lost the variable would keep
        confirming plan lines, writing `tw_order`/`sw_position` rows and placing orders as user 1
        while the worker beside it wrote nothing at all.
        """
        offenders: list[str] = []
        for name, path in _sole_user_readers().items():
            if not path.exists():
                continue
            source = path.read_text()
            for line in source.splitlines():
                if "BASKFY_SOLE_USER_ID" not in line or line.lstrip().startswith("#"):
                    continue
                # A default that is a usable account id is a tenant invented out of nothing.
                for literal in ('"1"', "'1'", ", 1)", "= 1"):
                    if literal in line:
                        offenders.append(f"{name}: {line.strip()}")
        assert offenders == [], (
            "a module defaults BASKFY_SOLE_USER_ID to a real account id instead of refusing to "
            f"guess: {offenders}"
        )

    async def test_a_blank_variable_means_the_same_thing_to_the_writer_and_the_reader(
        self, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C5 — DISAGREE.** `BASKFY_SOLE_USER_ID=` (set, empty) is one line in one env file and
        three different outcomes:

        * the worker strips it, reads `None`, and every detector logs "skipped" — no rows;
        * `resolve_sole_user_id` does not strip and does not check, so `int("")` raises
          `ValueError` out of a request handler — a 500, not the 503 its own "not configured"
          branch was written to give;
        * the desk's `int(os.getenv(..., "1"))` raises `ValueError` at import.

        One typo, three failure modes, and only one of them says what is wrong.
        """
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", "")
        assert worker_rule() is None, "the worker already treats a blank variable as 'not set'"
        try:
            reader_answer: object = await resolve_sole_user_id(screener_session)
        except ValueError as error:  # pragma: no cover - the failure this test names
            pytest.fail(
                "the API reads a blank BASKFY_SOLE_USER_ID as a number and raises "
                f"{type(error).__name__}: {error} — the worker reads the same blank value as "
                "'no tenant configured' and skips quietly. The writer and the reader disagree "
                "about what an empty variable means."
            )
        except Problem as refusal:
            # AGENT F, 12 Sep 2026. This branch is the fix landing, not the assertion softening.
            #
            # The failure this test names is the *ValueError* — the branch above, and this
            # module's own prose: "a 500, not the 503 its own 'not configured' branch exists to
            # give". `resolve_sole_user_id` now reads the variable exactly as the worker does
            # (strip; empty is unset) and falls through to that branch, which on a database with
            # no seeded e2e account is `pipeline_degraded`. That IS the agreement the test is
            # about: the worker says "no tenant, skip", the API says "no tenant, and here is the
            # variable to set".
            #
            # The trailing `is not None` below was written expecting this fixture to carry the
            # e2e account. It must not be reached by widening the refusal into an answer: a
            # reader that invented a tenant where the writer refused to have one would be a
            # worse disagreement than the one this test was written to catch (C4).
            assert refusal.type is ProblemType.PIPELINE_DEGRADED
            assert refusal.status == 503
            assert "BASKFY_SOLE_USER_ID" in refusal.detail, (
                "the refusal must name the variable an operator has to set"
            )
            return
        assert reader_answer is not None

    async def test_a_non_numeric_variable_means_the_same_thing_to_both(
        self, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C6 — DISAGREE**, the same crack, widened. The worker logs a warning and skips; the
        API raises `ValueError` from inside a request."""
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", "maulik")
        assert worker_rule() is None
        try:
            await resolve_sole_user_id(screener_session)
        except ValueError as error:  # pragma: no cover - the failure this test names
            pytest.fail(
                "a mistyped BASKFY_SOLE_USER_ID is a warning-and-skip in the worker and an "
                f"unhandled {type(error).__name__} in the API: {error}"
            )
        except Problem as refusal:
            # As in C5: the API now answers the same "no tenant configured" the worker does,
            # loudly and in the shape an operator can act on, instead of raising ValueError out
            # of a request handler. See the note there.
            assert refusal.type is ProblemType.PIPELINE_DEGRADED
            assert refusal.status == 503
            assert "BASKFY_SOLE_USER_ID" in refusal.detail


# --- C7: the sleeve a second account cannot tell apart from an empty one -----


class TestASecondAccount:
    async def test_a_refused_reader_is_not_the_same_answer_as_an_empty_one(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C7 — DISAGREE, live on the box today.**

        The writer writes for the sole tenant (user 1). The reader calls `scoped_sole_user_id`,
        which **404s** any principal who is not the sole tenant. A 404 is the right answer for the
        API — M43.4 settled that, and collapsing a second account onto the first was the bug it
        fixed. The disagreement is one layer up: `apps/web/src/lib/{swing,vbt,twt}/fetch.ts` wrap
        every non-OK response in `readOrNull`, which returns `null`, and every page renders `null`
        as *"Nothing has been read for this strategy yet."*

        So the second account — and `app_user` on the box holds six, with portfolios for users 1
        **and 6** — is told the strategy has never run. The API knows it refused; the page cannot
        tell a refusal from an empty database. That is the same silent blindness as the TWT bug,
        arriving through the user axis rather than the date axis.

        This test asserts the API half of the contract that makes the page's two cases separable:
        the refusal must be distinguishable in the response, so the web layer *can* tell them
        apart. The web half is `apps/web/src/lib/swing/__tests__/read-contract.test.ts`.
        """
        sole_id, _ = await make_user(screener_session, "sole@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(sole_id))
        screener_session.add(SwConfig(user_id=sole_id, updated_by="agent-c"))
        _, other_public = await make_user(screener_session, "second@example.com")
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/swing/setups"), headers=bearer(other_public))

        assert response.status_code == 404
        detail = str(response.json().get("detail", "")).lower()
        assert "watchlist" not in detail, (
            "the refusal names a surface the caller never asked for. "
            f"GET /swing/setups answers {detail!r} — `scoped_sole_user_id` raises "
            '`not_found("watchlist", ...)` whatever route it is guarding, so the one record '
            "of the refusal points an operator at the curated-basket watchlist rather than at "
            "the swing book. Combined with `readOrNull` collapsing every non-OK response to "
            "null, a second account is told the strategy has never run and the server log says "
            "a watchlist is missing."
        )


# --- C8: the TWT hub has no reader at all ------------------------------------


class TestTheTwtReader:
    async def test_the_twt_hub_has_a_read_route(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**C8 — DISAGREE**, and it is the bug that started this audit. Agent R owns the fix;
        this is the assertion that it was a missing *route*, not a missing row.

        `apps/web/src/lib/twt/fetch.ts` asks for `/twt/today` and `/twt/backtest`.
        `services/api/src/baskfy_api/routers/twt.py` serves `POST /twt/scan` and
        `GET /twt/scan/{run_id}` and says so in its own module docstring: *"routes that do not
        exist yet and answer null by design, so the page renders its empty state. Serving those is
        somebody else's module."* Nobody built the module, and the empty state then stopped being
        a design and became a lie: 58 `tw_state_daily` rows, 2 `tw_signal_daily` rows and a
        `tw_breadth_daily` row for user 1 / 2026-09-11 sat under a page saying nothing had been
        read.

        **The rule the new route must follow is C1's**: key its `as_of` on `tw_breadth_daily`,
        which the detector writes every session. This strategy signals about eighteen times a
        year, so a reader keyed on `tw_signal_daily` would serve a session from *last month* and
        look perfectly current doing it.
        """
        user_id, public_id = await make_user(screener_session, "twt-reader@example.com")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
        await screener_session.flush()

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert response.status_code != 404, (
            "the web app's TWT hub reads GET /twt/today and the API serves no such route, so "
            "every render falls through to 'Nothing has been read for this strategy yet' over a "
            "database that holds the session"
        )
