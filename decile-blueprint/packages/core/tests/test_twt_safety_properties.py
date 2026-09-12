"""TW10's safety half: every Track-B and Track-C claim of ``docs/twt/02`` as a test.

``docs/twt/06`` § TW10 asks for **a property test over every route and every task, not a spot
check**, and the property is one sentence:

    With ``BASKFY_TWT_EXECUTION_ENABLED`` false, no TWT code path reaches ``OrderGateway.place``
    or ``place_gtt_stop`` with ``DRY_RUN=false``.

The clause that matters is the last one, and it is the clause the existing suite cannot give.
``kite-momentum-rebalancer/tests/test_twt_execute.py`` pins **both** switches — its
``_flag_is_false`` fixture sets ``TWT_EXECUTION_ENABLED=False`` *and* ``DRY_RUN=True`` — so every
test there passes for two reasons and cannot tell you which one did the work. If the desk went
live tomorrow (``DRY_RUN=false``) and only the sleeve flag held this book back, that suite would
not notice a regression. Here ``DRY_RUN`` is **False** in every test below, so the sleeve's own
flag is the only thing standing between this code and a broker.

``app/twt_execute.py``'s gates are where that lives::

    dry_run = C.DRY_RUN or not C.TWT_EXECUTION_ENABLED

Two independent switches, either sufficient. This module asserts the right-hand one alone.

**Why "every route and every task" is discovered rather than listed.** A property over a
hardcoded list stops being a property the moment somebody adds the eighth route. So the routes
are parsed out of ``app/twt_desk.py`` and the tasks out of ``celery_tasks.py``, and
:class:`TestTheSurfaceIsWhatWeThinkItIs` fails when either set changes — which makes adding a
TWT route a deliberate act with a failing test on it, exactly as ``test_vbt_safety.py`` does for
VBT-1.

**Why this lives in ``packages/core/tests`` despite law 1.** Law 1 constrains the core *package*,
not its test suite; ``test_broker_capability_honesty.py`` already imports ``baskfy_api`` from
here and says so. ``gates/twt-10.md`` G1 names this path. Nothing in ``baskfy_core`` imports
anything below.

**The journal.** The desk's ``tests/conftest.py`` points the gateway's journal at a tmp file and
is autouse — and it does not apply here, because this file is in the other tree's rootdir. So
:func:`_journal_is_isolated` does the same job explicitly. Without it these tests would append
synthetic orders to the real audit record, which is the one file that must only ever contain
events that happened.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol

import pytest

REPO: Final = Path(__file__).resolve().parents[3].parent
DESK: Final = REPO / "kite-momentum-rebalancer"
BLUEPRINT: Final = REPO / "decile-blueprint"

for _extra in (DESK, DESK / "tests"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from app import config as C  # noqa: E402 - after the sys.path inserts above
from app import twt_execute as X  # noqa: E402
from app.core.risk import RiskManager  # noqa: E402
from test_twt_execute import (  # noqa: E402
    NOW,
    a_position,
    a_store,
    run,
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: The nine routes ``app/twt_desk.py`` mounts. Six mutate; three only read.
#:
#: **TW12 added the last two and this set is why that was a deliberate act.** Adding
#: ``POST /twt/scan`` and ``GET /twt/scan/{run_id}`` turned
#: :meth:`test_the_desk_mounts_exactly_the_routes_this_file_covers` red before a line of test was
#: written for them — which is the whole design of discovering the surface instead of listing it.
#: ``POST /twt/scan`` mutates (it inserts one ``tw_scan_run`` row) and is nevertheless money-free
#: by construction: :class:`TestTheScanRoutesAreMoneyFree` drives both against a real gateway with
#: ``DRY_RUN`` false and asserts the gateway was never called *at all*, which is a stronger claim
#: than the dry-run answer the confirm paths below can make.
EXPECTED_ROUTES: Final = frozenset(
    {
        ("GET", "/twt"),
        ("GET", "/twt/data"),
        ("POST", "/twt/execute"),
        ("POST", "/twt/halt"),
        ("POST", "/twt/rearm"),
        ("POST", "/twt/sweep"),
        ("POST", "/twt/reconcile"),
        ("POST", "/twt/scan"),
        ("GET", "/twt/scan/{run_id}"),
    }
)

#: The five Celery tasks the worker registers for this sleeve. ``twt_backtest`` is deliberately
#: absent: it is a module ``tools/twt/backtest.py`` and ``make twt-backtest`` call, never a
#: scheduled task, and :meth:`test_no_twt_task_is_scheduled_into_an_execute_path` is the assertion
#: that keeps it that way.
#:
#: ``baskfy.twt.scan`` and ``baskfy.twt.scan_publish`` are TW12's. Neither can confirm anything:
#: the scan calls ``twt.detect_session`` — the detector ``baskfy.twt.detect`` already calls — and
#: the publisher sends ids.
#:
#: **The publisher is not called a sweep**, although the swing book's and VBT-1's equivalents are.
#: On this sleeve ``sweep`` is ``sweep_naked``, which re-arms GTT stops through the gateway, and
#: ``test_twt_beat.py::test_the_sweep_is_not_on_a_timer`` refuses any TWT Beat entry carrying the
#: word. It refused this one until it was renamed. DECISIONS-TW **TW12.4**.
EXPECTED_TASKS: Final = frozenset(
    {
        "baskfy.twt.detect",
        "baskfy.twt.evening",
        "baskfy.twt.morning",
        "baskfy.twt.scan",
        "baskfy.twt.scan_publish",
    }
)

DESK_TWT_SOURCES: Final = ("twt_desk.py", "twt_execute.py")


# =========================================================================================
# Fixtures: the flag is false, DRY_RUN is FALSE, and the journal is a tmp file
# =========================================================================================


@pytest.fixture(autouse=True)
def _journal_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the gateway's journal at a per-test file.

    The desk's own autouse fixture cannot reach this tree. Unconditional and first, so no test
    here can reach ``data/outputs/orders_journal.jsonl`` by omission.
    """
    import app.core.gateway as gateway_module  # noqa: PLC0415 - after the sys.path insert

    monkeypatch.setattr(gateway_module, "JOURNAL", str(tmp_path / "orders_journal.jsonl"))


