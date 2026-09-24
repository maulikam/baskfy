"""OP13 — the pure half of the safety proof and the source scans (``06`` OP13, ``02`` Tracks B-C).

Properties, by Hypothesis over fill paths and tick paths:

* **never naked** (Track C §2): whatever each attempt fills — nothing, part, all — the book is never
  short more than long on either side after any fill of an entry or its exit (the venue asserts it
  after every fill; the executor asserts it before every send);
* **an exit closes exactly the position's legs**: it sends only for a leg with units open, never
  closes more than is open, and a ``FLAT`` exit leaves every leg at zero;
* **never overnight** (Track C §1): every sleeve's hard exit is at or before the 15:00 ceiling,
  and at or after it ``exits.evaluate`` closes any position whatever the marks and the index say.

Source scans, docstrings and comments stripped:

* no gateway, broker or order path in the web app's, the API's or the worker's options code (Track
  C §4); the pure core imports no gateway either;
* no ``*OPTIONS*AUTO*`` flag is read anywhere in the repo (Track B, PACK.3);
* nothing schedules ``/nifty-options/execute``: only the desk's POST route and its form name it, no
  Beat entry targets an options execute, and only the route calls ``execute_entry`` (Track C §3).

The desk's half — the side door, the gateway's refusals, the four-flag AND with a spy, no second
entry — is ``kite-momentum-rebalancer/tests/test_options_safety.py``.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from baskfy_core.options.bars import IST
from baskfy_core.options.config import ExecutionConfig, OptionsConfig, Sleeve
from baskfy_core.options.execution import Attempt, LegRole, never_naked
from baskfy_core.options.executor import Book, Fill, Outcome, run_entry, run_exit
from baskfy_core.options.exits import IndexState, LegMark, OpenLeg, OpenPosition, evaluate
from baskfy_core.options.structures import Direction, Structure

ROOT = Path(__file__).resolve().parents[4]
CFG = ExecutionConfig()
OPTIONS = OptionsConfig()
TICK = Decimal("0.05")
QTY = 65
STRUCTURES: dict[Sleeve, tuple[LegRole, ...]] = {
    Sleeve.O1M: (LegRole.SHORT_CALL, LegRole.SHORT_PUT, LegRole.LONG_CALL, LegRole.LONG_PUT),
    Sleeve.O1W: (LegRole.SHORT_CALL, LegRole.SHORT_PUT, LegRole.LONG_CALL, LegRole.LONG_PUT),
    Sleeve.O2: (LegRole.LONG_CALL,),
    Sleeve.O3A: (LegRole.LONG_CALL, LegRole.SHORT_CALL),
    Sleeve.O3B: (LegRole.LONG_PUT, LegRole.SHORT_PUT),
}


# --- never naked, and exits close exactly the position ---------------------------------------


@dataclass
class ScriptedVenue:
    """Each send fills the next scripted fraction of its order (cycling), and the venue checks
    the book after every fill and the exit's discipline after every closing send."""

    script: list[int]
    book: dict[LegRole, int] = field(default_factory=dict)
    sends: int = 0

    def quote(self, role: LegRole) -> Book:
        del role
        return Book(Decimal("10.00"), Decimal("10.20"))

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        share = self.script[self.sends % len(self.script)] if self.script else 100
        self.sends += 1
        filled = quantity * share // 100
        if closing:
            open_units = self.book.get(role, 0)
            assert open_units > 0, f"a close was sent for {role} with nothing open"
            assert quantity <= open_units, f"closing {quantity} of {open_units} {role}"
            self.book[role] = open_units - filled
        else:
            self.book[role] = self.book.get(role, 0) + filled
        assert never_naked(self.book), (role, self.book)
        return Fill(filled, attempt.price if filled else None)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    sleeve=st.sampled_from(list(Sleeve)),
    script=st.lists(st.sampled_from([0, 0, 25, 50, 100, 100, 100]), min_size=1, max_size=24),
)
def test_never_naked_and_the_exit_closes_exactly_the_position(
    sleeve: Sleeve, script: list[int]
) -> None:
    venue = ScriptedVenue(script)
    entry = run_entry(venue, STRUCTURES[sleeve], QTY, sleeve=sleeve, tick=TICK, config=CFG)
    assert never_naked(entry.position)
    if entry.outcome is not Outcome.OPEN:
        return
    assert entry.position == venue.book
    held = {role: units for role, units in venue.book.items() if units}
    closed = run_exit(venue, dict(held), sleeve=sleeve, tick=TICK, config=CFG)
    for role in venue.book:
        assert 0 <= venue.book[role] <= held.get(role, 0)
    if closed.outcome is Outcome.FLAT:
        assert all(units == 0 for units in venue.book.values())


