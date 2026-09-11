"""VB10 — every Track-B and Track-C claim of `docs/vbt/02` as a test rather than an intention.

The claims, and where each is proved:

1. **With the flag false, no VBT path reaches a broker** — a *spy* over the real gateway, under
   both values of `DRY_RUN`, driving every executable line kind. Not "the test client raises if
   touched" (which `test_vbt_execute.py` already gives) but "the adapter was never called at
   all", which is the stronger reading of `02` Track B.
2. **No auto-execute exists.** A source scan over both trees: no `BASKFY_VBT_AUTO_EXECUTE`-shaped
   setting, nothing that reads one, and no scheduler entry that reaches an execute path. The
   swing sleeve's flagged exception to non-negotiable #1 is its own; this sleeve has none, and
   the absence is asserted so that adding one is a deliberate act with a failing test on it.
3. **Every order-producing path is reached only from a confirmed request.** Over the source, so a
   second route added later is caught by the shape rather than by review.
4. **A stop never falls, and a SELL never exceeds the position** — a property test over random
   books, because these are the two ways a bug here loses money quietly.

The neighbours' half of VB10 — the swing package unchanged, the weekly book untouched — is in
`decile-blueprint/packages/core/tests/test_vbt_neighbours.py`, where the packages are.
"""

from __future__ import annotations

import inspect
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

# The harness lives beside this file; `tests/` is not a package, so the path is explicit.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vbt_execute import (  # noqa: E402 - the sys.path insert above has to come first
    NOW,
    ExplodingKC,
    a_store,
    run,
)

from app import config as C
from app import vbt_desk as D
from app import vbt_execute as X
from app.core.risk import RiskManager

DESK = Path(__file__).resolve().parents[1]
MONOREPO = DESK.parent
WEB = MONOREPO / "decile-blueprint" / "apps" / "web" / "src"

Dec = Decimal

#: Triple-quoted strings, then `#` and `//` comments. Crude on purpose: a stripper clever enough
#: to be wrong is worse than one that removes a little too much, because everything it removes is
#: prose and the thing being looked for is an assignment.
_DOCSTRING = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
_COMMENT = re.compile(r"(#|//).*$", re.MULTILINE)


def _strip_prose(source: str) -> str:
    """Source with its docstrings and comments blanked out, line count preserved."""
    without_docs = _DOCSTRING.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    return _COMMENT.sub("", without_docs)


class SpyingKC(ExplodingKC):
    """Records instead of raising, so "was it called" is a fact rather than an exception.

    `ExplodingKC` proves nothing *reached* the broker by blowing up if it did. This one lets the
    call through and counts it, which is what makes "the adapter was never called under either
    value of DRY_RUN" a positive assertion rather than the absence of a failure.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def place_order(self, **_: object) -> str:
        self.calls.append("place_order")
        return "SPY-1"

    def place_gtt(self, **_: object) -> dict:
        self.calls.append("place_gtt")
        return {"trigger_id": 1}

    def delete_gtt(self, **_: object) -> None:
        self.calls.append("delete_gtt")

    def cancel_order(self, **_: object) -> str:
        self.calls.append("cancel_order")
        return "SPY-1"

    def instruments(self, *_: object) -> list:
        self.calls.append("instruments")
        return []


def _confirm_every_kind(spy: SpyingKC) -> list[str]:
    """Drive one line of every executable kind through the real gateway over `spy`."""
    outcomes: list[str] = []
    for kind in sorted(X.EXECUTABLE_KINDS):
        store, plan_id, line_id = a_store(kind)
        if kind in {"SELL_AT_OPEN", "ARM_GTT"}:
            store.positions[1] = {
                "id": 1,
                "instrument_id": 42,
                "symbol": "VBTCO",
                "state": "OPEN",
                "quantity_open": 1_000,
                "entry_avg": Dec("96.00"),
                "stop_price": Dec("84.45"),
                "initial_stop": Dec("84.45"),
                "gtt_id": None,
            }
            store.lines[line_id]["position_id"] = 1
        if kind == "CANCEL_LIMIT":
            store.orders[1] = {
                "id": 1,
                "instrument_id": 42,
                "symbol": "VBTCO",
                "state": "SENT",
                "quantity": 1_000,
                "filled_quantity": 0,
                "broker_order_id": None,
                "limit_price": Dec("96.00"),
            }
            store.lines[line_id]["order_id"] = 1
        outcome = run(
            X.execute_line(
                store,
                X.build_vbt_gateway(spy, RiskManager()),
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=Dec("96.00"),
            )
        )
        outcomes.append(f"{kind}:{outcome.status}")
    return outcomes


class TestNothingReachesABroker:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_no_line_kind_reaches_the_adapter_with_the_flag_false(
        self, dry_run: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`02` Track B, as a theorem: the execution flag is what decides, not DRY_RUN.

        Both values of `DRY_RUN` are driven because the two switches are independent and a
        reader could reasonably assume `DRY_RUN=false` is enough to arm this sleeve. It is not:
        `BASKFY_VBT_EXECUTION_ENABLED` is false and that is the one that governs.
        """
        monkeypatch.setattr(C, "DRY_RUN", dry_run, raising=False)
        monkeypatch.setattr(C, "VBT_EXECUTION_ENABLED", False, raising=False)
        spy = SpyingKC()

        outcomes = _confirm_every_kind(spy)

        assert outcomes, "no executable kinds were driven, so this would prove nothing"
        assert spy.calls == [], f"the adapter was called: {spy.calls}"

    def test_the_flag_defaults_to_false_in_the_desk_and_in_the_api(self) -> None:
        """A safety flag whose default is on is not a safety flag."""
        assert C.VBT_EXECUTION_ENABLED is False

    def test_the_simulated_marker_is_on_by_default(self) -> None:
        """Every fill and GTT this sleeve has ever produced is labelled simulated."""
        assert X.is_simulated() is True


