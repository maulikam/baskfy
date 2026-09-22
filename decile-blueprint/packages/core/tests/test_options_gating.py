"""``06`` OP2 AC: ``options_gates()`` returns ``PAPER`` for every combination but all-four-true.

The 16-row table (``DRY_RUN``, ``OPTIONS_ENABLED``, ``INTRADAY_ENABLED``, the sleeve's execution
flag) by every sleeve, asserted against ``docs/options/02`` Track B — plus the other sleeves'
flags, which must never make a sleeve live. The desk and worker wrappers repeat the table against
their own environment readers.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from baskfy_core.options import gating
from baskfy_core.options.config import Mode, Sleeve, SleeveGroup, group_of
from baskfy_core.options.gating import EXECUTION_FLAG_ENV, OptionsFlags, options_gates

MONOREPO = Path(__file__).resolve().parents[4]
SCOPE = (MONOREPO / "docs" / "options" / "02-scope-and-gating.md").read_text(encoding="utf-8")

ROWS = list(itertools.product((False, True), repeat=4))
_FIELD = {
    SleeveGroup.O1M: "o1m_execution_enabled",
    SleeveGroup.O1W: "o1w_execution_enabled",
    SleeveGroup.O2: "o2_execution_enabled",
    SleeveGroup.O3: "o3_execution_enabled",
}


def _flags(
    sleeve: Sleeve, dry_run_off: bool, options: bool, intraday: bool, execution: bool
) -> OptionsFlags:
    return OptionsFlags(
        dry_run=not dry_run_off,
        options_enabled=options,
        intraday_enabled=intraday,
        **{_FIELD[group_of(sleeve)]: execution},
    )


def test_the_table_has_sixteen_rows_and_five_sleeves() -> None:
    assert len(ROWS) == 16
    assert len(list(Sleeve)) == 5


@pytest.mark.parametrize("sleeve", list(Sleeve))
@pytest.mark.parametrize(("dry_run_off", "options", "intraday", "execution"), ROWS)
def test_live_only_when_all_four_are_on(
    sleeve: Sleeve, dry_run_off: bool, options: bool, intraday: bool, execution: bool
) -> None:
    gate = options_gates(sleeve, _flags(sleeve, dry_run_off, options, intraday, execution))
    all_four = dry_run_off and options and intraday and execution
    assert gate.mode is (Mode.LIVE if all_four else Mode.PAPER)
    assert gate.dry_run is (not all_four), "a PAPER gate must hand the gateway dry_run=True"
    assert gate.simulated is (not all_four)
    assert gate.sleeve is sleeve


@pytest.mark.parametrize("sleeve", list(Sleeve))
def test_every_other_sleeves_flag_on_never_makes_this_one_live(sleeve: Sleeve) -> None:
    others = {f: True for g, f in _FIELD.items() if g is not group_of(sleeve)}
    flags = OptionsFlags(dry_run=False, options_enabled=True, intraday_enabled=True, **others)
    assert options_gates(sleeve, flags).mode is Mode.PAPER


def test_o3a_and_o3b_share_the_o3_flag() -> None:
    flags = OptionsFlags(
        dry_run=False, options_enabled=True, intraday_enabled=True, o3_execution_enabled=True
    )
    assert options_gates(Sleeve.O3A, flags).mode is Mode.LIVE
    assert options_gates(Sleeve.O3B, flags).mode is Mode.LIVE


def test_the_defaults_are_paper_for_every_sleeve() -> None:
    for sleeve in Sleeve:
        assert options_gates(sleeve, OptionsFlags()).mode is Mode.PAPER


@pytest.mark.parametrize("sleeve", list(Sleeve))
@pytest.mark.parametrize(("options", "intraday"), list(itertools.product((False, True), repeat=2)))
def test_a_half_flipped_sleeve_hands_the_gateway_the_real_desk_switches(
    sleeve: Sleeve, options: bool, intraday: bool
) -> None:
    """OP2.3: execution flag on but not live → the gateway sees the desk's own switches, so it
    refuses the leg itself when one is off (``06`` OP13's defence in depth)."""
    gate = options_gates(sleeve, _flags(sleeve, False, options, intraday, True))
    assert gate.mode is Mode.PAPER
    assert (gate.options_enabled, gate.intraday_enabled) == (options, intraday)


@pytest.mark.parametrize("sleeve", list(Sleeve))
def test_with_the_execution_flag_off_paper_runs_the_real_gateway_path(sleeve: Sleeve) -> None:
    """OP2.3: the paper gates admit NFO MIS *only* under dry_run, so the dry-run branch runs."""
    gate = options_gates(sleeve, OptionsFlags())
    assert (gate.dry_run, gate.options_enabled, gate.intraday_enabled) == (True, True, True)


def test_the_flag_names_are_the_documents() -> None:
    for name in EXECUTION_FLAG_ENV.values():
        assert f"`{name}`" in SCOPE
    for name in gating.OPERATIONAL_FLAG_ENV + gating.DESK_SWITCH_ENV:
        assert f"`{name}`" in SCOPE
    for name in gating.CEILING_ENV.values():
        assert f"`{name}`" in SCOPE
    assert len(EXECUTION_FLAG_ENV) + len(gating.OPERATIONAL_FLAG_ENV) + 2 == 9  # 02's nine


def test_there_is_no_auto_execute_input() -> None:
    """PACK.3: no field, flag or env name for auto-execution, not even defaulted false."""
    source = Path(gating.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if "PACK.3" not in line)
    assert "AUTO_EXECUTE" not in code.upper().replace("AUTO-EXECUTE", "")
    assert not [f for f in OptionsFlags.__dataclass_fields__ if "auto" in f]