@pytest.fixture(autouse=True)
def _the_flag_is_false_and_the_desk_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The whole point of this module.** ``DRY_RUN`` is False; only the sleeve flag remains.

    No test in this file flips either switch. A test that needed to would be asserting the
    opposite of what TW10 exists to prove.
    """
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", False)


class _Gateway(Protocol):
    """The three calls a TWT confirm can make on a gateway.

    A Protocol rather than ``Any``: house rule 3 forbids an explicit ``Any`` annotation and
    ``test_no_escape_hatches.py`` scans for it, with the note that ``object`` is almost always
    the honest alternative. Here ``object`` is not enough — the spy has to *call* these — so the
    honest alternative is to say what the three calls are.
    """

    async def place(self, **kwargs: object) -> dict[str, object]: ...

    async def place_gtt_stop(self, **kwargs: object) -> dict[str, object]: ...

    async def delete_gtt(self, **kwargs: object) -> dict[str, object]: ...


class _Store(Protocol):
    """The two mappings these tests reach into on the desk's in-memory store."""

    lines: dict[int, dict[str, object]]
    positions: dict[int, dict[str, object]]


class SpyGateway:
    """A real :class:`OrderGateway` with a tape, asserting on **every** answer.

    Not "a client that raises if touched" — ``ExplodingKC`` underneath already gives that, and it
    proves only that this particular path did not call a broker. The stronger reading of ``02``
    Track B is that the gateway's own answer was a dry-run one, which is what a live desk would
    be relying on. So each answer is checked as it is produced rather than tallied at the end,
    and the failure names the call that did it.
    """

    def __init__(self, inner: _Gateway) -> None:
        self.inner = inner
        self.tape: list[tuple[str, str]] = []

    def _record(self, call: str, result: dict[str, object]) -> dict[str, object]:
        status = str(result.get("status"))
        self.tape.append((call, status))
        assert status.startswith("DRY_RUN"), (
            f"{call} answered {status!r} with BASKFY_TWT_EXECUTION_ENABLED false and "
            f"DRY_RUN false — that is a real order"
        )
        return result

    async def place(self, **kwargs: object) -> dict[str, object]:
        return self._record("place", await self.inner.place(**kwargs))

    async def place_gtt_stop(self, **kwargs: object) -> dict[str, object]:
        return self._record("place_gtt_stop", await self.inner.place_gtt_stop(**kwargs))

    async def delete_gtt(self, **kwargs: object) -> dict[str, object]:
        return self._record("delete_gtt", await self.inner.delete_gtt(**kwargs))


