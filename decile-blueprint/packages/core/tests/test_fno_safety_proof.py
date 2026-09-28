"""FO11 — the pure half of the FO safety proof and the source scans (``docs/fno/06`` FO11,
``02`` Tracks B and C).

Properties, by Hypothesis over fill scripts and simulated clocks:

* **never naked, by the FO rule** (Track C §1, ``02`` §2.1): whatever each attempt of an F1 condor
  entry or exit fills — nothing, part, all, or a refusal at that step — the book after every fill
  is covered by ``baskfy_core.fno.covered.uncovered`` (the predicate the gateway is wired with,
  FO6.1), and every order the executor sends would itself pass the gateway's covered-overnight
  guard on the book it would leave (so the sequencing alone is safe; the guard is the second
  wall);
* **an exit closes exactly the open legs**: only a leg with units open is closed, never more than
  is open, and a ``FLAT`` exit leaves every leg at zero;
* **no stock derivative past E-1 15:00, no index position into expiry day** (Track C §2): over any
  exchange calendar (holidays drawn at random) and any expiry, ``E - n`` is strictly before the
  expiry for every admissible ``n``; at any clock at or after 15:00 on it (or any later day)
  ``exits.f1_exit`` returns an exit whatever the marks, and ``monitor.f2_action`` returns an
  action (roll, stop, time exit, ``LATE_EXIT`` or ``NAKED_FUTURE``) whatever the price.

Source scans (docstrings and comments stripped, so a sentence about a thing is not the thing):

* no name matching ``BASKFY_FNO_*AUTO*`` is read anywhere: the desk, the services, the packages,
  the tools, the compose files and every ``.env`` example (Track B);
* ``apps/web`` and ``services/api`` contain no route or fetch that reaches the FO execute path or a
  gateway (Track C §4; FO5's uncommitted files included — they are on disk);
* nothing outside ``packages/execution`` (and the desk's broker adapter the gateway calls) calls
  ``place_order`` (law 2);
* ``frozen/`` is unchanged against ``HEAD`` (Track C §10).

The desk's half — the flag matrix with a spy broker per sleeve, ``LIVE_NOT_BUILT``, the
``INTRADAY_ENABLED`` refusal, and the Hypothesis fuzz over ``POST /fno/execute`` — is
``kite-momentum-rebalancer/tests/test_fno_safety.py``; the paper-day drill is
``tools/fno/drill.py``.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import re
import subprocess
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from baskfy_core.fno.calendar import hard_exit_session, monthly_expiries, roll_session
from baskfy_core.fno.condor import ENTRY_SEQUENCE, CondorStrikes
from baskfy_core.fno.condor import LegRole as CondorRole
from baskfy_core.fno.config import CommonConfig, F1Config, F2Config
from baskfy_core.fno.covered import OptionPosition, uncovered
from baskfy_core.fno.exits import ExitReason, f1_exit
from baskfy_core.fno.monitor import f2_action
from baskfy_core.options.config import ExecutionConfig, OptionType, Sleeve
from baskfy_core.options.execution import Attempt, LegRole
from baskfy_core.options.executor import Book, Fill, Outcome, run_entry, run_exit

ROOT = Path(__file__).resolve().parents[4]
SCREENER = ROOT / "decile-blueprint"
DESK = ROOT / "kite-momentum-rebalancer"
CFG = ExecutionConfig()
TICK = Decimal("0.05")
#: FO7.2: the desk sends F1 through the options executor with O1's rules (the only
#: sleeve-dependent rule is which close reduces risk, and for a condor that is O1's reading).
CONDOR_RULES = Sleeve.O1M
EXPIRY = dt.date(2026, 11, 24)
KIND = {
    LegRole.LONG_CALL: OptionType.CE,
    LegRole.SHORT_CALL: OptionType.CE,
    LegRole.SHORT_PUT: OptionType.PE,
    LegRole.LONG_PUT: OptionType.PE,
}


# --- never naked, by the FO predicate, and exits close exactly the open legs ---------------------


def _signed(role: LegRole, units: int) -> int:
    return units if role.is_long else -units


@dataclass
class ScriptedFoVenue:
    """Each send takes the next scripted outcome (cycling): a fill share in percent, or ``-1`` for
    the gateway refusing the order at that step (nothing fills). Before every send it checks the
    order against the FO covered predicate as the gateway would; after every fill it checks the
    book; on a close it checks the exit's discipline."""

    underlying: str
    strikes: CondorStrikes
    script: list[int]
    book: dict[LegRole, int] = field(default_factory=dict)
    sends: int = 0
    refusals: int = 0

    def _strike(self, role: LegRole) -> Decimal:
        return self.strikes.strike(CondorRole(role.value))

    def _position(self, extra: tuple[LegRole, int] | None = None) -> list[OptionPosition]:
        rows = [
            OptionPosition(self.underlying, EXPIRY, KIND[r], self._strike(r), _signed(r, q))
            for r, q in self.book.items()
            if q
        ]
        if extra is not None:
            role, signed = extra
            rows.append(
                OptionPosition(self.underlying, EXPIRY, KIND[role], self._strike(role),
                               signed)
            )  # fmt: skip
        return rows

    def quote(self, role: LegRole) -> Book:
        del role
        return Book(Decimal("10.00"), Decimal("10.20"))

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        # The order, fully filled, on the book it would leave: what the gateway re-proves (04 §2).
        signed = quantity if attempt.side.value == "BUY" else -quantity
        assert uncovered(self._position((role, signed))) == (), (role, closing, self.book)
        outcome = self.script[self.sends % len(self.script)] if self.script else 100
        self.sends += 1
        if outcome < 0:
            self.refusals += 1
            return Fill(0, None)
        filled = quantity * outcome // 100
        if closing:
            open_units = self.book.get(role, 0)
            assert open_units > 0, f"a close was sent for {role} with nothing open"
            assert quantity <= open_units, f"closing {quantity} of {open_units} {role}"
            self.book[role] = open_units - filled
        else:
            self.book[role] = self.book.get(role, 0) + filled
        assert uncovered(self._position()) == (), (role, self.book)
        return Fill(filled, attempt.price if filled else None)


