"""`place_gtt_stop(limit_fraction=...)` — the swing book's 3% cushion, additive to the weekly book.

`docs/swing/07` (SW9.5): he uses market stops ("I always use market stops, never limit stops"),
and a GTT fires a LIMIT order. The weekly book rests that limit a half-percent under the trigger
(`GTT_LIMIT_FRACTION`, 0.995); a swing stop is tight and a fast-falling book can walk through
half a percent and leave the stop resting unfilled. So the swing route passes `limit_fraction=0.97`
— the limit rests 3% under the trigger and fills on the way down like the market stop he uses.

Two things are asserted here, and the second matters as much as the first: that 0.97 lands as
the GTT's limit, and that a caller who passes nothing gets exactly the number every existing test
in `test_gtt_gateway.py` pins — the weekly book's GTTs are byte-for-byte unaffected. The fixtures
are that file's, reused rather than retyped, so the two suites arm the same stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from baskfy_execution.gtt import DRY_RUN_GTT, GTT_LIMIT_FRACTION, GTT_PLACED
from test_gtt_gateway import SpyKC, arm, journal, make_gateway

SWING_LIMIT_FRACTION = 0.97


def _leg(kc: SpyKC) -> dict[str, object]:
    ((_, params),) = [c for c in kc.calls if c[0] == "place_gtt"]
    orders = params["orders"]
    assert isinstance(orders, list)
    (leg,) = orders
    assert isinstance(leg, dict)
    return leg


def test_the_swing_fraction_lands_as_the_gtts_limit(tmp_path: Path) -> None:
    """89.00 x 0.97 = 86.33, snapped to the 5-paisa tick -> 86.35; the trigger itself is where
    it was. The limit sits 3% under the trigger, not the weekly book's half-percent."""
    gw, kc = make_gateway(tmp_path)
    out = arm(
        gw,
        symbol="RELIANCE",
        qty=10,
        trigger=89.0,
        last_price=100.0,
        limit_fraction=SWING_LIMIT_FRACTION,
    )
    assert out["status"] == GTT_PLACED
    assert out["trigger"] == 89.0
    assert out["limit"] == 86.35
    leg = _leg(kc)
    assert leg["price"] == 86.35
    assert leg["price"] < out["trigger"]
    (placed,) = [r for r in journal(tmp_path) if r.get("event") == "gtt_placed"]
    assert placed["limit"] == 86.35
    assert placed["limit_fraction"] == SWING_LIMIT_FRACTION


def test_the_swing_fraction_is_snapped_to_a_coarse_tick_too(tmp_path: Path) -> None:
    """OFSS trades in whole rupees: 2515 x 0.97 = 2439.55 -> 2440, a multiple of its tick, or
    Kite rejects the whole trigger."""
    gw, kc = make_gateway(tmp_path)
    out = arm(
        gw,
        symbol="OFSS",
        qty=3,
        trigger=2515.1,
        last_price=2750.0,
        limit_fraction=SWING_LIMIT_FRACTION,
    )
    assert out["status"] == GTT_PLACED
    assert out["trigger"] == 2515.0
    assert out["limit"] == 2440.0
    assert _leg(kc)["price"] == 2440.0


def test_omitting_the_keyword_keeps_the_weekly_books_half_percent(tmp_path: Path) -> None:
    """The default is unchanged: 89.00 x 0.995 = 88.555 -> 88.55, the number
    `test_gtt_gateway.test_a_live_stop_is_a_cnc_sell_limit_just_under_a_tick_snapped_trigger`
    has always asserted. `None` and the constant itself answer the same."""
    assert GTT_LIMIT_FRACTION == 0.995
    for fraction in (None, GTT_LIMIT_FRACTION):
        gw, kc = make_gateway(tmp_path / str(fraction))
        kwargs = {} if fraction is None else {"limit_fraction": fraction}
        out = arm(gw, symbol="RELIANCE", qty=10, trigger=89.0, last_price=100.0, **kwargs)
        assert out["status"] == GTT_PLACED
        assert out["limit"] == 88.55
        assert _leg(kc)["price"] == 88.55
        (placed,) = [r for r in journal(tmp_path / str(fraction)) if r.get("event") == "gtt_placed"]
        assert placed["limit_fraction"] == GTT_LIMIT_FRACTION


def test_a_dry_run_records_the_fraction_it_would_have_used(tmp_path: Path) -> None:
    """The swing book rehearses for twenty sessions in dry run (docs/swing/02 §3.2); the
    rehearsal must say which cushion the live GTT would carry, or it rehearses nothing."""
    gw, kc = make_gateway(tmp_path, dry_run=True)
    out = arm(gw, limit_fraction=SWING_LIMIT_FRACTION)
    assert out["status"] == DRY_RUN_GTT
    assert kc.calls == [], "a dry run reaches no broker"
    (dry,) = [r for r in journal(tmp_path) if r.get("event") == "gtt_dry_run"]
    assert dry["limit_fraction"] == SWING_LIMIT_FRACTION
    gw, _ = make_gateway(tmp_path / "weekly", dry_run=True)
    arm(gw)
    (dry,) = [r for r in journal(tmp_path / "weekly") if r.get("event") == "gtt_dry_run"]
    assert dry["limit_fraction"] == GTT_LIMIT_FRACTION


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.03, 2.0])
def test_a_fraction_that_would_rest_the_limit_above_the_trigger_is_a_caller_bug(
    fraction: float, tmp_path: Path
) -> None:
    """A sell limit above its trigger cannot fill on the way down. That is not a market
    condition to journal and move past; it is a wrong constant, refused before any layer runs
    and before anything is written."""
    gw, kc = make_gateway(tmp_path)
    with pytest.raises(ValueError, match="limit_fraction"):
        arm(gw, limit_fraction=fraction)
    assert kc.calls == []
    assert journal(tmp_path) == []


def test_exactly_one_is_allowed_and_rests_the_limit_on_the_trigger(tmp_path: Path) -> None:
    """The boundary of the range: a limit *at* the trigger is a legal (if optimistic) GTT."""
    gw, kc = make_gateway(tmp_path)
    out = arm(gw, symbol="RELIANCE", qty=10, trigger=89.0, last_price=100.0, limit_fraction=1.0)
    assert out["status"] == GTT_PLACED
    assert out["limit"] == out["trigger"] == 89.0
    assert _leg(kc)["price"] == 89.0