class CountingKC:
    """A broker client that counts instead of exploding.

    ``ExplodingKC`` is right for a unit test and wrong for a property: an exception ends the run
    at the first mistake, so the tape stops where the bug is instead of where the sleeve ends.
    Counting lets every path finish and then reports the number, which is the number TW10 is
    about. Any call at all is a failure, asserted by the caller.

    It restates ``ExplodingKC``'s constants rather than inheriting them: the desk's test module
    is not typed from this tree, and subclassing ``Any`` is exactly the escape hatch house rule 3
    forbids. The constants are the Kite client's public vocabulary and do not drift.
    """

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_CNC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = "CNC", "LIMIT", "MARKET"
    VALIDITY_DAY, GTT_TYPE_SINGLE = "DAY", "single"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def place_order(self, **_: object) -> str:
        self.calls.append("place_order")
        return "SPY-1"

    def place_gtt(self, **_: object) -> dict[str, object]:
        self.calls.append("place_gtt")
        return {"trigger_id": 1}

    def delete_gtt(self, *_: object, **__: object) -> None:
        self.calls.append("delete_gtt")

    def cancel_order(self, **_: object) -> str:
        self.calls.append("cancel_order")
        return "SPY-1"

    def instruments(self, *_: object) -> list[object]:
        self.calls.append("instruments")
        return []


def _spied() -> tuple[SpyGateway, CountingKC]:
    kc = CountingKC()
    return SpyGateway(X.build_twt_gateway(kc, RiskManager())), kc


def _a_line_that_reaches_the_gateway(kind: str) -> tuple[_Store, str, int]:
    """A store whose line of ``kind`` actually gets as far as an order call.

    Every guard in ``04`` §7 sits between a confirm and the gateway, and each kind needs a
    different arrangement to clear them — a naked position for ``ARM_GTT``, a *strictly higher*
    new stop under the last price for ``RAISE_GTT_STOP``. Getting this wrong makes the test
    vacuous rather than red, which is the failure mode this helper exists to remove.
    """
    if kind == "BUY_AT_OPEN":
        store, plan_id, line_id = a_store(kind)
        return store, str(plan_id), int(line_id)
    if kind == "ARM_GTT":
        store, plan_id, line_id = a_store(kind, stop_price=Decimal("80.00"))
        position_id = a_position(store, gtt_id=None, gtt_trigger=None)
        store.lines[line_id]["position_id"] = position_id
        return store, str(plan_id), int(line_id)
    if kind == "RAISE_GTT_STOP":
        # Strictly above the resting 80.00 and below the 130.00 last price: a raise the
        # ratchet accepts, so it cancels and re-arms and the gateway is actually asked.
        store, plan_id, line_id = a_store(kind, stop_price=Decimal("95.00"))
        position_id = a_position(store, gtt_id="551234", gtt_trigger="80.00", stop="80.00")
        store.lines[line_id]["position_id"] = position_id
        return store, str(plan_id), int(line_id)
    raise AssertionError(f"no gateway-reaching setup for {kind} — add one rather than skipping")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


#: Triple-quoted strings, then ``#`` and ``//`` comments. Crude on purpose, and the same stripper
#: ``test_vbt_safety.py`` uses: everything it removes is prose, and the thing being looked for is
#: an assignment. A cleverer stripper that is occasionally wrong would be worse.
_DOCSTRING = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
_COMMENT = re.compile(r"(#|//).*$", re.MULTILINE)


def _strip_prose(source: str) -> str:
    return _COMMENT.sub("", _DOCSTRING.sub("", source))


# =========================================================================================
# G1 part one — the surface is discovered, so a new route cannot arrive unnoticed
# =========================================================================================


