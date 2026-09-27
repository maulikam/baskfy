"""TW17 — TWT auto-execute (Maulik, in session, 21 Sep 2026: "TWT has no auto-execute flag - this
should be implemented auto execute").

These tests pin that decision, not a document: the flag exists in the desk only, defaults false,
needs all three flags, and when on it removes the click and **nothing else** — every line goes
through the same ``execute_line`` the button reaches, once, and every refusal still refuses.

No test here ever holds a live gateway. Where the real ``execute_line`` runs, the sleeve's gates
stay dry (``DRY_RUN`` true, the TWT flag false) and the runner's own switch is forced on by
monkeypatching :func:`app.twt_auto.auto_execute_enabled` — so the path is the real one and the
broker is ``ExplodingKC``. Where the three flags are all set live, ``execute_line`` is a spy that
records and places nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app import config as C
from app import twt_auto as A
from app import twt_execute as X
from app.twt_desk import KIND_ORDER
from scripts import twt_auto_loop as L
from tests.test_twt_execute import (
    FIFTY_CRORE,
    SESSION,
    TWENTY_FIVE_LAKH,
    MemoryStore,
    SpyGateway,
    gateway,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
#: Monday 21 Sep 2026, ten seconds past the open — when the loop runs it.
OPEN = dt.datetime(2026, 9, 21, 9, 15, 10, tzinfo=IST)
BUILT = dt.datetime(2026, 9, 21, 9, 5, tzinfo=IST)
D = Decimal

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "decile-blueprint" / "infra" / "docker" / "Dockerfile.desk"
COMPOSE = REPO / "decile-blueprint" / "infra" / "docker" / "compose.prod.yml"


class _Calendar:
    """A connection that answers only the runner's one calendar read."""

    def __init__(self, closed: set[dt.date]) -> None:
        self.closed = closed

    def execute(self, _sql: str, params: tuple) -> _Calendar:
        self._day = params[0]
        return self

    def fetchone(self) -> dict:
        return {"is_trading_day": self._day not in self.closed}


class AutoStore(MemoryStore):
    """``MemoryStore`` plus the two page reads the runner uses: ``todays_plan``, ``lines_for``."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.conn = _Calendar(set())

    def t(self, table: str) -> str:
        return table

    def todays_plan(self, on_or_before: dt.date) -> dict | None:
        plans = [p for p in self.plans.values() if p["session_date"] <= on_or_before]
        plans.sort(key=lambda p: (p["session_date"], p["built_at"]), reverse=True)
        return plans[0] if plans else None

    def lines_for(self, plan_pk: int) -> list[dict]:
        rows = [line for line in self.lines.values() if line["plan_pk"] == plan_pk]
        return sorted(rows, key=lambda line: (KIND_ORDER.index(line["kind"]), line["symbol"]))


def a_plan(
    store: AutoStore,
    *,
    source: str = "MORNING",
    built_at: dt.datetime = BUILT,
    ttl_minutes: int = 30,
) -> tuple[str, int]:
    plan_id = str(uuid.uuid4())
    pk = len(store.plans) + 1
    store.plans[plan_id] = {
        "id": pk,
        "plan_id": plan_id,
        "session_date": SESSION,
        "source": source,
        "built_at": built_at,
        "expires_at": built_at + dt.timedelta(minutes=ttl_minutes),
        "gate": "OPEN",
    }
    return plan_id, pk


def a_line(  # noqa: PLR0913 - a plan line is its columns
    store: AutoStore,
    plan_id: str,
    pk: int,
    *,
    line_id: int,
    symbol: str,
    kind: str = "BUY_AT_OPEN",
    state: str = "PROPOSED",
    instrument_id: int | None = None,
) -> int:
    suffix = "" if kind == "BUY_AT_OPEN" else f":{kind}"
    store.lines[line_id] = {
        "id": line_id,
        "plan_pk": pk,
        "plan_id": plan_id,
        "kind": kind,
        "instrument_id": instrument_id if instrument_id is not None else 1000 + line_id,
        "symbol": symbol,
        "quantity": 2_500,
        "stop_price": D("80.00"),
        "value_inr": D("250000.00"),
        "high_since": None,
        "previous_trigger": None,
        "note": "",
        "state": state,
        "client_id": f"{plan_id}:{symbol}{suffix}",
        "order_id": None,
        "position_id": None,
        "session_date": SESSION,
        "turnover_avg_inr": FIFTY_CRORE,
    }
    return line_id


class SpyExecute:
    """Stands in for ``execute_line`` when the three flags are live. Records; sends nothing."""

    def __init__(self, store: AutoStore, *, raise_on: str | None = None) -> None:
        self.store = store
        self.calls: list[dict] = []
        self.raise_on = raise_on

    async def __call__(self, store: object, gateway: object, **kwargs: object) -> object:
        self.calls.append({"gateway": gateway, **kwargs})
        line = self.store.lines[int(str(kwargs["line_id"]))]
        if line["symbol"] == self.raise_on:
            raise RuntimeError("the broker fell over")
        line["state"] = "SENT"

        class _Outcome:
            status = "SENT"
            reason = ""

        return _Outcome()


class _Refuse:
    """A collaborator that must never be reached."""

    def __init__(self, what: str) -> None:
        self.what = what

    def __call__(self, *_: object, **__: object) -> object:
        raise AssertionError(f"{self.what} was reached with auto-execute off")


def _flags(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool, execution: bool, auto: bool) -> None:
    monkeypatch.setattr(C, "DRY_RUN", dry_run)
    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", execution)
    monkeypatch.setattr(C, "TWT_AUTO_EXECUTE", auto)


@pytest.fixture(autouse=True)
def _dry_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts dry with the flag off. A test that needs a flag on says so."""
    _flags(monkeypatch, dry_run=True, execution=False, auto=False)


