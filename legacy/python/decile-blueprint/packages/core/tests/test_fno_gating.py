"""``06`` FO6: ``fno_gates()`` is ``PAPER`` for every combination but all-four-true.

``02`` Track B.

The four: ``DRY_RUN=false``, ``OPTIONS_ENABLED``, ``BASKFY_FNO_CARRY_ENABLED`` and the sleeve's
``BASKFY_FNO_<F1|F2>_EXECUTION_ENABLED``. ``INTRADAY_ENABLED`` is not one of them, and the gate
never hands the gateway ``intraday_enabled=True`` (an FO order is NRML, never MIS).
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.fno.gating import (
    CARRY_FLAG_ENV,
    DESK_SWITCH_ENV,
    EXECUTION_FLAG_ENV,
    FnoFlags,
    fno_gates,
)
from baskfy_core.options.config import Mode

ROWS = list(itertools.product((False, True), repeat=4))
_FIELD = {
    FoSleeveGroup.F1: "f1_execution_enabled",
    FoSleeveGroup.F2: "f2_execution_enabled",
    FoSleeveGroup.F3: "f3_execution_enabled",
}


def _flags(sleeve: FoSleeve, dry_off: bool, options: bool, carry: bool, ex: bool) -> FnoFlags:
    return FnoFlags(
        dry_run=not dry_off,
        options_enabled=options,
        fno_carry_enabled=carry,
        **{_FIELD[group_of(sleeve)]: ex},
    )


@pytest.mark.parametrize("sleeve", list(FoSleeve))
@pytest.mark.parametrize(("dry_off", "options", "carry", "execution"), ROWS)
def test_live_only_when_all_four_are_on(
    sleeve: FoSleeve, dry_off: bool, options: bool, carry: bool, execution: bool
) -> None:
    gate = fno_gates(sleeve, _flags(sleeve, dry_off, options, carry, execution))
    all_four = dry_off and options and carry and execution
    assert gate.mode is (Mode.LIVE if all_four else Mode.PAPER)
    assert gate.dry_run is (not all_four)
    assert gate.simulated is (not all_four)
    assert gate.intraday_enabled is False
    assert gate.sleeve is sleeve
    if not execution:
        # With the sleeve flag off the result is simulated regardless of DRY_RUN, and the paper
        # gates let the confirm run the real gateway path (OP2.3's shape).
        assert (gate.options_enabled, gate.fno_carry_enabled) == (True, True)
    elif not all_four:
        # A half-flipped configuration passes the real switches, so the gateway refuses it.
        assert (gate.options_enabled, gate.fno_carry_enabled) == (options, carry)


@pytest.mark.parametrize("sleeve", list(FoSleeve))
@pytest.mark.parametrize("other", list(FoSleeveGroup))
def test_another_sleeves_flag_never_makes_a_sleeve_live(
    sleeve: FoSleeve, other: FoSleeveGroup
) -> None:
    if other is group_of(sleeve):
        pytest.skip("its own flag")
    flags = FnoFlags(
        dry_run=False, options_enabled=True, fno_carry_enabled=True, **{_FIELD[other]: True}
    )
    assert fno_gates(sleeve, flags).mode is Mode.PAPER


def test_the_defaults_are_paper_for_every_sleeve() -> None:
    for sleeve in FoSleeve:
        assert fno_gates(sleeve, FnoFlags()).mode is Mode.PAPER


def test_intraday_is_not_an_fo_switch_and_there_is_no_unattended_flag() -> None:
    names = {CARRY_FLAG_ENV, *DESK_SWITCH_ENV, *EXECUTION_FLAG_ENV.values()}
    assert "INTRADAY_ENABLED" not in names
    assert "intraday_enabled" not in FnoFlags.__dataclass_fields__
    src = (
        Path(__file__).resolve().parents[1] / "src" / "baskfy_core" / "fno" / "gating.py"
    ).read_text(encoding="utf-8")
    assert not re.search(r"BASKFY_FNO_\w*AUTO", src.upper())