@st.composite
def condors(draw: st.DrawFn) -> tuple[str, CondorStrikes, int]:
    underlying, step, lot = draw(st.sampled_from([("NIFTY", 50, 65), ("BANKNIFTY", 100, 30)]))
    spot = draw(st.integers(400, 600)) * step
    sc = spot + draw(st.integers(1, 40)) * step
    sp = spot - draw(st.integers(1, 40)) * step
    lc = sc + draw(st.integers(1, 20)) * step
    lp = sp - draw(st.integers(1, 20)) * step
    lots = draw(st.integers(1, 4))
    return underlying, CondorStrikes(Decimal(lc), Decimal(sc), Decimal(sp), Decimal(lp)), lots * lot


OUTCOMES = st.sampled_from([-1, 0, 0, 25, 50, 99, 100, 100, 100])


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(condor=condors(), script=st.lists(OUTCOMES, min_size=1, max_size=24))
def test_f1_is_never_naked_at_any_instant_and_the_exit_closes_exactly_the_open_legs(
    condor: tuple[str, CondorStrikes, int], script: list[int]
) -> None:
    underlying, strikes, quantity = condor
    venue = ScriptedFoVenue(underlying, strikes, script)
    entry = run_entry(venue, ENTRY_SEQUENCE_ROLES, quantity, sleeve=CONDOR_RULES, tick=TICK,
                      config=CFG)  # fmt: skip
    assert uncovered(venue._position()) == ()
    if entry.outcome is not Outcome.OPEN:
        # Abandoned: what filled was closed in exit order, each close checked by the venue. A close
        # that did not fill (or was refused) leaves its leg open, and the desk alerts "close it by
        # hand"; what is left is still covered (asserted above) — never a short alone.
        return
    assert all(venue.book.get(r, 0) == quantity for r in ENTRY_SEQUENCE_ROLES)
    held = {role: units for role, units in venue.book.items() if units}
    closed = run_exit(venue, dict(held), sleeve=CONDOR_RULES, tick=TICK, config=CFG)
    for role in venue.book:
        assert 0 <= venue.book[role] <= held.get(role, 0)
    if closed.outcome is Outcome.FLAT:
        assert all(units == 0 for units in venue.book.values())