class TestTheSurfaceIsWhatWeThinkItIs:
    """The enumeration below is what "every route and every task" means in this file.

    When somebody adds an eighth route or a fourth task, these fail. That is the intent: the
    coverage tests further down iterate over the *same* constants, so a new surface is a
    deliberate act that has to be added here first.
    """

    def _routes(self) -> set[tuple[str, str]]:
        """Every ``@router.<verb>("/twt...")`` decorator in the desk's TWT modules."""
        found: set[tuple[str, str]] = set()
        for name in DESK_TWT_SOURCES:
            tree = ast.parse(_source(DESK / "app" / name))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for deco in node.decorator_list:
                    if not isinstance(deco, ast.Call):
                        continue
                    func = deco.func
                    if not isinstance(func, ast.Attribute):
                        continue
                    value = func.value
                    if not isinstance(value, ast.Name) or value.id != "router":
                        continue
                    if not deco.args or not isinstance(deco.args[0], ast.Constant):
                        continue
                    path = str(deco.args[0].value)
                    if path.startswith("/twt"):
                        found.add((func.attr.upper(), path))
        return found

    def _tasks(self) -> set[str]:
        """Every ``@shared_task(name=...)`` in the worker's registry whose name is a TWT one.

        **This used to be a regex over string literals, and TW12 found the hole.** The registry
        registers several tasks through module constants (``SWING_SCAN_NOW_TASK``,
        ``SWING_SCAN_SWEEP_TASK``), and a TWT task registered the same way would have been
        invisible here — a scheduled path the safety property does not know exists is exactly
        what "every route and every task" is supposed to make impossible. It never mattered while
        every ``baskfy.twt.*`` name happened to be spelled inline, which is the worst kind of
        safe: safe by coincidence.

        So the scan is now AST, and it resolves the two shapes a decorator can take — a literal
        and a module-level constant bound to a literal. Anything it *cannot* resolve is reported
        rather than dropped (:meth:`test_no_shared_task_name_is_unresolvable`), because a name
        this function silently ignores is the same hole in a different place.
        """
        return {name for name in self._registered_task_names() if name.startswith("baskfy.twt")}

    def _registry_source(self) -> str:
        return _source(
            BLUEPRINT
            / "services"
            / "worker"
            / "src"
            / "baskfy_worker"
            / "tasks"
            / "celery_tasks.py"
        )

    @staticmethod
    def _string_constants(source: str) -> dict[str, str]:
        """Module-level ``NAME = "literal"`` / ``NAME: Final = "literal"`` bindings."""
        found: dict[str, str] = {}
        for node in ast.parse(source).body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            value = getattr(node, "value", None)
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = value.value
        return found

    @classmethod
    def _imported_string_constants(cls, source: str) -> dict[str, str]:
        """String constants the registry imports from its sibling task modules.

        The third shape, and the one that made this whole scan necessary:
        ``from baskfy_worker.tasks.swing_backtest import SWING_BACKTEST_TASK`` and then
        ``@shared_task(name=SWING_BACKTEST_TASK)``. A scan that stops at this module's own
        constants reports that decorator as unreadable — honest, but it would wedge the suite on
        a task that is perfectly fine. So the import is followed, one level, into the worker's own
        package and nowhere else.
        """
        resolved: dict[str, str] = {}
        tasks_dir = BLUEPRINT / "services" / "worker" / "src" / "baskfy_worker" / "tasks"
        for node in ast.parse(source).body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("baskfy_worker.tasks."):
                continue
            path = tasks_dir / f"{node.module.rsplit('.', 1)[-1]}.py"
            if not path.exists():
                continue
            constants = cls._string_constants(_source(path))
            for alias in node.names:
                if alias.name in constants:
                    resolved[alias.asname or alias.name] = constants[alias.name]
        return resolved

    @classmethod
    def _shared_task_names(cls, source: str) -> tuple[set[str], set[str]]:
        """``(resolved names, unresolvable decorator expressions)`` from one module's source."""
        tree = ast.parse(source)
        constants: dict[str, str] = {
            **cls._imported_string_constants(source),
            **cls._string_constants(source),
        }

        names: set[str] = set()
        unresolved: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                func = deco.func
                label = getattr(func, "id", None) or getattr(func, "attr", None)
                if label != "shared_task":
                    continue
                keyword = next((k for k in deco.keywords if k.arg == "name"), None)
                if keyword is None:
                    unresolved.add(f"{node.name}: @shared_task with no name=")
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    names.add(keyword.value.value)
                elif isinstance(keyword.value, ast.Name) and keyword.value.id in constants:
                    names.add(constants[keyword.value.id])
                else:
                    unresolved.add(f"{node.name}: name={ast.dump(keyword.value)[:60]}")
        return names, unresolved

    def _registered_task_names(self) -> set[str]:
        names, _unresolved = self._shared_task_names(self._registry_source())
        return names

    def test_the_desk_mounts_exactly_the_routes_this_file_covers(self) -> None:
        assert self._routes() == EXPECTED_ROUTES

    def test_the_worker_registers_exactly_the_tasks_this_file_covers(self) -> None:
        assert self._tasks() == EXPECTED_TASKS

    def test_no_shared_task_name_is_unresolvable(self) -> None:
        """A decorator this scan cannot read is a task it cannot check. Say so, loudly.

        Not "ignore the ones we cannot parse": that is how the literal-only regex this replaced
        managed to report three TWT tasks when the sleeve had five.
        """
        _names, unresolved = self._shared_task_names(self._registry_source())
        assert unresolved == set(), (
            "a @shared_task name could not be resolved from source, so the surface scan below "
            f"cannot claim to be complete: {sorted(unresolved)}"
        )

    def test_the_task_scan_resolves_a_constant_and_not_only_a_literal(self) -> None:
        """Non-vacuity for the hardening above, planted rather than trusted.

        Both shapes are registered in the planted module and both must come back. Without the
        constant arm the regex this replaced would report one of two.
        """
        planted = (
            'LITERAL_ELSEWHERE: Final = "baskfy.twt.sweep_by_constant"\n'
            '@shared_task(name="baskfy.twt.sweep_by_literal")\n'
            "def by_literal() -> None:\n    ...\n\n"
            "@shared_task(name=LITERAL_ELSEWHERE, acks_late=True)\n"
            "def by_constant() -> None:\n    ...\n"
        )
        names, unresolved = self._shared_task_names(planted)
        assert names == {"baskfy.twt.sweep_by_literal", "baskfy.twt.sweep_by_constant"}
        assert unresolved == set()

    def test_the_task_scan_reports_a_name_it_cannot_read(self) -> None:
        """…and the other half: an expression it cannot resolve is reported, not dropped."""
        planted = (
            "@shared_task(name=some_function(), acks_late=True)\ndef by_call() -> None:\n    ...\n"
        )
        names, unresolved = self._shared_task_names(planted)
        assert names == set()
        assert len(unresolved) == 1

    def test_the_route_scan_would_notice_a_new_one(self) -> None:
        """Non-vacuity: the parser finds a route in source shaped like a real one."""
        tree = ast.parse(
            '@router.post("/twt/liquidate")\nasync def liquidate() -> None:\n    ...\n'
        )
        node = tree.body[0]
        assert isinstance(node, ast.AsyncFunctionDef)
        deco = node.decorator_list[0]
        assert isinstance(deco, ast.Call)
        assert isinstance(deco.func, ast.Attribute)
        assert deco.func.attr == "post"
        assert isinstance(deco.args[0], ast.Constant)
        assert str(deco.args[0].value) == "/twt/liquidate"

    def test_no_twt_task_is_scheduled_into_an_execute_path(self) -> None:
        """``02`` Track C: a task may plan. Nothing scheduled may confirm.

        The ratchet is the line that would most like to be a job. It is not one, and this is the
        assertion that keeps it from becoming one quietly.
        """
        source = _strip_prose(
            _source(
                BLUEPRINT
                / "services"
                / "worker"
                / "src"
                / "baskfy_worker"
                / "tasks"
                / "celery_tasks.py"
            )
        )
        for forbidden in ("execute_line", "rearm_gtt", "sweep_naked", "build_twt_gateway"):
            assert forbidden not in source, (
                f"{forbidden} is reachable from the Celery task registry — a scheduled path "
                f"that can confirm is non-negotiable 1's exception being widened"
            )


