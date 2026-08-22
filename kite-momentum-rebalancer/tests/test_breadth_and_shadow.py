"""M14 — the breadth input, the flag, and the harness that earns the flag.

The desk's cash bands key off "percent of the universe above its 20-day moving average". That
number was computed privately from whatever CSV was uploaded; the pipeline now computes the same
figure over a named index universe. These tests are about the one thing that makes that a *wiring*
change rather than a strategy change: the number must be measured over the same population, and
when it is not, the desk must notice rather than average two different markets together.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from app import breadth_source as B
from app import config as C


# =====================================================================================
# which number the cash bands see
# =====================================================================================
def test_the_pipeline_wins_when_the_population_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(B, "pipeline_breadth", lambda *_: (68.6347, {"A", "B"}))
    result = B.breadth_for_plan(68.6, {"A", "B"}, dt.date(2026, 8, 18))

    assert result["source"] == "pipeline"
    assert result["value"] == 68.6347
    assert result["scan_value"] == 68.6          # both are kept, always
    assert "same 2 symbols" in result["reason"]


def test_a_different_population_falls_back_to_the_scans_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breadth number about a different set of stocks is not more authoritative. It is wrong."""
    monkeypatch.setattr(B, "pipeline_breadth", lambda *_: (12.0, {"X", "Y"}))
    result = B.breadth_for_plan(68.6, {"A", "B"}, dt.date(2026, 8, 18))

    assert result["source"] == "scan"
    assert result["value"] == 68.6
    assert result["pipeline_value"] == 12.0      # recorded, and deliberately not used
    assert "differs from" in result["reason"]


def test_a_missing_pipeline_row_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(B, "pipeline_breadth", lambda *_: (None, {"A"}))
    result = B.breadth_for_plan(68.6, {"A"}, dt.date(2020, 1, 2))
    assert result["source"] == "scan"
    assert "no market_health_daily row" in result["reason"]


def test_an_unreachable_screener_never_stops_a_rebalance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desk trades on Monday whether or not a Postgres container is up."""

    def boom(*_: object) -> tuple[float | None, set[str]]:
        raise OSError("connection refused")

    monkeypatch.setattr(B, "pipeline_breadth", boom)
    result = B.breadth_for_plan(68.6, {"A"}, dt.date(2026, 8, 18))

    assert result["source"] == "scan"
    assert result["value"] == 68.6
    assert "unreachable" in result["reason"]


def test_both_numbers_are_always_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whichever is used, the other is kept — that is what makes weekly divergence visible."""
    monkeypatch.setattr(B, "pipeline_breadth", lambda *_: (50.0, {"A"}))
    for symbols in ({"A"}, {"Z"}):
        result = B.breadth_for_plan(68.6, symbols, dt.date(2026, 8, 18))
        assert result["scan_value"] == 68.6
        assert result["pipeline_value"] == 50.0


# =====================================================================================
# the band table itself did not move
# =====================================================================================
def test_repointing_the_input_did_not_re_tune_the_bands() -> None:
    """M14 §1 repoints the breadth input. The cash bands are a strategy parameter and are frozen.

    Re-expressing them against the pipeline's 50-day figure would have been a strategy change
    wearing a wiring change's clothes, which is why `pct_above_20dma` was added to the pipeline
    instead. If these numbers ever move, it must be somebody's deliberate decision.
    """
    assert C.CASH_BANDS == [(65.0, 5.0), (45.0, 15.0), (0.0, 35.0)]