def _run(store: AutoStore, *, at: dt.datetime = OPEN, **kwargs: object) -> A.RunReport:
    @contextlib.contextmanager
    def _open():  # noqa: ANN202 - a context manager
        yield store

    defaults: dict[str, object] = {
        "now": lambda: at,
        "open_store": _open,
        "gateway": lambda: "the-gateway",
        "price_for": lambda _symbol: D("100.00"),
        "authed": lambda: True,
        "sleep": lambda _s: None,
    }
    defaults.update(kwargs)
    return A.run_once(**defaults)


def _morning(store: AutoStore) -> tuple[str, int]:
    plan_id, pk = a_plan(store)
    a_line(store, plan_id, pk, line_id=1, symbol="BBB")
    a_line(store, plan_id, pk, line_id=2, symbol="AAA")
    a_line(store, plan_id, pk, line_id=3, symbol="ZZZ", kind="RAISE_GTT_STOP")
    a_line(store, plan_id, pk, line_id=4, symbol="YYY", kind="ARM_GTT")
    a_line(store, plan_id, pk, line_id=5, symbol="CCC", state="REJECTED")
    return plan_id, pk


# =========================================================================================
# The flag: the desk's, default false, and all three or nothing
# =========================================================================================
class TestTheFlag:
    def test_it_defaults_false_and_is_read_from_its_own_variable(self) -> None:
        source = Path(C.__file__).read_text(encoding="utf-8")
        assert 'os.getenv("BASKFY_TWT_AUTO_EXECUTE", "false")' in source
        assert C.TWT_AUTO_EXECUTE is False

    def test_it_needs_all_three_flags_and_any_one_off_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The truth table, exhaustively. Only one of eight cells auto-executes."""
        live = []
        for dry_run in (True, False):
            for execution in (False, True):
                for auto in (False, True):
                    _flags(monkeypatch, dry_run=dry_run, execution=execution, auto=auto)
                    if A.auto_execute_enabled():
                        live.append((dry_run, execution, auto))
        assert live == [(False, True, True)], f"more than one live cell: {live}"

    def test_the_kinds_it_sends_are_exactly_the_kinds_a_click_may_send(self) -> None:
        """Since LV9 (Maulik, 28 Sep 2026 — DECISIONS-TW TW20) that includes ``SELL_AT_OPEN``:
        Qullamaggie's partial and MA-trail exit are drained like the stops and the buys."""
        assert A.AUTO_KINDS == X.EXECUTABLE_KINDS
        assert "SELL_AT_OPEN" in A.AUTO_KINDS


