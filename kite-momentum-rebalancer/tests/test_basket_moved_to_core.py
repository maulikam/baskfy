"""Plan construction moved to `baskfy_core.basket` at M15, and did not change (P3.2).

M15's acceptance is "same plans on the corpus". That was verified directly at the time by running
the pre-move file out of git alongside the moved one over both scans at three cash levels,
including a holding the desk must never touch: every plan identical.

What that comparison cannot check afterwards is the thing this refactor could actually have broken.
Making the untouchable-instrument guard an argument means a caller could pass a permissive one, so
the tests below pin the guard down rather than the arithmetic.
"""
from __future__ import annotations

import inspect
import pathlib

import pandas as pd
import pytest

from app import config as C
from app import rebalance


def test_core_requires_the_guard_and_will_not_default_it() -> None:
    """`tradeable` has no default, so forgetting it is a TypeError and not a silent hole."""
    import baskfy_core.basket as basket

    sig = inspect.signature(basket.build_plan)
    assert sig.parameters["tradeable"].default is inspect.Parameter.empty
    assert sig.parameters["tradeable"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_desk_passes_the_real_guard_not_a_stand_in() -> None:
    """The desk's wrapper must bind core.guards.assert_tradeable, the same check the gateway uses."""
    src = pathlib.Path(inspect.getsourcefile(rebalance)).read_text()
    assert "tradeable=_tradeable" in src
    assert "from .core.guards import UntouchableInstrumentError, assert_tradeable" in src

    from app.core.guards import UntouchableInstrumentError, assert_tradeable

    # And it behaves like the gateway's: prefix-aware, not an exact-match set.
    assert rebalance._tradeable("RELIANCE") is True
    assert rebalance._tradeable("SGBDE31III-GB") is False
    with pytest.raises(UntouchableInstrumentError):
        assert_tradeable("SGBDE31III-GB")


def test_the_planner_still_refuses_an_untouchable_after_the_move() -> None:
    """The scar this guard came from: build_plan once proposed EXIT SGBDE31III-GB -392."""
    scored = pd.DataFrame(
        [{"symbol": "RELIANCE", "close": 1000.0, "ma_20": 900.0, "rank": 1.0, "SCORE": 80.0,
          "reject": "", "volatility_one_year": 0.3, "rsi_one_month": 60.0,
          "median_volume_one_year": 1e9, "marketcap": 500000.0}]
    )
    holdings = [{"symbol": "SGBDE31III-GB", "quantity": 392, "pledged_qty": 0,
                 "last_price": 7412.0, "average_price": 6000.0}]
    plan = rebalance.build_plan(scored, holdings, 100000.0, live_prices={"RELIANCE": 1000.0})
    for order in plan.get("orders", []):
        assert "SGBDE31III" not in order["symbol"], "the planner proposed an untouchable again"


def test_the_sectors_read_stayed_at_the_desk() -> None:
    """Reading a file is a boundary; core takes the mapping as an argument (docs/04 §2).

    Checked against the parsed CODE, not the source text: core's docstring explains the move and
    therefore mentions `read_csv` and `sectors.csv` on purpose, so a grep would fail on the
    explanation while a real read hid in a helper.
    """
    import ast

    import baskfy_core.basket as basket

    tree = ast.parse(pathlib.Path(inspect.getsourcefile(basket)).read_text())
    # Every docstring in the file, not just the module's: build_plan's own docstring explains
    # where the sector mapping now comes from, and that explanation is the point.
    # clean=False matters -- get_docstring dedents by default, and the dedented text
    # then never matches the raw Constant it came from.
    docstrings = {
        ast.get_docstring(node, clean=False) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "read_csv" not in called, "core reads a file; that belongs at the desk"

    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - docstrings
    assert not any("sectors.csv" in text for text in literals), (
        "core names a file path outside its docstring"
    )

    # The desk keeps the read, and the path.
    desk_src = pathlib.Path(inspect.getsourcefile(rebalance)).read_text()
    assert "read_csv" in desk_src and "sectors.csv" in desk_src