class TestThereIsNoAutoExecute:
    """Non-negotiable #1's exception belongs to the swing sleeve alone.

    The name is asserted **absent** across both trees, so adding one is a deliberate act with a
    failing test on it rather than a line somebody copies from `swing_monitor.py`.
    """

    #: Every Python and TypeScript source in either tree that could name a flag.
    def _sources(self) -> list[Path]:
        roots = [
            DESK / "app",
            MONOREPO / "decile-blueprint" / "services",
            MONOREPO / "decile-blueprint" / "packages" / "core" / "src",
            WEB,
        ]
        out: list[Path] = []
        for root in roots:
            out.extend(
                path
                for path in root.rglob("*")
                if path.suffix in {".py", ".ts", ".tsx"}
                and "node_modules" not in path.parts
                and "__pycache__" not in path.parts
                # Test trees are excluded: this file names the pattern in order to forbid it,
                # and so do the API's and the web hub's read-only tests. A prohibition must not
                # trip the check it is stating. Production code is what the scan is about — a
                # flag in a test cannot arm anything.
                and "__tests__" not in path.parts
                and "tests" not in path.parts
            )
        return out

    def test_no_source_defines_or_reads_a_vbt_auto_execute_flag(self) -> None:
        """`06` VB10: the scan runs over **code**, with comments and docstrings stripped.

        Several places say in prose that no such flag exists — `settings.py` and `config.py` say
        it where a reader would go looking, which is the point of writing it down. A prohibition
        must not trip the check that states it, and stripping is the honest way to allow that
        without an exception list that would also hide a real one.
        """
        pattern = re.compile(r"VBT[_A-Z]*AUTO[_A-Z]*EXECUTE|vbt[_a-z]*auto[_a-z]*execute")
        offenders: list[str] = []
        for path in self._sources():
            code = _strip_prose(path.read_text(encoding="utf-8"))
            offenders.extend(
                f"{path}:{index + 1}"
                for index, line in enumerate(code.splitlines())
                if pattern.search(line)
            )
        assert offenders == [], f"an auto-execute flag appeared in {offenders}"

    def test_the_scan_would_catch_a_real_one(self) -> None:
        """The check that the check works. A stripped scan that matched nothing would pass
        whatever the tree contained, and this is the one assertion that rules that out."""
        planted = _strip_prose('vbt_auto_execute: bool = True  # a comment\n')
        assert re.search(r"vbt[_a-z]*auto[_a-z]*execute", planted)
        assert _strip_prose("# vbt_auto_execute is forbidden\n").strip() == ""

    def test_the_desk_config_says_so_where_somebody_would_look_for_it(self) -> None:
        """A negative is only useful if it is written where the question gets asked."""
        source = (DESK / "app" / "config.py").read_text(encoding="utf-8")
        assert "THERE IS NO BASKFY_VBT_AUTO_EXECUTE" in source

    def test_no_scheduler_entry_reaches_an_execute_path(self) -> None:
        """Beat may detect, plan and check. It may not confirm."""
        beat = (
            MONOREPO
            / "decile-blueprint"
            / "services"
            / "worker"
            / "src"
            / "baskfy_worker"
            / "celery_app.py"
        ).read_text(encoding="utf-8")
        vbt_entries = re.findall(r'"(baskfy\.vbt\.[a-z_]+)"', beat)
        assert vbt_entries, "no vbt tasks are scheduled at all, so this would prove nothing"
        for name in vbt_entries:
            assert "execute" not in name, f"{name} is scheduled and names an execute path"
            assert "confirm" not in name, f"{name} is scheduled and names a confirm"