# --- never overnight -------------------------------------------------------------------------

HARD_EXIT_CEILING = dt.time(15, 0)
DAY = dt.date(2026, 10, 27)


def _position(sleeve: Sleeve) -> OpenPosition:
    roles = STRUCTURES[sleeve]
    structure = {4: Structure.IRON_CONDOR, 1: Structure.LONG_OPTION, 2: Structure.DEBIT_SPREAD}[
        len(roles)
    ]
    legs = tuple(
        OpenLeg(role, Decimal(25000 + 100 * i), QTY, Decimal("20.00"), i + 1)
        for i, role in enumerate(roles)
    )
    return OpenPosition(
        sleeve=sleeve, structure=structure, legs=legs, entry_points=Decimal("20.00"),
        opened_at=dt.datetime.combine(DAY, dt.time(10, 5), IST),
        risk_budget_inr=Decimal("50000"), direction=Direction.UP,
        range_high=Decimal("25100"), range_low=Decimal("24900"), half_gap=Decimal("24950"),
    )  # fmt: skip


def test_no_sleeve_holds_past_the_ceiling() -> None:
    for sleeve in Sleeve:
        assert OPTIONS.hard_exit_time(sleeve) <= HARD_EXIT_CEILING, sleeve


prices = st.one_of(st.none(), st.decimals("0.05", "500", places=2))


@settings(max_examples=300, deadline=None)
@given(
    sleeve=st.sampled_from(list(Sleeve)),
    minutes_after=st.integers(0, 30),
    bid=prices,
    ask=prices,
    spot=st.one_of(st.none(), st.decimals("24000", "26000", places=2)),
    tick_age=st.integers(0, 600),
)
def test_at_or_after_the_hard_exit_every_position_closes(  # noqa: PLR0913, PLR0917 - the whole tick
    sleeve: Sleeve,
    minutes_after: int,
    bid: Decimal | None,
    ask: Decimal | None,
    spot: Decimal | None,
    tick_age: int,
) -> None:
    hard = dt.datetime.combine(DAY, OPTIONS.hard_exit_time(sleeve), IST)
    now = hard + dt.timedelta(minutes=minutes_after)
    position = _position(sleeve)
    marks = {leg.role: LegMark(bid, ask, now) for leg in position.legs}
    index = IndexState(spot=spot, last_tick_at=now - dt.timedelta(seconds=tick_age))
    verdict = evaluate(position, marks, index, now, options=OPTIONS)
    assert verdict is not None, "a position was held at or past its hard exit"


# --- the source scans ------------------------------------------------------------------------

SKIP_DIRS = {
    "node_modules", ".venv", "venv", ".next", "__pycache__", ".git", "dist", "build", "coverage",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "frozen", "data", "tests", "__tests__", "e2e",
    "docs", "evidence", "fixtures", "playwright-report", "test-results",
}  # fmt: skip
CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".yml", ".yaml", ".toml", ".json", ".plist",
    ".service", ".timer", ".cron", ".conf", ".example", ".html", ".j2",
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


def _python_code(path: Path) -> str:
    """The file with its docstrings and comments removed: what runs, not what is said about it."""
    source = path.read_text()
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
    out: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(kept).readline):
        if token.type != tokenize.COMMENT:
            out.append(token.string)
    return " ".join(out)


_TS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", re.DOTALL)
_HASH_COMMENT = re.compile(r"(?m)^\s*#.*$")


def _code(path: Path) -> str:
    if path.suffix == ".py":
        return _python_code(path)
    text = path.read_text(errors="replace")
    if path.suffix in {".ts", ".tsx", ".js", ".mjs"}:
        return _TS_COMMENT.sub("", text)
    return _HASH_COMMENT.sub("", text)