ENTRY_SEQUENCE_ROLES: tuple[LegRole, ...] = tuple(LegRole(r.value) for r in ENTRY_SEQUENCE)


def test_the_entry_sequence_is_longs_first() -> None:
    """``04`` §2, as the desk sends it (``fno_execute._enter_condor`` reads ``ENTRY_SEQUENCE``)."""
    longs = [r.is_long for r in ENTRY_SEQUENCE_ROLES]
    assert longs == sorted(longs, reverse=True) and longs.count(True) == 2


# --- never into expiry day ------------------------------------------------------------------------


@st.composite
def calendars(draw: st.DrawFn) -> tuple[list[dt.date], dt.date]:
    """Weekday sessions from October 2026 to March 2027 with random holidays, and a monthly."""
    start = dt.date(2026, 10, 1)
    days = [start + dt.timedelta(days=i) for i in range(182)]
    weekdays = [d for d in days if d.weekday() < 5]
    holidays = set(draw(st.lists(st.sampled_from(weekdays), max_size=12)))
    sessions = [d for d in weekdays if d not in holidays]
    expiry = draw(st.sampled_from(list(monthly_expiries(sessions))[1:]))
    return sessions, expiry


times = st.times(min_value=dt.time(0, 0), max_value=dt.time(23, 59, 59))
costs = st.one_of(st.none(), st.decimals("0", "2000", places=2))


@settings(max_examples=400, deadline=None)
@given(
    cal=calendars(),
    n=st.integers(1, 5),
    days_after=st.integers(0, 10),
    clock=times,
    close_cost=costs,
)
def test_no_f1_position_survives_its_hard_exit_and_none_reaches_expiry_day(
    cal: tuple[list[dt.date], dt.date],
    n: int,
    days_after: int,
    clock: dt.time,
    close_cost: Decimal | None,
) -> None:
    sessions, expiry = cal
    common = CommonConfig(hard_exit_before_expiry=n)
    hard = hard_exit_session(sessions, expiry, n)
    assert hard is not None and hard < expiry  # never held into expiry day, index included
    day = hard + dt.timedelta(days=days_after)
    if days_after == 0 and clock < common.hard_exit_time:
        return  # before 15:00 on E - n the structure may still be held
    decision = f1_exit(
        now=dt.datetime.combine(day, clock),
        hard_exit_date=hard,
        close_cost=close_cost,
        entry_credit=Decimal("90"),
        strikes=CondorStrikes(Decimal(25400), Decimal(25000), Decimal(23000), Decimal(22600)),
        f1=F1Config(),
        common=common,
    )
    assert decision is not None, "an F1 structure was held past 15:00 on E - n"
    if days_after > 0:
        assert decision.reason is ExitReason.LATE_EXIT


prices = st.one_of(st.none(), st.decimals("1", "5000", places=2))


@settings(max_examples=400, deadline=None)
@given(
    cal=calendars(),
    n=st.integers(1, 3),
    days_after=st.integers(0, 10),
    clock=times,
    price=prices,
    time_exit_offset=st.one_of(st.none(), st.integers(-30, 30)),
    naked=st.booleans(),
)
def test_no_stock_future_survives_e_minus_1_1500(  # noqa: PLR0913, PLR0917 - the whole tick
    cal: tuple[list[dt.date], dt.date],
    n: int,
    days_after: int,
    clock: dt.time,
    price: Decimal | None,
    time_exit_offset: int | None,
    naked: bool,
) -> None:
    sessions, expiry = cal
    common = CommonConfig()
    F2Config(roll_before_expiry=n)  # admissible
    roll = roll_session(sessions, expiry, n)
    assert roll is not None and roll < expiry
    time_exit = (None if time_exit_offset is None
                 else roll + dt.timedelta(days=time_exit_offset))  # fmt: skip
    now = dt.datetime.combine(roll + dt.timedelta(days=days_after), clock)
    action = f2_action(now=now, price=price, trigger=Decimal("1000"), time_exit_date=time_exit,
                       roll_date=roll, naked=naked, common=common)  # fmt: skip
    if naked:
        assert action is not None and action.reason is ExitReason.NAKED_FUTURE
        return
    if price is not None and price <= Decimal("1000"):
        assert action is not None  # the stop, or a late exit when the roll date has passed
        assert action.reason in {ExitReason.STOP, ExitReason.LATE_EXIT}
        return
    if days_after == 0 and clock < common.hard_exit_time:
        return  # before 15:00 on E - n the future may still be held
    assert action is not None, "a stock future was held past 15:00 on E - n"
    assert action.reason in {ExitReason.ROLL, ExitReason.TIME_EXIT, ExitReason.LATE_EXIT}
    if days_after > 0:
        assert action.reason in {ExitReason.LATE_EXIT, ExitReason.TIME_EXIT}