# =========================================================================================
# Off means off
# =========================================================================================
class TestOffMeansNothing:
    @pytest.mark.parametrize(
        ("dry_run", "execution", "auto"),
        [
            (True, False, False),
            (True, True, True),  # DRY_RUN alone off-switches it
            (False, False, True),  # the sleeve not live
            (False, True, False),  # live, but not unattended
            (True, False, True),
            (True, True, False),
            (False, False, False),
        ],
    )
    def test_any_flag_off_executes_nothing_and_opens_nothing(
        self, monkeypatch: pytest.MonkeyPatch, dry_run: bool, execution: bool, auto: bool
    ) -> None:
        _flags(monkeypatch, dry_run=dry_run, execution=execution, auto=auto)
        store = AutoStore()
        _morning(store)
        report = A.run_once(
            now=lambda: OPEN,
            open_store=_Refuse("the store"),
            gateway=_Refuse("the gateway"),
            price_for=_Refuse("a price read"),
            authed=_Refuse("the Kite session"),
            execute=_Refuse("execute_line"),
        )
        assert report.ran is False
        assert "off" in report.reason
        assert all(line["state"] in {"PROPOSED", "REJECTED"} for line in store.lines.values())

    def test_the_drain_itself_refuses_with_the_flag_off(self) -> None:
        """The check is inside the drain too, so no caller can reach the order path around it."""
        store = AutoStore()
        plan_id, _pk = _morning(store)
        done = asyncio.run(
            A.drain_plan(
                store,
                "gw",
                store.plans[plan_id],
                now=lambda: OPEN,
                price_for=lambda _s: D("100"),
                execute=_Refuse("execute_line"),
            )
        )
        assert done == []

    def test_main_exits_zero_without_reading_a_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(A, "run_once", _Refuse("run_once"))
        assert A.main() == 0


# =========================================================================================
# On: every PROPOSED line through execute_line, once, in the plan's own order
# =========================================================================================
class TestOnSendsEachProposedLineOnce:
    def test_each_proposed_line_goes_through_execute_line_once_stops_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, dry_run=False, execution=True, auto=True)
        store = AutoStore()
        plan_id, _pk = _morning(store)
        spy = SpyExecute(store)
        report = _run(store, execute=spy)

        assert report.ran is True
        assert report.plan_id == plan_id
        assert [c["line_id"] for c in spy.calls] == [4, 3, 2, 1], "ARM, RAISE, then buys A-Z"
        assert all(c["confirm"] == "true" for c in spy.calls)
        assert all(c["plan_id"] == plan_id for c in spy.calls)
        assert all(c["gateway"] == "the-gateway" for c in spy.calls)
        assert all(c["last_price"] == D("100.00") for c in spy.calls)
        assert 5 not in [c["line_id"] for c in spy.calls], "a REJECTED line was re-sent"
        assert "4 line(s)" in report.line()

    def test_one_line_that_raises_does_not_cost_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch, dry_run=False, execution=True, auto=True)
        store = AutoStore()
        _morning(store)
        spy = SpyExecute(store, raise_on="AAA")
        report = _run(store, execute=spy)
        statuses = {symbol: status for symbol, _kind, status in report.attempts}
        assert statuses == {"YYY": "SENT", "ZZZ": "SENT", "AAA": "ERROR", "BBB": "SENT"}
        assert store.lines[2]["state"] == "PROPOSED", "the failed line stays confirmable by hand"

    def test_a_second_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, dry_run=False, execution=True, auto=True)
        store = AutoStore()
        _morning(store)
        spy = SpyExecute(store)
        _run(store, execute=spy)
        _run(store, execute=spy)
        assert len(spy.calls) == 4