def test_the_two_breadth_numbers_land_in_the_same_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole exercise: repointing the input changed no decision.

    68.6 (the desk's rounded figure) and 68.6347 (the pipeline's) are different numbers and the
    same cash band. If a future reconciliation puts them either side of a boundary, that is a
    behaviour change and has to be argued for rather than absorbed.
    """
    from baskfy_core.basket import cash_pct_for  # noqa: PLC0415

    class _NotFullyInvested:
        FULLY_INVESTED = False
        CASH_BANDS = C.CASH_BANDS

    assert cash_pct_for(68.6, _NotFullyInvested) == cash_pct_for(68.6347, _NotFullyInvested) == 5.0


def test_todays_settings_short_circuit_the_band_entirely() -> None:
    """And the honest part: on this desk, right now, breadth decides nothing.

    `FULLY_INVESTED` is on, and `cash_pct_for` returns 0% before it ever looks at the bands. So
    M14's wiring is correct, reconciled and — today — inert. It becomes load-bearing the moment
    anyone turns `FULLY_INVESTED` off, which is exactly when you want the input to already be
    right rather than to be repointing it under pressure.
    """
    from app.rebalance import _cash_pct  # noqa: PLC0415

    assert C.FULLY_INVESTED is True
    assert _cash_pct(68.6) == _cash_pct(20.0) == 0.0


def test_the_flag_is_off() -> None:
    """Four green Fridays buy this flag. Nothing else does."""
    assert C.SCAN_SOURCE_DEFAULT == "upload"


def test_the_protocol_is_written_down() -> None:
    doc = Path(__file__).resolve().parent.parent / "docs" / "SHADOW-MODE.md"
    text = doc.read_text(encoding="utf-8")
    assert "four consecutive green fridays" in text.lower()
    assert "SCAN_SOURCE_DEFAULT=generated" in text
    assert "restarts" in text          # a red week resets the counter


# =====================================================================================
# the harness
# =====================================================================================
def test_the_diff_is_at_order_level_not_score_level() -> None:
    """Symbol, side and quantity. A score that moves a tenth changes nothing tradeable."""
    from scripts.shadow_mode import _diff, _orders  # noqa: PLC0415

    left = _orders({"orders": [{"symbol": "A", "action": "BUY", "delta": 10}]})
    right = _orders({"orders": [{"symbol": "A", "action": "BUY", "delta": 10}]})
    assert _diff(left, right) == []


@pytest.mark.parametrize(
    ("right_order", "why"),
    [
        ({"symbol": "A", "action": "BUY", "delta": 11}, "one share is still a different order"),
        ({"symbol": "A", "action": "SELL", "delta": 10}, "the side changed"),
        ({"symbol": "B", "action": "BUY", "delta": 10}, "a different name entirely"),
    ],
)
def test_nothing_is_small_enough_to_ignore(right_order: dict, why: str) -> None:
    from scripts.shadow_mode import _diff, _orders  # noqa: PLC0415

    left = _orders({"orders": [{"symbol": "A", "action": "BUY", "delta": 10}]})
    right = _orders({"orders": [right_order]})
    assert _diff(left, right), why


def test_a_zero_delta_is_not_an_order() -> None:
    """`build_plan` emits held names with delta 0. They are not instructions to a broker."""
    from scripts.shadow_mode import _orders  # noqa: PLC0415

    assert _orders({"orders": [{"symbol": "A", "action": "HOLD", "delta": 0}]}) == {}


def test_the_harness_never_touches_a_broker() -> None:
    """It builds two plans and sends neither. Nothing in it may reach the order path."""
    import inspect  # noqa: PLC0415

    from scripts import shadow_mode  # noqa: PLC0415

    # Names, not prose: an earlier version of this test failed on the word "executed" in a
    # comment, which teaches nobody anything and trains you to weaken the assertion.
    source = inspect.getsource(shadow_mode)
    for forbidden in ("place_order", "OrderGateway", "gateway(", "kite(", "core.gateway"):
        assert forbidden not in source, f"shadow mode must not reference {forbidden}"


def test_the_log_is_one_json_object_per_run() -> None:
    """Append-only JSONL, so a Friday's result cannot overwrite the Friday before it."""
    log = Path(__file__).resolve().parent.parent / "data" / "shadow-mode.jsonl"
    if not log.exists():
        pytest.skip("no shadow-mode run recorded yet")
    for line in log.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert {"as_of", "green", "order_deltas", "screen_run_id"} <= set(row)