# =========================================================================================
# G1 part two — every mutating path, driven, with DRY_RUN false
# =========================================================================================


class TestNoTwtPathReachesABrokerWithDryRunFalse:
    """Every executable line kind and every mutating route helper, with the flag false."""

    @pytest.mark.parametrize("kind", sorted(X.EXECUTABLE_KINDS))
    def test_no_executable_line_kind_reaches_the_adapter(self, kind: str) -> None:
        """Each kind is driven into a state that genuinely **reaches** the gateway.

        The tape assertion at the end is not decoration. The first version of this test set a
        ``RAISE_GTT_STOP`` whose new stop equalled the resting trigger; ``04`` §7.2 refuses that
        before any gateway call, so the case passed while proving nothing — and it kept passing
        when the flag was mutated to true, which is how it was caught. A path that never got to
        the gateway cannot tell you what the gateway would have answered.
        """
        gateway, kc = _spied()
        store, plan_id, line_id = _a_line_that_reaches_the_gateway(kind)
        outcome = run(
            X.execute_line(
                store,
                gateway,
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=Decimal("130.00"),
            )
        )
        assert kc.calls == [], f"{kind} reached a broker: {kc.calls}"
        assert outcome is not None
        assert gateway.tape, (
            f"{kind} never reached the gateway, so this case asserts nothing — "
            f"outcome was {getattr(outcome, 'status', outcome)!r}"
        )
        assert all(status.startswith("DRY_RUN") for _call, status in gateway.tape)

    def test_the_rearm_route_reaches_no_broker(self) -> None:
        gateway, kc = _spied()
        store, _plan_id, _line_id = a_store("ARM_GTT")
        position_id = a_position(store, gtt_id=None)
        run(
            X.rearm_gtt(
                store,
                gateway,
                position_id=position_id,
                confirm="true",
                now=NOW,
                last_price=Decimal("100.00"),
            )
        )
        assert kc.calls == []

    def test_the_sweep_route_reaches_no_broker(self) -> None:
        gateway, kc = _spied()
        store, _plan_id, _line_id = a_store("ARM_GTT")
        a_position(store, gtt_id=None)
        run(
            X.sweep_naked(
                store,
                gateway,
                now=NOW.replace(hour=15, minute=15),
                prices={"TWTCO": Decimal("100.00")},
            )
        )
        assert kc.calls == []

    def test_the_reconcile_route_reaches_no_broker(self) -> None:
        _gateway, kc = _spied()
        store, _plan_id, _line_id = a_store("ARM_GTT")
        a_position(store, gtt_id=None)
        X.reconcile_gtts(store, None, now=NOW)
        assert kc.calls == []

    def test_the_halt_route_reaches_no_broker_and_removes_no_stop(self) -> None:
        _gateway, kc = _spied()
        store, _plan_id, _line_id = a_store("BUY_AT_OPEN")
        position_id = a_position(store, gtt_id="551234")
        X.halt_sleeve(store, confirm="true", now=NOW)
        assert kc.calls == []
        assert store.positions[position_id]["gtt_id"] == "551234", (
            "the halt removed protection — non-negotiable 4 says it must never"
        )

    def test_the_spy_was_actually_exercised(self) -> None:
        """Non-vacuity: the gateway really was driven, and answered a dry-run status."""
        gateway, kc = _spied()
        store, plan_id, line_id = a_store("BUY_AT_OPEN")
        run(
            X.execute_line(
                store,
                gateway,
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=Decimal("100.00"),
            )
        )
        assert gateway.tape, "the gateway was never called — this test proves nothing"
        assert all(status.startswith("DRY_RUN") for _call, status in gateway.tape)
        assert kc.calls == []