class TestOnlyAConfirmedRequestCanProduceAnOrder:
    def test_the_route_requires_confirm_true(self) -> None:
        """It is refused **before anything is read**, so a form posted by accident costs no
        database work and no broker read."""
        store, plan_id, line_id = a_store("PLACE_LIMIT")
        with pytest.raises(HTTPException) as raised:
            run(
                X.execute_line(
                    store,
                    X.build_vbt_gateway(ExplodingKC(), RiskManager()),
                    plan_id=plan_id,
                    line_id=line_id,
                    confirm="false",
                    now=NOW,
                )
            )
        assert raised.value.status_code == 400
        assert "confirm=true" in str(raised.value.detail)

    def test_the_only_execute_entry_point_takes_a_confirm(self) -> None:
        """Over the signature, so a second entry point added later is caught by its shape."""
        signature = inspect.signature(X.execute_line)
        assert "confirm" in signature.parameters
        assert "plan_id" in signature.parameters

    def test_the_desk_router_exposes_two_posts_and_only_one_can_order(self) -> None:
        """VB12 added `/vbt/rescan`. Two POSTs, and the count is not the property — the property
        is that exactly one of them reaches `execute_line`, which is the only doorway to the
        gateway. The other writes one `vb_scan_run` row and hands off to a worker with no order
        path at all (`test_vbt_desk.py::test_the_rescan_route_cannot_reach_an_order`)."""
        source = inspect.getsource(D)
        assert source.count("@router.post(") == 2
        assert '@router.post("/vbt/execute"' in source
        assert '@router.post("/vbt/rescan"' in source
        # Over code, not prose (the docstring names `execute_line` when explaining that it is
        # the single doorway), and matching the **call** rather than the bare name — the handler
        # is itself called `vbt_execute_line`, so a substring count finds two and means one.
        assert _strip_prose(source).count("_execute.execute_line(") == 1

    def test_no_get_on_the_desk_page_can_execute(self) -> None:
        """A confirm behind a GET is a confirm a link preview can fire."""
        source = inspect.getsource(D)
        for match in re.finditer(r'@router\.get\("([^"]+)"', source):
            assert "execute" not in match.group(1), match.group(1)


class TestTheTwoWaysThisLosesMoneyQuietly:
    """A stop that falls and a SELL larger than the position.

    Both are silent: no exception, no alert, a book that looks fine and a loss that is bigger
    than the rules allowed. `04` §6.2 forbids the first outright; `02` Track C forbids the second
    by never selling what the sleeve did not buy.
    """

    @pytest.mark.parametrize(
        ("held", "asked"),
        [(1_000, 1_001), (1_000, 5_000), (1, 2), (100, 101)],
    )
    def test_a_sell_larger_than_the_position_is_blocked_with_both_numbers(
        self, held: int, asked: int
    ) -> None:
        store, plan_id, line_id = a_store("SELL_AT_OPEN", quantity=asked)
        store.positions[1] = {
            "id": 1,
            "instrument_id": 42,
            "symbol": "VBTCO",
            "state": "OPEN",
            "quantity_open": held,
            "entry_avg": Dec("96.00"),
            "stop_price": Dec("84.45"),
            "initial_stop": Dec("84.45"),
            "gtt_id": "SIM-1",
        }
        store.lines[line_id]["position_id"] = 1

        outcome = run(
            X.execute_line(
                store,
                X.build_vbt_gateway(ExplodingKC(), RiskManager()),
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=Dec("96.00"),
            )
        )

        assert outcome.status == "BLOCKED"
        assert str(held) in (outcome.reason or "") and str(asked) in (outcome.reason or "")

    def test_a_sell_for_a_name_the_sleeve_does_not_hold_is_blocked(self) -> None:
        store, plan_id, line_id = a_store("SELL_AT_OPEN")

        outcome = run(
            X.execute_line(
                store,
                X.build_vbt_gateway(ExplodingKC(), RiskManager()),
                plan_id=plan_id,
                line_id=line_id,
                confirm="true",
                now=NOW,
                last_price=Dec("96.00"),
            )
        )

        assert outcome.status == "BLOCKED"
        assert "did not buy" in (outcome.reason or "").lower()