# =========================================================================================
# No plan, no order — and never a plan of its own
# =========================================================================================
class TestThePlanItActsOn:
    @pytest.fixture(autouse=True)
    def _live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, dry_run=False, execution=True, auto=True)

    def _refused(self, store: AutoStore, **kwargs: object) -> A.RunReport:
        report = _run(store, execute=_Refuse("execute_line"), **kwargs)
        assert report.ran is False
        assert report.attempts == []
        return report

    def test_no_plan_sends_nothing(self) -> None:
        assert self._refused(AutoStore()).reason.startswith("NO_PLAN")

    def test_an_expired_plan_sends_nothing(self) -> None:
        store = AutoStore()
        plan_id, pk = a_plan(store, ttl_minutes=5)  # expired 09:10
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        assert self._refused(store).reason.startswith("EXPIRED")
        assert store.lines[1]["state"] == "PROPOSED"

    def test_a_live_plan_built_today_is_used(self) -> None:
        """LV8 (TW19): the Scan button's LIVE plan is drained like the MORNING one."""
        store = AutoStore()
        plan_id, pk = a_plan(store, source="LIVE", built_at=OPEN + dt.timedelta(hours=2))
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        spy = SpyExecute(store)
        report = _run(store, at=OPEN + dt.timedelta(hours=2, minutes=5), execute=spy)
        assert report.ran is True and report.plan_id == plan_id
        assert [c["line_id"] for c in spy.calls] == [1]

    def test_an_expired_live_plan_sends_nothing(self) -> None:
        store = AutoStore()
        plan_id, pk = a_plan(store, source="LIVE", built_at=OPEN + dt.timedelta(hours=2))
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        report = self._refused(store, at=OPEN + dt.timedelta(hours=2, minutes=31))
        assert report.reason.startswith("EXPIRED")

    def test_a_live_plan_is_drained_in_the_afternoon_too(self) -> None:
        """SW26's widening, inherited: the live scan may run at 14:00 and its plan is good then."""
        store = AutoStore()
        built = OPEN.replace(hour=14, minute=0)
        plan_id, pk = a_plan(store, source="LIVE", built_at=built)
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        spy = SpyExecute(store)
        report = _run(store, at=built + dt.timedelta(minutes=1), execute=spy)
        assert report.ran is True and len(spy.calls) == 1

    def test_drain_now_is_run_once_without_a_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The supervisor's entry point: no sleep, no wait, the same refusals."""
        store = AutoStore()
        plan_id, pk = a_plan(store, source="LIVE", built_at=OPEN + dt.timedelta(hours=1))
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        spy = SpyExecute(store)
        seen: dict = {}

        def fake_run_once(**kwargs: object) -> A.RunReport:
            seen.update(kwargs)
            return A.RunReport()

        monkeypatch.setattr(A, "run_once", fake_run_once)
        A.drain_now(OPEN + dt.timedelta(hours=1, minutes=1))
        assert seen["wait_seconds"] == 0 and seen["poll_seconds"] == 0
        assert seen["now"]() == OPEN + dt.timedelta(hours=1, minutes=1)
        del spy

    def test_drain_now_with_the_flag_off_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same three flags as the click and the 09:15 loop; the supervisor adds none."""
        _flags(monkeypatch, dry_run=True, execution=False, auto=False)
        report = A.drain_now(OPEN + dt.timedelta(hours=1))
        assert report.ran is False and "auto-execute is off" in report.reason

    def test_an_evening_plan_is_never_used(self) -> None:
        store = AutoStore()
        plan_id, pk = a_plan(store, source="EVENING", ttl_minutes=24 * 60)
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        assert self._refused(store).reason.startswith("NOT_MORNING")

    def test_yesterdays_morning_plan_is_never_used(self) -> None:
        store = AutoStore()
        plan_id, pk = a_plan(store, built_at=BUILT - dt.timedelta(days=1), ttl_minutes=48 * 60)
        a_line(store, plan_id, pk, line_id=1, symbol="AAA")
        assert self._refused(store).reason.startswith("STALE_PLAN")

    def test_nothing_before_the_open_or_on_a_weekend(self) -> None:
        store = AutoStore()
        _morning(store)
        self._refused(store, at=dt.datetime(2026, 9, 21, 9, 14, 59, tzinfo=IST))
        self._refused(store, at=dt.datetime(2026, 9, 19, 9, 20, tzinfo=IST))  # Saturday

    def test_an_exchange_holiday_sends_nothing(self) -> None:
        store = AutoStore()
        _morning(store)
        store.conn = _Calendar({OPEN.date()})
        assert "not a trading session" in self._refused(store).reason

    def test_it_waits_for_a_kite_session_then_gives_up_without_sending(self) -> None:
        store = AutoStore()
        _morning(store)
        slept: list[float] = []
        report = self._refused(
            store, authed=lambda: False, sleep=slept.append, wait_seconds=90, poll_seconds=30
        )
        assert report.reason.startswith("no Kite session")
        assert slept == [30, 30, 30]

    def test_a_session_that_arrives_late_is_used(self) -> None:
        store = AutoStore()
        _morning(store)
        answers = iter([False, False, True])
        spy = SpyExecute(store)
        report = _run(store, authed=lambda: next(answers), execute=spy)
        assert report.ran is True
        assert len(spy.calls) == 4


# =========================================================================================
# The real execute_line: every refusal still refuses, and nothing double-sends
# =========================================================================================
class TestTheRealPathStillRefuses:
    """The real ``execute_line`` and the real dry-run gateway over ``ExplodingKC``. The runner's
    switch is forced on; the sleeve's gates stay dry, so the spy asserts every answer is a dry run.
    """

    @pytest.fixture(autouse=True)
    def _runner_on_gates_dry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(A, "auto_execute_enabled", lambda: True)
        assert X.twt_gates().dry_run is True

    def _four_buys(self) -> tuple[AutoStore, str]:
        store = AutoStore(capital=TWENTY_FIVE_LAKH)
        plan_id, pk = a_plan(store)
        for index, symbol in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1):
            a_line(store, plan_id, pk, line_id=index, symbol=symbol)
        return store, plan_id

    def test_the_per_session_cap_still_refuses_a_fourth_entry(self) -> None:
        store, _plan_id = self._four_buys()
        tape_before = len(SpyGateway.tape)
        report = _run(store, gateway=gateway)
        statuses = [status for _symbol, _kind, status in report.attempts]
        assert statuses[:3] == ["SIMULATED"] * 3, report.attempts
        assert statuses[3] == "BLOCKED"
        assert store.lines[4]["note"].startswith("SESSION_CAP")
        assert store.entries_taken(SESSION) == 3
        assert len(SpyGateway.tape) > tape_before, "the real gateway was not exercised"

    def test_re_running_does_not_double_send(self) -> None:
        store, _plan_id = self._four_buys()
        _run(store, gateway=gateway)
        orders, fills = len(store.orders), len(store.fills)
        tape = len(SpyGateway.tape)
        again = _run(store, gateway=gateway)
        assert again.attempts == []
        assert (len(store.orders), len(store.fills), len(SpyGateway.tape)) == (orders, fills, tape)

    def test_every_buy_gets_its_stop_in_the_same_request(self) -> None:
        store, _plan_id = self._four_buys()
        _run(store, gateway=gateway)
        positions = store.open_positions()
        assert len(positions) == 3
        assert all(p["gtt_id"] is not None for p in positions), "a buy was left without a stop"

    def test_a_halted_sleeve_sends_nothing(self) -> None:
        store, _plan_id = self._four_buys()
        X.halt_sleeve(store, confirm="true", now=OPEN - dt.timedelta(seconds=5))
        report = _run(store, gateway=gateway)
        assert report.ran is False
        assert report.reason.startswith("EXPIRED")
        assert store.orders == {}


# =========================================================================================
# The runner never builds a plan and never reaches the broker except through execute_line
# =========================================================================================
class TestTheRunnersShape:
    def test_it_names_no_order_call_of_its_own(self) -> None:
        source = Path(A.__file__).read_text(encoding="utf-8")
        for forbidden in ("place_order", "place_gtt", ".place(", "kc.", "run_twt_evening",
                          "store_plan", "INSERT", "UPDATE "):
            assert forbidden not in source, f"app/twt_auto.py names {forbidden}"
        assert "twt_execute.execute_line" in source


# =========================================================================================
# The clock: scripts/twt_auto_loop.py, and the wiring that runs it
# =========================================================================================
class TestTheLoop:
    def test_before_the_open_it_waits_for_nine_fifteen_ten(self) -> None:
        early = dt.datetime(2026, 9, 21, 8, 0, tzinfo=IST)
        assert L.next_run(early) == dt.datetime(2026, 9, 21, 9, 15, 10, tzinfo=IST)

    def test_inside_the_window_it_runs_now(self) -> None:
        late = dt.datetime(2026, 9, 21, 9, 20, tzinfo=IST)
        assert L.next_run(late) == late

    def test_after_the_window_it_waits_for_the_next_weekday(self) -> None:
        friday = dt.datetime(2026, 9, 18, 10, 0, tzinfo=IST)
        assert L.next_run(friday) == dt.datetime(2026, 9, 21, 9, 15, 10, tzinfo=IST)

    def test_once_it_has_run_today_it_waits_for_tomorrow(self) -> None:
        late = dt.datetime(2026, 9, 21, 9, 20, tzinfo=IST)
        assert L.next_run(late, ran_today=True) == dt.datetime(2026, 9, 22, 9, 15, 10, tzinfo=IST)

    def test_a_weekend_waits_for_monday(self) -> None:
        saturday = dt.datetime(2026, 9, 19, 9, 20, tzinfo=IST)
        assert L.next_run(saturday) == dt.datetime(2026, 9, 21, 9, 15, 10, tzinfo=IST)

    def test_run_forever_calls_the_module_once_per_day(self) -> None:
        clock = iter(
            [
                dt.datetime(2026, 9, 21, 9, 20, tzinfo=IST),
                dt.datetime(2026, 9, 21, 9, 20, tzinfo=IST),
                dt.datetime(2026, 9, 21, 9, 21, tzinfo=IST),
            ]
        )
        calls: list[list[str]] = []
        L.run_forever(
            now_fn=lambda: next(clock),
            sleep_fn=lambda _s: None,
            call_fn=lambda argv: calls.append(argv) or 0,
            limit=1,
        )
        assert len(calls) == 1
        assert calls[0][1:] == ["-m", "app.twt_auto"]

    def test_the_image_runs_this_module(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "RUN cat > /usr/local/bin/twt-auto-loop" in text
        assert "exec python -m scripts.twt_auto_loop" in text
        assert "/usr/local/bin/twt-auto-loop" in text.split("RUN chmod 0755", 1)[1].split("\n")[0]

    def test_compose_runs_it_from_the_desk_block_with_the_flag_named_false(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        assert "  twt-auto:\n    <<: *desk\n    command: [twt-auto-loop]" in text
        compose = yaml.safe_load(text)
        env = compose["services"]["twt-auto"]["environment"]
        assert env["BASKFY_TWT_AUTO_EXECUTE"] == "${BASKFY_TWT_AUTO_EXECUTE:-false}"
        assert env["DRY_RUN"] == "${BASKFY_DESK_DRY_RUN:-true}"
        # The desk's own flag only: the api/worker/beat block does not name it.
        common = compose["x-python-image"].get("environment", {})
        assert "BASKFY_TWT_AUTO_EXECUTE" not in common