SCREENER = ROOT / "decile-blueprint"
DESK = ROOT / "kite-momentum-rebalancer"
OPTIONS_CODE = [
    SCREENER / "packages/core/src/baskfy_core/options",
    SCREENER / "services/worker/src/baskfy_worker/options",
    SCREENER / "services/worker/src/baskfy_worker/options_cli.py",
    SCREENER / "services/api/src/baskfy_api/routers/options.py",
    SCREENER / "services/api/src/baskfy_api/options_read.py",
    SCREENER / "apps/web/src/app/(app)/options",
    SCREENER / "apps/web/src/components/options",
    SCREENER / "apps/web/src/lib/options",
]
ORDER_PATH = re.compile(
    r"baskfy_execution|OrderGateway|place_order|place_gtt|kite_client|gateway\.place|"
    r"nifty-options/execute|execute_entry|execute_exit"
)


def test_the_options_code_outside_the_desk_has_no_order_path() -> None:
    scanned = list(_files(*(p for p in OPTIONS_CODE if p.exists())))
    assert len(scanned) > 40  # the scan really read the trees
    hits = {
        str(path.relative_to(ROOT)): sorted(set(ORDER_PATH.findall(_code(path))))
        for path in scanned
    }
    assert {k: v for k, v in hits.items() if v} == {}


AUTO_FLAG = re.compile(r"OPTIONS\w*AUTO|AUTO\w*OPTIONS")


def test_no_options_auto_execute_flag_is_read_anywhere() -> None:
    tops = [SCREENER / "packages", SCREENER / "services", SCREENER / "apps", SCREENER / "infra",
            DESK / "app", DESK / "scripts", ROOT / "tools", ROOT / "infra"]  # fmt: skip
    tops += [p for p in (*SCREENER.glob(".env*"), *DESK.glob(".env*.example")) if p.is_file()]
    scanned = list(_files(*(p for p in tops if p.exists())))
    assert len(scanned) > 500
    hits = [str(p.relative_to(ROOT)) for p in scanned if AUTO_FLAG.search(_code(p))]
    assert hits == []


def test_nothing_schedules_the_options_execute_route() -> None:
    tops = [SCREENER / "services", SCREENER / "infra", DESK / "app", DESK / "scripts",
            ROOT / "tools", ROOT / "infra"]  # fmt: skip
    route = re.compile(r"nifty-options/execute")
    allowed = {DESK / "app/options_desk.py", DESK / "app/templates/nifty_options.html"}
    hits = [
        str(p.relative_to(ROOT))
        for p in _files(*(t for t in tops if t.exists()))
        if p not in allowed and route.search(_code(p))
    ]
    assert hits == []


def test_only_the_post_route_calls_execute_entry() -> None:
    callers = [
        str(p.relative_to(ROOT))
        for p in _files(DESK / "app", DESK / "scripts")
        if p.suffix == ".py" and re.search(r"\bexecute_entry\s*\(", _code(p))
    ]
    # options_execute defines it; options_desk's POST handler is the one caller. FO7/FO8 added
    # the FO book's own `execute_entry` (fno_execute defines it, fno_desk's POST /fno/execute is
    # its one caller), which this scan also sees; the list stays exact for both books
    # (DECISIONS-FO FO11.3; the FO half is test_fno_safety_proof).
    assert sorted(callers) == [
        "kite-momentum-rebalancer/app/fno_desk.py",
        "kite-momentum-rebalancer/app/fno_execute.py",
        "kite-momentum-rebalancer/app/options_desk.py",
        "kite-momentum-rebalancer/app/options_execute.py",
    ]
    assert '"/nifty-options/execute"' in (DESK / "app/options_desk.py").read_text()


def test_no_beat_entry_targets_an_options_order() -> None:
    from baskfy_worker.celery_app import BEAT_SCHEDULE  # noqa: PLC0415 - the worker's schedule

    tasks = [str(entry["task"]) for entry in BEAT_SCHEDULE.values()]
    options = [t for t in tasks if t.startswith("baskfy.options.")]
    assert options  # the options book does have scheduled reads and plans
    assert not [t for t in options if re.search(r"execute|confirm|order|place", t)]


def test_02_still_asks_for_these_scans() -> None:
    text = " ".join((ROOT / "docs/options/02-scope-and-gating.md").read_text().split())
    assert "No name matching `BASKFY_OPTIONS_*AUTO*` is read anywhere (OP13 scans for it)" in text
