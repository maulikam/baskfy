"""``docs/options/04-business-rules.md`` is the contract; this test is what makes that true.

``04`` §14 carries one row per field of ``baskfy_core.options.config.OptionsConfig`` with the
value it holds. This test regenerates that table from the code and asserts it **both ways** (the
``test_vbt_docs_parity.py`` pattern): a field added, renamed or re-valued without a doc edit is
red, and a row in the document with no field behind it is red too. OP1's AC "every config field
name appears in ``04``" is the weaker half of this.

CLAUDE.md's rule applies where a test enforces a document: **when the code and a doc disagree,
look for the commit that changed the value before assuming the code is wrong.**
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest

from baskfy_core.options.config import (
    DEFAULT_OPTIONS_CONFIG,
    CalendarConfig,
    CondorVariant,
    OptionsConfig,
    Sleeve,
    SleeveGroup,
    group_of,
)

DOC = Path(__file__).resolve().parents[4] / "docs" / "options" / "04-business-rules.md"
_ROW = re.compile(r"^\| `([a-z_0-9]+\.[a-z_0-9]+)` \| `(.*)` \|$")


def render(value: object) -> str:  # noqa: PLR0911 - one return per value type
    """How a default is written in §14's table. One renderer, used by the test and the doc."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return ", ".join(render(item) for item in value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def from_code() -> dict[str, str]:
    out: dict[str, str] = {}
    for group in dataclasses.fields(DEFAULT_OPTIONS_CONFIG):
        sub = getattr(DEFAULT_OPTIONS_CONFIG, group.name)
        for spec in dataclasses.fields(sub):
            out[f"{group.name}.{spec.name}"] = render(getattr(sub, spec.name))
    return out


def from_doc() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        found = _ROW.match(line)
        if found:
            rows[found.group(1)] = found.group(2)
    return rows


def test_the_contract_document_exists() -> None:
    assert DOC.is_file()


def test_the_document_names_every_field_and_nothing_else() -> None:
    code, doc = from_code(), from_doc()
    assert sorted(doc) == sorted(code), (
        f"only in the code: {sorted(set(code) - set(doc))}; "
        f"only in docs/options/04 §14: {sorted(set(doc) - set(code))}"
    )


@pytest.mark.parametrize(("field", "value"), sorted(from_code().items()))
def test_every_default_is_the_documented_value(field: str, value: str) -> None:
    assert from_doc().get(field) == value, f"docs/options/04 §14 disagrees about {field}"


def test_the_groups_are_the_ten_04_names() -> None:
    """``04``'s header: groups calendar, chain, condor_monthly, condor_weekly, directional,
    expiry_setups, costs, sizing, execution, risk."""
    assert [f.name for f in dataclasses.fields(OptionsConfig)] == [
        "calendar", "chain", "condor_monthly", "condor_weekly", "directional",
        "expiry_setups", "costs", "sizing", "execution", "risk",
    ]  # fmt: skip


def test_the_prose_states_the_load_bearing_numbers() -> None:
    """The table is machine-checked; these are numbers §1-§9 state in prose, and they must agree."""
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    for phrase in (
        "`snapshot_strikes` [15]",
        "`rate` [0.065]",
        "max_spread_pct` [3.0]",
        "min_oi_lots` [200]",
        "gap_max_pct` [1.0]",
        "or_max_pct` [0.90]",
        "vix_max` [22]",
        "`itm_steps` 1",
        "[`width_points` 100]",
        "**0.15** (from 1 Apr 2026; verified OP0)",
        "**0.03553**",
        "daily_loss_r` [2]",
        "`first_live_trades` [5]",
    ):
        assert phrase in text, f"docs/options/04's prose no longer states {phrase!r}"


class TestTheCondorVariants:
    """``04`` §3.2: two groups, equal except risk, distinct instances."""

    def test_distinct_instances(self) -> None:
        cfg = OptionsConfig()
        assert cfg.condor_monthly is not cfg.condor_weekly
        assert cfg.condor_monthly.variant is CondorVariant.MONTHLY
        assert cfg.condor_weekly.variant is CondorVariant.WEEKLY

    def test_equal_except_risk_and_sample(self) -> None:
        m = dataclasses.asdict(DEFAULT_OPTIONS_CONFIG.condor_monthly)
        w = dataclasses.asdict(DEFAULT_OPTIONS_CONFIG.condor_weekly)
        differ = {k for k in m if m[k] != w[k]}
        assert differ == {"variant", "risk_per_trade_pct", "max_lots", "tier3_min_sessions"}
        assert (m["risk_per_trade_pct"], w["risk_per_trade_pct"]) == (
            Decimal("1.0"),
            Decimal("0.5"),
        )
        assert (m["max_lots"], w["max_lots"]) == (3, 2)

    def test_moving_one_does_not_move_the_other(self) -> None:
        moved = dataclasses.replace(
            DEFAULT_OPTIONS_CONFIG,
            condor_weekly=dataclasses.replace(
                DEFAULT_OPTIONS_CONFIG.condor_weekly, er_max=Decimal("0.25")
            ),
        )
        assert moved.condor_monthly.er_max == Decimal("0.30")


class TestPerSleeveAccessors:
    @pytest.mark.parametrize(
        ("sleeve", "hard_exit", "pct", "lots", "reserve", "share", "tier3"),
        [
            (Sleeve.O1M, dt.time(14, 30), "1.0", 3, "1000", "0.20", 12),
            (Sleeve.O1W, dt.time(14, 30), "0.5", 2, "1000", "0.20", 20),
            (Sleeve.O2, dt.time(15, 0), "0.5", 2, "300", "0.15", 60),
            (Sleeve.O3A, dt.time(14, 45), "0.5", 2, "500", "0.20", 20),
            (Sleeve.O3B, dt.time(14, 45), "0.5", 2, "500", "0.20", 20),
        ],
    )
    def test_the_documented_values_per_sleeve(  # noqa: PLR0913, PLR0917 - parametrize columns
        self,
        sleeve: Sleeve,
        hard_exit: dt.time,
        pct: str,
        lots: int,
        reserve: str,
        share: str,
        tier3: int,
    ) -> None:
        cfg = DEFAULT_OPTIONS_CONFIG
        assert cfg.hard_exit_time(sleeve) == hard_exit
        assert cfg.risk_per_trade_pct(sleeve) == Decimal(pct)
        assert cfg.max_lots(sleeve) == lots
        assert cfg.reserve_per_lot_inr(sleeve) == Decimal(reserve)
        assert cfg.cost_share_max(sleeve) == Decimal(share)
        assert cfg.tier3_min_sessions(sleeve) == tier3

    def test_o3a_and_o3b_are_one_group(self) -> None:
        assert group_of(Sleeve.O3A) is group_of(Sleeve.O3B) is SleeveGroup.O3
        assert group_of(Sleeve.O2) is SleeveGroup.O2


class TestNiftyOnly:
    """Track C §5 / PACK.12: the config accepts NIFTY only."""

    @pytest.mark.parametrize("underlying", ["BANKNIFTY", "FINNIFTY", "SENSEX", "RELIANCE"])
    def test_another_underlying_is_refused(self, underlying: str) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            CalendarConfig(underlying=underlying)

    def test_widening_the_allow_list_is_the_only_way(self) -> None:
        assert DEFAULT_OPTIONS_CONFIG.calendar.allowed_underlyings == ("NIFTY",)


def test_the_config_dataclasses_are_frozen() -> None:
    for group in dataclasses.fields(OptionsConfig):
        sub = getattr(DEFAULT_OPTIONS_CONFIG, group.name)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(sub, dataclasses.fields(sub)[0].name, None)