def test_zero_sessions_before_expiry_is_not_a_setting() -> None:
    """``02`` §2.2: ``fo_hard_exit_before_expiry`` and ``f2_roll_before_expiry`` are never 0."""
    with pytest.raises(ValueError, match="fo_hard_exit_before_expiry"):
        CommonConfig(hard_exit_before_expiry=0)
    with pytest.raises(ValueError, match="f2_roll_before_expiry"):
        F2Config(roll_before_expiry=0)
    with pytest.raises(ValueError, match="never zero"):
        hard_exit_session([EXPIRY], EXPIRY, 0)
    assert CommonConfig().hard_exit_time == dt.time(15, 0)


# --- the source scans -----------------------------------------------------------------------------

SKIP_DIRS = {
    "node_modules", ".venv", "venv", ".next", "__pycache__", ".git", "dist", "build", "coverage",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "frozen", "data", "tests", "__tests__", "e2e",
    "docs", "evidence", "fixtures", "playwright-report", "test-results",
}  # fmt: skip
CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".yml", ".yaml", ".toml", ".json", ".plist",
    ".service", ".timer", ".cron", ".conf", ".example", ".html", ".j2", ".compose",
}  # fmt: skip


def _files(*tops: Path) -> Iterator[Path]:
    for top in tops:
        if top.is_file():
            yield top
            continue
        for path in top.rglob("*"):
            rel = path.relative_to(ROOT).parts
            if any(part in SKIP_DIRS for part in rel[:-1]) or not path.is_file():
                continue
            if path.suffix in CODE_SUFFIXES or path.name.startswith((".env", "Makefile")):
                yield path


def _python_code(source: str) -> str:
    """The source with its docstrings and comments removed: what runs, not what is said."""
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                spans.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
    lines = source.splitlines()
    for start, end in spans:
        for i in range(start - 1, end):
            lines[i] = ""
    kept = "\n".join(lines)
    return " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(kept).readline)
        if token.type != tokenize.COMMENT
    )


_TS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", re.DOTALL)
_HASH_COMMENT = re.compile(r"(?m)^\s*#.*$")


def _code(path: Path) -> str:
    text = path.read_text(errors="replace")
    if path.suffix == ".py":
        return _python_code(text)
    if path.suffix in {".ts", ".tsx", ".js", ".mjs"}:
        return _TS_COMMENT.sub("", text)
    return _HASH_COMMENT.sub("", text)


AUTO_FLAG = re.compile(r"FNO\w*AUTO|AUTO\w*FNO", re.IGNORECASE)
#: The one admitted name (DECISIONS-FO M.5, Maulik in session 28 Sep 2026: "Auto-exit under a new
#: flag"): the F3 monitor may send the **exit** it raised. It is his; the scan admits this exact
#: spelling and nothing else, so a second name, or an entry variant, is still a red test.
ADMITTED_AUTO_NAME = "BASKFY_FNO_F3_AUTO_EXIT"


def _auto_scan_tops() -> list[Path]:
    tops = [
        SCREENER / "packages", SCREENER / "services", SCREENER / "apps", SCREENER / "infra",
        DESK / "app", DESK / "scripts", DESK / "deploy", DESK / "config", ROOT / "tools",
        ROOT / "ops", ROOT / "infra",
    ]  # fmt: skip
    env_files = (
        *ROOT.glob(".env*.example"), *ROOT.glob(".env*.compose"), *ROOT.glob(".env.example"),
        *SCREENER.glob(".env*.example"), *SCREENER.glob(".env.example"),
        *DESK.glob(".env*.example"), *(SCREENER / "infra/docker").glob(".env*.compose"),
        *(SCREENER / "infra/docker").glob("compose*.yml"), *ROOT.glob("compose*.yml"),
        *ROOT.glob("docker-compose*.yml"),
    )  # fmt: skip
    return [p for p in (*tops, *env_files) if p.exists()]