class TestTheScanRoutesAreMoneyFree:
    """TW12's two routes, driven for real with ``DRY_RUN`` **False** and the sleeve flag false.

    **The assertion here is stronger than the one every other route in this file can make.** A
    confirm path is allowed to reach the gateway and must get a ``DRY_RUN`` answer back; that is
    what :class:`SpyGateway` checks. A *scan* is allowed to reach the gateway **never** — it
    inserts one row and asks a worker to run the detectors — so the assertion is that
    ``gateway.tape`` is empty as well as ``kc.calls``. A scan that produced a dry-run order would
    pass the weaker test and be a catastrophe.

    ``twt_gateway`` is replaced by a function that fails the test if it is called at all, so the
    route cannot even *ask* for a broker connection. That is the difference between "did not place
    an order this time" and "has no path to one".
    """

    @staticmethod
    def _desk() -> ModuleType:
        """``app.twt_desk``, as a module rather than as ``object``.

        House rule 3 forbids an ``Any`` annotation and there is no ``type: ignore`` here either;
        ``ModuleType`` is the honest type of an imported module and typeshed gives it a
        ``__getattr__``, so the route functions below are reachable without either escape hatch.

        ``import_module`` rather than ``from app import twt_desk``: the desk tree is not typed
        from here, so the plain import is ``Any`` and returning it would be the very escape hatch
        this method exists to avoid. ``importlib.import_module`` is declared to return
        ``ModuleType``, which is what it is.
        """
        import importlib  # noqa: PLC0415 - only this class needs it

        return importlib.import_module("app.twt_desk")

    def _store(self) -> object:
        """A ``PgTwtStore`` over an in-memory sqlite carrying only ``tw_scan_run``.

        Only that table on purpose: if the route touched anything else — a position, a plan, a
        config row — this store would raise rather than quietly serve a default, and the test
        would say which table it reached for.
        """
        import sqlite3  # noqa: PLC0415 - only this class needs it

        sqlite3.register_adapter(dt.datetime, lambda value: value.isoformat())
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE tw_scan_run (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
              requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
              session_date TEXT, status TEXT NOT NULL DEFAULT 'QUEUED',
              source TEXT NOT NULL DEFAULT 'desk', detail TEXT, error TEXT, task_id TEXT);
            """
        )
        return self._desk().PgTwtStore(conn, user_id=1, schema="")

    def _driven(self, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, SpyGateway, CountingKC]:
        import contextlib  # noqa: PLC0415 - only this class needs it

        desk = self._desk()
        gateway, kc = _spied()
        store = self._store()
        monkeypatch.setattr(desk, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(desk, "_now", lambda: NOW)

        def _no_gateway() -> object:
            raise AssertionError(
                "a scan route asked for a broker gateway — a scan is money-free by construction"
            )

        monkeypatch.setattr(desk, "twt_gateway", _no_gateway)
        monkeypatch.setattr(
            desk,
            "last_price",
            lambda _symbol: (_ for _ in ()).throw(
                AssertionError("a scan route read a broker quote")
            ),
        )
        return desk, gateway, kc

    def test_posting_a_scan_reaches_no_broker_and_no_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        desk, gateway, kc = self._driven(monkeypatch)
        answer = desk.twt_scan_now()
        assert answer["status"] == "QUEUED"
        assert int(answer["run_id"]) >= 1
        assert kc.calls == [], f"the scan reached a broker: {kc.calls}"
        assert gateway.tape == [], f"the scan reached the gateway: {gateway.tape}"

    def test_reading_a_scan_reaches_no_broker_and_no_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        desk, gateway, kc = self._driven(monkeypatch)
        run_id = int(desk.twt_scan_now()["run_id"])
        run = desk.twt_scan_status(run_id)
        assert run["id"] == run_id
        assert run["status"] == "QUEUED"
        assert kc.calls == []
        assert gateway.tape == []

    def test_the_second_press_is_refused_and_still_reaches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 409 path, which is the one that runs most often once somebody starts pressing."""
        from fastapi import HTTPException  # noqa: PLC0415 - only this test needs it

        desk, gateway, kc = self._driven(monkeypatch)
        desk.twt_scan_now()
        with pytest.raises(HTTPException) as refused:
            desk.twt_scan_now()
        assert refused.value.status_code == 409
        assert kc.calls == []
        assert gateway.tape == []

    def test_the_scan_wrote_one_row_and_only_to_its_own_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity, and the money half of it.

        Without this the three tests above would pass against a route that did nothing at all.
        One ``tw_scan_run`` row exists afterwards, ``QUEUED``, with no ``task_id`` — the desk has
        no Celery client, so the sweep is what publishes it — and the only table in the database
        is that one, so nothing else was written because nothing else *could* be.
        """
        desk, _gateway, _kc = self._driven(monkeypatch)
        desk.twt_scan_now()
        with desk.open_store() as store:
            rows = store.conn.execute("SELECT status, source, task_id FROM tw_scan_run").fetchall()
            tables = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert len(rows) == 1
        assert rows[0]["status"] == "QUEUED"
        assert rows[0]["source"] == "desk"
        assert rows[0]["task_id"] is None
        assert tables == {"tw_scan_run", "sqlite_sequence"}

    def test_the_scan_module_names_no_broker_and_no_execution_package(self) -> None:
        """The static half: the worker task the row queues cannot reach an order either.

        Prose is stripped first — ``twt_scan.py`` *describes* what it must not do, and a
        prohibition must not trip the check (the rule ``tools/deploy/verify-safety.sh`` learned
        for VBT-1).
        """
        source = _strip_prose(
            _source(
                BLUEPRINT
                / "services"
                / "worker"
                / "src"
                / "baskfy_worker"
                / "tasks"
                / "twt_scan.py"
            )
        )
        for forbidden in (
            "place_order",
            "place_gtt",
            "OrderGateway",
            "build_twt_gateway",
            "execute_line",
            "rearm_gtt",
            "sweep_naked",
            "KiteConnect",
        ):
            assert forbidden not in source, (
                f"baskfy_worker.tasks.twt_scan names {forbidden} — the task a scan queues must "
                f"have no path to an order"
            )

    def test_the_scan_task_calls_the_detector_that_already_exists(self) -> None:
        """TW12.2: one detector, not two.

        A second implementation of the tight-state rule would drift from the first the moment a
        threshold moved, and the two would then disagree about a session on the same page.
        """
        source = _strip_prose(
            _source(
                BLUEPRINT
                / "services"
                / "worker"
                / "src"
                / "baskfy_worker"
                / "tasks"
                / "twt_scan.py"
            )
        )
        assert "from baskfy_worker.tasks.twt import detect_session" in source
        assert "detect_session(" in source
        for reimplemented in ("run_detect_twt", "def detect_session", "tight_state"):
            assert reimplemented not in source, (
                f"twt_scan.py contains {reimplemented} — it should call the nightly's detector, "
                f"not carry one"
            )


class TestTheFlagIsTheReasonAndNotDryRun:
    """The property above must hold *because of the flag*, which these pin exactly."""

    def test_the_gates_are_dry_run_with_the_desk_live_and_the_flag_false(self) -> None:
        assert C.DRY_RUN is False
        assert C.TWT_EXECUTION_ENABLED is False
        assert X.twt_gates().dry_run is True
        assert X.is_simulated() is True

    def test_the_gates_stop_being_dry_run_only_when_the_flag_is_flipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one test that sets the flag true, and it asserts no order — only the gate.

        Without it, ``dry_run=True`` could be hardcoded and every test above would still pass.
        """
        monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", True)
        assert X.twt_gates().dry_run is False

    def test_the_sleeve_is_cnc_only_whatever_the_desk_allows(self) -> None:
        gates = X.twt_gates()
        assert gates.intraday_enabled is False
        assert gates.options_enabled is False

    def test_the_flag_defaults_to_false_in_the_desk(self) -> None:
        source = _strip_prose(_source(DESK / "app" / "config.py"))
        assert 'os.getenv("BASKFY_TWT_EXECUTION_ENABLED", "false")' in source


