"""SW18 — the 08:45 Kite login nudge (docs/swing/DECISIONS-SW SW18.1).

What is asserted here is the spec, not the implementation (house rule 2): a fresh token buys
silence, a stale or absent one buys exactly one message carrying a link Kite would accept, the
same morning never produces two of the same message, a mailer that falls over is a note rather
than a failed task, and nothing secret is ever rendered.

Needs a database only for the NSE calendar — ``is_session_day`` is what tells a holiday from a
working Thursday, and Beat's ``mon-fri`` cannot.
"""

from __future__ import annotations

import datetime as dt
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from celery.schedules import crontab
from cryptography.fernet import Fernet
from helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_oauth import (
    KITE_AUTHORIZE_URL,
    KiteLoginUrl,
    clear_oauth_states,
    consume_oauth_state,
    kite_login_url,
)
from baskfy_api.email import Mailer, Message
from baskfy_api.routers import brokers as brokers_router
from baskfy_api.swing_health import IST
from baskfy_core.models import TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker import settings as worker_settings
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import celery_tasks, kite_login_nudge
from baskfy_worker.tasks.kite_login_nudge import (
    SUBJECT,
    NudgeReport,
    NudgeWindow,
    _claim,  # the atomic claim, exercised directly by the race test
    marker_path,
    run_login_nudge,
)

pytestmark = [requires_db, pytest.mark.db]

#: A Thursday. The same day the SW11 suite uses, for the same reason: an ordinary session.
DAY = dt.date(2026, 9, 3)
HOLIDAY = dt.date(2026, 9, 4)  # a Friday this suite marks closed
SATURDAY = dt.date(2026, 9, 5)

API_KEY = "pubkfakeapikey01"
API_SECRET = "s3cr3t-api-secret-never-in-a-body"
TOKEN_VALUE = "livetoken-never-in-a-body"


def _at(hhmm: str, day: dt.date = DAY) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


class Sink:
    """A transport that keeps what it was handed. ``sent`` is what every test reads."""

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.calls = 0

    async def send(self, message: Message) -> None:
        self.calls += 1
        self.sent.append(message)


class Recording(Sink):
    """The ordinary one: everything handed to it arrives."""


class Exploding(Sink):
    """A transport whose failure is not the polite ``EmailNotSent`` one.

    `Mailer.deliver` catches that; nothing catches this but the job itself, which is the point.
    """

    async def send(self, message: Message) -> None:
        self.calls += 1
        raise RuntimeError("the relay is on fire")


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _store(state_dir: Path, key: str) -> AccessTokenStore:
    return AccessTokenStore(state_dir / "broker-token.enc", key)


@pytest.fixture(autouse=True)
def _clean_oauth_states() -> None:
    clear_oauth_states()


def _link(window: NudgeWindow) -> KiteLoginUrl:
    return kite_login_url(api_key=API_KEY, user_id=43)


async def _calendar(session: AsyncSession) -> None:
    """One trading day and one holiday, so the calendar branch is a fact and not a mock.

    ``merge`` rather than ``add``: the suite's conftest seeds the real NSE calendar, which
    already carries both dates as ordinary weekdays. The holiday is this test's invention and
    has to overwrite the seeded row.
    """
    await session.merge(
        TradingDay(exchange_id=NSE_EXCHANGE_ID, date=DAY, is_trading_day=True, source="bhavcopy")
    )
    await session.merge(
        TradingDay(
            exchange_id=NSE_EXCHANGE_ID,
            date=HOLIDAY,
            is_trading_day=False,
            holiday_name="a holiday this suite invents",
            source="holiday",
        )
    )
    await session.flush()