def test_no_fno_auto_execute_name_is_read_anywhere() -> None:
    tops = _auto_scan_tops()
    scanned = list(_files(*tops))
    assert len(scanned) > 500  # the scan really read the trees
    names = {p.name for p in scanned}
    assert {".env.staging.compose", "compose.yml", ".env.example"} <= names  # and the env files
    hits = [
        str(p.relative_to(ROOT))
        for p in scanned
        if AUTO_FLAG.search(_code(p).replace(ADMITTED_AUTO_NAME, ""))
    ]
    assert hits == []


def test_the_admitted_name_is_the_exit_flag_alone() -> None:
    """M.5 admits one spelling; ``F3_AUTO_EXECUTE`` or a ``F1``/``F2`` variant would still trip."""
    assert AUTO_FLAG.search(ADMITTED_AUTO_NAME)
    assert ADMITTED_AUTO_NAME.endswith("_AUTO_EXIT") and "F3" in ADMITTED_AUTO_NAME
    for variant in (
        "BASKFY_FNO_F3_AUTO_EXECUTE",
        "BASKFY_FNO_F1_AUTO_EXIT",
        "BASKFY_FNO_AUTO_EXIT",
    ):
        assert AUTO_FLAG.search(variant.replace(ADMITTED_AUTO_NAME, "")), variant


def test_the_auto_pattern_would_catch_one() -> None:
    """Non-vacuity: the regex finds the names it exists to find, in each spelling."""
    for name in ("BASKFY_FNO_AUTO_EXECUTE", "BASKFY_FNO_F1_AUTO_EXECUTE", "FNO_AUTO",
                 "fno_autorun", "AUTO_FNO_CONFIRM"):  # fmt: skip
        assert AUTO_FLAG.search(name), name


WEB = SCREENER / "apps/web/src"
API = SCREENER / "services/api/src"
#: What reaching an order looks like from outside the desk: the FO execute route (or any desk
#: execute route), the gateway class, its module, a broker write, or the desk's executor.
ORDER_PATH = re.compile(
    r"fno/execute|nifty-options/execute|OrderGateway|baskfy_execution\.gateway|"
    r"baskfy_execution\s+import\s+[^\n;]*\bOrderGateway|place_order|place_gtt|modify_gtt|"
    r"delete_gtt|gateway\.place|execute_entry|execute_exit|fno_execute|run_pending"
)


def test_the_web_app_and_the_api_have_no_route_to_the_gateway() -> None:
    scanned = list(_files(WEB, API))
    assert len(scanned) > 200
    # FO5's files are read as they stand on disk, committed or not.
    fo5 = {
        API / "baskfy_api/routers/fno.py", API / "baskfy_api/fno_read.py",
        API / "baskfy_api/fno_settings.py", WEB / "lib/fno/fetch.ts",
    }  # fmt: skip
    assert {p for p in fo5 if p.exists()} <= set(scanned)
    hits = {
        str(path.relative_to(ROOT)): sorted(set(ORDER_PATH.findall(_code(path))))
        for path in scanned
    }
    assert {k: v for k, v in hits.items() if v} == {}


def test_the_api_router_for_fno_declares_no_order_verb() -> None:
    router = API / "baskfy_api/routers/fno.py"
    if not router.exists():
        pytest.skip("FO5's router is not on disk")
    code = _HASH_COMMENT.sub("", router.read_text())
    assert not re.search(r"@\s*router\s*\.\s*(post|put|delete|api_route)\s*\(", code)
    assert re.findall(r"@\s*router\s*\.\s*patch\s*\(\s*\"([^\"]+)\"", code) == ["/config"]


#: The broker adapter the gateway itself calls (``test_seven_non_negotiables`` 6b's allowance).
PLACE_ORDER_ALLOWED = {DESK / "app/kite_client.py"}
#: A drill that calls ``place_order`` on its own recording spy to prove the spy records.
PLACE_ORDER_SPIES = {ROOT / "tools/friday-drill.py"}