# =========================================================================================
# G2 — there is no auto-execute for this sleeve, and the absence is asserted
# =========================================================================================


class TestThereIsNoAutoExecute:
    """``BASKFY_TWT_AUTO``-anything does not exist as a *setting*, anywhere in either tree.

    A plain ``grep | wc -l == 0`` cannot express this and never could: the tree deliberately
    contains the string inside prohibitions (``app/config.py`` and ``.env.example`` both carry
    "THERE IS NO BASKFY_TWT_AUTO_EXECUTE AND THERE WILL NOT BE ONE"), inside this docstring, and
    inside ``docs/twt``. ``tools/deploy/verify-safety.sh`` hit the same problem for VBT-1 and
    recorded it in a comment: *a prohibition must not trip the check*. So prose is stripped and
    what remains is scanned for a setting.
    """

    def _sources(self) -> list[Path]:
        paths = [DESK / "app" / name for name in DESK_TWT_SOURCES]
        paths.append(DESK / "app" / "config.py")
        paths.append(DESK / "app" / "main.py")
        worker = BLUEPRINT / "services" / "worker" / "src" / "baskfy_worker"
        paths.extend(sorted(worker.glob("tasks/twt*.py")))
        paths.append(worker / "tasks" / "celery_tasks.py")
        paths.append(worker / "celery_app.py")
        api = BLUEPRINT / "services" / "api" / "src" / "baskfy_api"
        paths.append(api / "twt_sleeve.py")
        # TW12's two API modules. Added the day they were written: this scan is a list of files,
        # and a list of files is exactly the thing that stops being complete when somebody adds a
        # module without remembering it.
        paths.append(api / "twt_scan.py")
        paths.append(api / "routers" / "twt.py")
        paths.extend(
            sorted((BLUEPRINT / "packages" / "core" / "src" / "baskfy_core" / "twt").glob("*.py"))
        )
        return [p for p in paths if p.exists()]

    def test_the_scan_reads_a_real_and_non_empty_set_of_files(self) -> None:
        """Non-vacuity: a scan over nothing would pass the test below for the wrong reason."""
        sources = self._sources()
        assert len(sources) >= 18, f"only {len(sources)} files scanned"
        assert any(p.name == "twt_execute.py" for p in sources)
        assert any(p.name == "celery_tasks.py" for p in sources)
        assert any(p.name == "twt_scan.py" and "worker" in str(p) for p in sources)
        assert any(p.name == "twt_scan.py" and "api" in str(p) for p in sources)

    def test_no_source_defines_or_reads_a_twt_auto_execute_flag(self) -> None:
        offenders: list[str] = []
        for path in self._sources():
            for line in _strip_prose(_source(path)).splitlines():
                if re.search(r"BASKFY_TWT_AUTO", line):
                    offenders.append(f"{path.relative_to(REPO)}: {line.strip()}")
        assert offenders == [], "an auto-execute setting exists for TWT:\n" + "\n".join(offenders)

    def test_the_scan_would_catch_a_real_one(self) -> None:
        """The scan is not vacuous: a planted setting survives prose-stripping and is found."""
        planted = (
            '"""A docstring mentioning BASKFY_TWT_AUTO_EXECUTE, which is prose."""\n'
            "# A comment mentioning BASKFY_TWT_AUTO_EXECUTE, which is prose.\n"
            'AUTO = os.getenv("BASKFY_TWT_AUTO_EXECUTE", "false") == "true"\n'
        )
        remaining = [
            line
            for line in _strip_prose(planted).splitlines()
            if re.search(r"BASKFY_TWT_AUTO", line)
        ]
        assert len(remaining) == 1, remaining
        assert "os.getenv" in remaining[0]

    def test_the_prohibition_is_where_somebody_would_look_for_the_setting(self) -> None:
        for path in (DESK / "app" / "config.py", DESK / ".env.example"):
            assert "THERE IS NO BASKFY_TWT_AUTO_EXECUTE" in _source(path), path

    def test_non_negotiable_ones_exception_is_still_the_swing_sleeves_alone(self) -> None:
        """The swing sleeve has an auto-execute flag. It is the only sleeve that does."""
        config = _strip_prose(_source(DESK / "app" / "config.py"))
        assert "BASKFY_SWING_AUTO_EXECUTE" in config
        for sleeve in ("TWT", "VBT"):
            assert f"BASKFY_{sleeve}_AUTO_EXECUTE" not in config, (
                f"{sleeve} grew an auto-execute flag — non-negotiable 1 names one exception"
            )