async def _run(  # noqa: PLR0913 - a test helper mirroring the job's own seams
    session: AsyncSession,
    store: AccessTokenStore,
    state_dir: Path,
    *,
    window: NudgeWindow = NudgeWindow.FIRST,
    now: dt.datetime | None = None,
    to: str = "maulik@example.com",
    transport: Sink | None = None,
) -> tuple[NudgeReport, Sink]:
    sink = transport if transport is not None else Recording()
    report = await run_login_nudge(
        session,
        StepOutcome(),
        now=now or _at("08:45"),
        token_store=store,
        mailer=Mailer(sink),
        login_url_for=_link,
        to=to,
        state_dir=state_dir,
        window=window,
    )
    return report, sink


class TestTheTokenDecidesWhetherAnyoneIsDisturbed:
    async def test_a_token_issued_today_sends_nothing(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """`AccessTokenStore.require_fresh` semantics: issued on today's IST date is usable."""
        await _calendar(session)
        store = _store(state_dir, key)
        store.save(TOKEN_VALUE, issued_at=_at("08:12"))

        report, sink = await _run(session, store, state_dir)

        assert sink.sent == []
        assert report.sent is False
        assert "already stored" in " ".join(report.notes)

    async def test_yesterdays_token_is_stale_however_recent(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """Kite kills a token at the next day's pre-open, so 23:55 last night is dead now."""
        await _calendar(session)
        store = _store(state_dir, key)
        store.save(TOKEN_VALUE, issued_at=_at("23:55", DAY - dt.timedelta(days=1)))

        report, sink = await _run(session, store, state_dir)

        assert len(sink.sent) == 1
        assert report.sent is True

    async def test_no_token_at_all_sends_one_message(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        store = _store(state_dir, key)

        report, sink = await _run(session, store, state_dir)

        assert len(sink.sent) == 1
        assert sink.sent[0].subject == SUBJECT
        assert report.sent is True

    async def test_a_simulated_token_is_not_a_session(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """`sim_` is what a box that cannot finish a login writes. It buys no silence."""
        await _calendar(session)
        store = _store(state_dir, key)
        store.save("sim_deadbeef", issued_at=_at("08:12"))

        _, sink = await _run(session, store, state_dir)

        assert len(sink.sent) == 1

    async def test_a_token_that_expires_mid_morning_is_seen_at_the_second_window(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """The 08:45 check is silent on a fresh token; a rotation of the encryption key that
        makes the blob unreadable by 09:05 is a stale token to this job, and it nudges."""
        await _calendar(session)
        store = _store(state_dir, key)
        store.save(TOKEN_VALUE, issued_at=_at("08:12"))
        _, first = await _run(session, store, state_dir)
        assert first.sent == []

        rotated = AccessTokenStore(store.path, Fernet.generate_key().decode())
        _, second = await _run(
            session, rotated, state_dir, window=NudgeWindow.SECOND, now=_at("09:05")
        )

        assert len(second.sent) == 1


class TestTheLinkIsOneKiteWouldAccept:
    async def test_the_url_is_kites_own_and_carries_the_api_key_and_state(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        _, sink = await _run(session, _store(state_dir, key), state_dir)

        body = sink.sent[0].text
        url = next(part for part in body.split() if part.startswith("http"))
        assert url.startswith(KITE_AUTHORIZE_URL)
        assert f"api_key={API_KEY}" in url
        assert "redirect_params=state%3D" in url

    async def test_the_state_in_the_link_is_one_the_callback_will_honour(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """The whole point of reusing `kite_login_url`: the callback's own state check passes.

        A second, private minter would produce a URL that looks identical and fails at the end
        of a login Maulik has already committed to.
        """
        await _calendar(session)
        _, sink = await _run(session, _store(state_dir, key), state_dir)

        url = next(part for part in sink.sent[0].text.split() if part.startswith("http"))
        state = url.split("state%3D")[1]
        pending = consume_oauth_state(state)

        assert pending is not None
        assert pending.user_id == 43
        assert pending.broker_id == "zerodha"

    async def test_each_window_mints_its_own_state(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """A state lives 30 minutes; the 09:05 link has to still work when tapped at 09:20."""
        await _calendar(session)
        store = _store(state_dir, key)
        _, first = await _run(session, store, state_dir)
        _, second = await _run(
            session, store, state_dir, window=NudgeWindow.SECOND, now=_at("09:05")
        )

        def state_of(sink: Sink) -> str:
            url = next(p for p in sink.sent[0].text.split() if p.startswith("http"))
            return url.split("state%3D")[1]

        assert state_of(first) != state_of(second)


class TestOneMessagePerMorning:
    async def test_a_second_call_in_the_first_window_sends_nothing(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        store = _store(state_dir, key)
        _, first = await _run(session, store, state_dir)
        report, again = await _run(session, store, state_dir)

        assert len(first.sent) == 1
        assert again.sent == []
        assert "already been used today" in " ".join(report.notes)

    async def test_the_second_window_still_sends_and_says_it_is_the_last(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        store = _store(state_dir, key)
        await _run(session, store, state_dir)
        report, second = await _run(
            session, store, state_dir, window=NudgeWindow.SECOND, now=_at("09:05")
        )

        assert len(second.sent) == 1
        assert "Second and last" in second.sent[0].text
        assert "no further reminder" in second.sent[0].text
        assert report.sent is True

    async def test_the_second_window_does_not_repeat_either(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        store = _store(state_dir, key)
        _, once = await _run(session, store, state_dir, window=NudgeWindow.SECOND, now=_at("09:05"))
        _, twice = await _run(
            session, store, state_dir, window=NudgeWindow.SECOND, now=_at("09:06")
        )

        assert len(once.sent) == 1
        assert twice.sent == []

    async def test_the_marker_is_claimed_before_the_send(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """A send that blew up must not leave the window open for a redelivery to re-send.

        The 09:05 window is the retry; a task that re-mails on every redelivery is how one
        reminder becomes twenty.
        """
        await _calendar(session)
        store = _store(state_dir, key)
        await _run(session, store, state_dir, transport=Exploding())

        assert marker_path(state_dir, DAY, NudgeWindow.FIRST).exists()
        _, again = await _run(session, store, state_dir)
        assert again.sent == []

    def test_two_workers_racing_the_same_tick_claim_it_once(self, state_dir: Path) -> None:
        """The reason the claim is ``O_EXCL`` and not "exists? then write".

        Two compute workers taking the same Beat tick is ordinary — a redelivery, a second
        replica — and check-then-act would send two identical emails on the morning the reader
        is least inclined to read either carefully. Threads rather than tasks, because the race
        is in the filesystem call and not in the event loop.
        """
        state_dir.mkdir(parents=True, exist_ok=True)
        barrier = threading.Barrier(8)

        def claim() -> bool:
            barrier.wait()
            return _claim(state_dir, DAY, NudgeWindow.FIRST)

        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = list(pool.map(lambda _: claim(), range(8)))

        assert sum(winners) == 1

    async def test_tomorrow_is_a_new_morning(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        await session.merge(
            TradingDay(
                exchange_id=NSE_EXCHANGE_ID,
                date=DAY + dt.timedelta(days=7),
                is_trading_day=True,
                source="bhavcopy",
            )
        )
        await session.flush()
        store = _store(state_dir, key)
        _, today = await _run(session, store, state_dir)
        _, next_week = await _run(
            session, store, state_dir, now=_at("08:45", DAY + dt.timedelta(days=7))
        )

        assert len(today.sent) == 1
        assert len(next_week.sent) == 1


class TestTheDaysThatAreNotMornings:
    async def test_an_nse_holiday_produces_no_nudge(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """Beat's `mon-fri` cannot know about Diwali; the calendar can."""
        await _calendar(session)
        _, sink = await _run(session, _store(state_dir, key), state_dir, now=_at("08:45", HOLIDAY))

        assert sink.sent == []
        assert not marker_path(state_dir, HOLIDAY, NudgeWindow.FIRST).exists()

    async def test_a_weekend_produces_no_nudge(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        report, sink = await _run(
            session, _store(state_dir, key), state_dir, now=_at("08:45", SATURDAY)
        )

        assert sink.sent == []
        assert "not a weekday" in " ".join(report.notes)


class TestItNeverGetsInTheWayOfTheMorning:
    async def test_a_mailer_that_raises_is_a_note_not_a_failure(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        boom = Exploding()
        report, _ = await _run(session, _store(state_dir, key), state_dir, transport=boom)

        assert boom.calls == 1
        assert report.sent is False
        assert any("could not be sent" in note for note in report.notes)

    async def test_no_recipient_configured_sends_nothing_and_raises_nothing(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        report, sink = await _run(session, _store(state_dir, key), state_dir, to="")

        assert sink.sent == []
        assert report.sent is False
        assert any("BASKFY_KITE_LOGIN_NUDGE_TO" in note for note in report.notes)
        # The window is untouched: a box that gets configured at 09:00 still gets its 09:05 mail.
        assert not marker_path(state_dir, DAY, NudgeWindow.FIRST).exists()

    async def test_a_url_builder_that_raises_is_a_note(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)

        def broken(window: NudgeWindow) -> KiteLoginUrl:
            raise RuntimeError("no api key on this box")

        sink = Recording()
        report = await run_login_nudge(
            session,
            StepOutcome(),
            now=_at("08:45"),
            token_store=_store(state_dir, key),
            mailer=Mailer(sink),
            login_url_for=broken,
            to="maulik@example.com",
            state_dir=state_dir,
        )

        assert sink.sent == []
        assert report.sent is False
        assert any("could not be built" in note for note in report.notes)

    async def test_the_step_outcome_carries_what_happened(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        outcome = StepOutcome()
        await run_login_nudge(
            session,
            outcome,
            now=_at("08:45"),
            token_store=_store(state_dir, key),
            mailer=Mailer(Recording()),
            login_url_for=_link,
            to="maulik@example.com",
            state_dir=state_dir,
        )

        detail = cast(JsonObject, outcome.detail["login_nudge"])
        assert detail["sent"] is True
        assert detail["window"] == "first"


class TestNothingSecretIsEverRendered:
    async def test_the_body_carries_no_secret_no_token_and_no_encryption_key(
        self, session: AsyncSession, state_dir: Path, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The api_key in the URL is a public app identifier. Nothing else may appear."""
        monkeypatch.setenv("BASKFY_KITE_API_SECRET", API_SECRET)
        await _calendar(session)
        store = _store(state_dir, key)
        store.save(TOKEN_VALUE, issued_at=_at("23:55", DAY - dt.timedelta(days=1)))

        _, sink = await _run(session, store, state_dir)

        rendered = sink.sent[0].text + sink.sent[0].html + sink.sent[0].subject
        assert API_SECRET not in rendered
        assert TOKEN_VALUE not in rendered
        assert key not in rendered
        assert str(store.path) not in rendered

    async def test_a_note_never_carries_the_recipient_or_the_blob_path(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        store = _store(state_dir, key)
        report, _ = await _run(session, store, state_dir, transport=Exploding())

        joined = " ".join(report.notes)
        assert "maulik@example.com" not in joined
        assert "example.com" not in joined
        assert str(store.path) not in joined


class TestTheMessageReadsRightAtQuarterToNine:
    async def test_it_says_whose_box_sent_it_and_what_happens_if_ignored(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        await _calendar(session)
        _, sink = await _run(session, _store(state_dir, key), state_dir)

        body = sink.sent[0].text
        assert "your own box" in body
        assert "no Zerodha password and no 2FA seed" in body
        assert "Zerodha's own login page" in body
        assert "idles" in body and "no order is possible today" in body
        assert "Zerodha — Baskfy's Kite Connect app" in body

    async def test_the_subject_is_the_same_for_both_windows(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """One thread on a phone, not two."""
        await _calendar(session)
        store = _store(state_dir, key)
        _, first = await _run(session, store, state_dir)
        _, second = await _run(
            session, store, state_dir, window=NudgeWindow.SECOND, now=_at("09:05")
        )

        assert first.sent[0].subject == second.sent[0].subject == SUBJECT

    async def test_the_html_has_exactly_one_link_and_it_is_the_login(
        self, session: AsyncSession, state_dir: Path, key: str
    ) -> None:
        """One tap. A second link is a second decision to make before 09:15."""
        await _calendar(session)
        _, sink = await _run(session, _store(state_dir, key), state_dir)

        html = sink.sent[0].html
        assert html.count("<a href=") == 1
        assert KITE_AUTHORIZE_URL in html


class TestTheFlagAndTheSchedule:
    def test_the_task_does_nothing_with_the_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker_settings.get_worker_settings.cache_clear()
        monkeypatch.setenv("BASKFY_KITE_LOGIN_NUDGE_ENABLED", "false")
        try:
            result = celery_tasks.kite_login_nudge_task()
        finally:
            worker_settings.get_worker_settings.cache_clear()

        assert result == {"skipped": "BASKFY_KITE_LOGIN_NUDGE_ENABLED is false"}

    def test_the_flag_defaults_off_and_the_recipient_defaults_empty(self) -> None:
        defaults = WorkerSettings(_env_file=None)

        assert defaults.kite_login_nudge_enabled is False
        assert defaults.kite_login_nudge_to == ""

    def test_a_state_the_api_could_not_read_back_is_refused_rather_than_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker and the API are two containers. Without the shared state file the link
        starts a real Kite login and dies at the callback — a wasted tap at 09:05."""
        worker_settings.get_worker_settings.cache_clear()
        monkeypatch.setenv("BASKFY_KITE_LOGIN_NUDGE_ENABLED", "true")
        monkeypatch.setenv("BASKFY_SOLE_USER_ID", "43")
        monkeypatch.delenv("BASKFY_BROKER_OAUTH_STATE_PATH", raising=False)
        try:
            result = celery_tasks.kite_login_nudge_task()
        finally:
            worker_settings.get_worker_settings.cache_clear()

        assert "BASKFY_BROKER_OAUTH_STATE_PATH" in str(result["skipped"])

    @pytest.mark.parametrize(
        ("entry", "hour", "minute", "window"),
        [
            ("kite-login-nudge", 8, 45, "first"),
            ("kite-login-nudge-second", 9, 5, "second"),
        ],
    )
    def test_beat_runs_it_twice_on_weekdays_on_the_compute_queue(
        self, entry: str, hour: int, minute: int, window: str
    ) -> None:
        row = BEAT_SCHEDULE[entry]
        schedule = cast(crontab, row["schedule"])

        assert row["task"] == "baskfy.kite.login_nudge"
        assert (schedule.hour, schedule.minute) == ({hour}, {minute})
        assert schedule.day_of_week == {1, 2, 3, 4, 5}
        assert row["kwargs"] == {"window": window}
        assert row["options"] == {"queue": QUEUE_COMPUTE}
        assert TASK_ROUTES["baskfy.kite.*"] == {"queue": QUEUE_COMPUTE}
        # 09:14 is when `swing_monitor` builds its Kite client and reads the blob. Both
        # windows have to be in front of it or the nudge is a reminder about yesterday.
        assert (hour, minute) < (9, 14)


class TestTheBuilderIsShared:
    def test_the_router_and_the_worker_use_one_login_url_builder(self) -> None:
        """A private second builder is how `state` got dropped from the URL once already."""
        source = inspect.getsource(brokers_router)

        assert "kite_login_url(" in source
        # The two calls that would mean a second, private builder had grown back. Comments
        # may still discuss `redirect_params`; a call to `urlencode` is the tell.
        assert "register_oauth_state(" not in source
        assert "urlencode(" not in source

    def test_the_worker_never_builds_a_url_of_its_own(self) -> None:
        source = inspect.getsource(kite_login_nudge)

        assert "urlencode" not in source
        assert "kite.zerodha.com" not in source