def _place_order_callers(*tops: Path) -> list[str]:
    found: list[str] = []
    for path in _files(*tops):
        if path.suffix != ".py" or path in PLACE_ORDER_ALLOWED | PLACE_ORDER_SPIES:
            continue
        if (SCREENER / "packages/execution/src") in path.parents:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "place_order"
            ):
                found.append(str(path.relative_to(ROOT)))
    return sorted(set(found))


def test_nothing_outside_packages_execution_calls_place_order() -> None:
    tops = [SCREENER / "packages", SCREENER / "services", DESK / "app", DESK / "scripts",
            ROOT / "tools"]  # fmt: skip
    assert _place_order_callers(*tops) == []
    # Non-vacuity: the execution package does call it, and the scan sees that when asked.
    gateway = SCREENER / "packages/execution/src/baskfy_execution"
    assert (
        "place_order" in (gateway / "brokers.py").read_text() + (gateway / "gateway.py").read_text()
    )


def test_frozen_is_untouched() -> None:
    """Track C §10: no change to ``frozen/`` against ``HEAD`` — tracked, staged or untracked."""
    frozen = ROOT / "frozen"
    assert frozen.is_dir()
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "frozen"],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert tracked  # the tree is under version control, so "unchanged" means something
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", "frozen"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert status == ""
    diff = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", "frozen"], check=False
    )
    assert diff.returncode == 0


def test_nothing_schedules_the_fno_execute_route() -> None:
    """Track C §3: an entry needs ``POST /fno/execute``; only the desk's route and its page name
    it — no worker task, script, tool, compose file or timer does."""
    tops = [
        SCREENER / "services",
        SCREENER / "infra",
        SCREENER / "packages",
        DESK / "app",
        DESK / "scripts",
        DESK / "deploy",
        ROOT / "tools",
        ROOT / "ops",
        ROOT / "infra",
    ]
    allowed = {DESK / "app/fno_desk.py", DESK / "app/templates/fno.html",
               ROOT / "tools/fno/drill.py"}  # fmt: skip
    route = re.compile(r"fno/execute")
    hits = [
        str(p.relative_to(ROOT))
        for p in _files(*(t for t in tops if t.exists()))
        if p not in allowed and route.search(_code(p))
    ]
    assert hits == []
    assert '@router.post("/fno/execute")' in (DESK / "app/fno_desk.py").read_text()


def test_only_the_fno_post_route_calls_the_fno_execute_entry() -> None:
    """The FO book's ``execute_entry`` has one caller: the route's handler. The monitor raises
    plans and runs exits under a confirm; it never confirms (Track C §3)."""
    callers = sorted(
        str(p.relative_to(ROOT))
        for p in _files(DESK / "app", DESK / "scripts")
        if p.suffix == ".py" and re.search(r"\bexecute_entry\s*\(", _code(p)) and "fno" in p.name
    )
    assert callers == [
        "kite-momentum-rebalancer/app/fno_desk.py",
        "kite-momentum-rebalancer/app/fno_execute.py",
    ]
    monitor = _code(DESK / "app/fno_monitor.py")
    assert "execute_entry" not in monitor and ". confirm (" not in monitor


def test_no_beat_entry_targets_an_fo_order() -> None:
    from baskfy_worker.celery_app import BEAT_SCHEDULE  # noqa: PLC0415 - the worker's schedule

    tasks = [str(entry["task"]) for entry in BEAT_SCHEDULE.values()]
    fno = [t for t in tasks if t.startswith("baskfy.fno.")]
    assert fno  # the FO book does have scheduled reads
    assert not [t for t in fno if re.search(r"execute|confirm|order|place|entry", t)]


def test_02_still_asks_for_these_scans() -> None:
    text = " ".join((ROOT / "docs/fno/02-scope-and-gating.md").read_text().split())
    assert "The one name matching `BASKFY_FNO_*AUTO*` is **`BASKFY_FNO_F3_AUTO_EXIT`**" in text
    assert "never an entry or an add" in text
    plan = " ".join((ROOT / "docs/fno/06-module-plan.md").read_text().split())
    assert "No `BASKFY_FNO_*AUTO*` name is read anywhere" in plan
