"""`baskfy_core.desk_config.DESK_CONFIG` is this desk's `app/config.py`, attribute for attribute.

Ranking Phase 2, leaf 2.B (docs/ranking/PLAN.md C2). The screener and the nightly
`desk_score_daily` writer cannot import this module — core touches nothing, and `app.config`
reads the environment at import — so core carries a copy. A copy nothing checks is how Phase 1's
`DeskScoringDefaults` came to exist untested. This file is the check, from the side that owns the
values: the desk is what was traded, so when the two disagree it is core that is wrong.

Two properties, because either alone is not enough:

1. every attribute `score.ScoringConfig` names is equal here and there — values, types, and for
   the two blends the item ORDER, which changes both `required()`'s column order and the float
   sum inside `score()`;
2. the attributes `momentum_scan.build`, `score.required` and `score.score` actually READ are all
   among those compared, so a knob added to the scorer tomorrow cannot bypass property 1.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from app import config as C
from app import scan_source
from app import scoring
from baskfy_core import momentum_scan
from baskfy_core import score as core_score
from baskfy_core.desk_config import DESK_CONFIG, SCORING_ATTRIBUTES

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "decile-blueprint" / "packages" / "core" / "tests"
)
sys.path.insert(0, str(FIXTURE_DIR))
import _desk_score_fixture as fixture  # noqa: E402


def _normalise(value: object) -> object:
    if isinstance(value, dict):
        return ("dict", list(value.items()))
    if isinstance(value, (set, frozenset)):
        return ("set", sorted(value))
    return (type(value).__name__, value)


def mismatches(desk: object, core: object) -> list[str]:
    """Every scoring attribute on which `core` differs from `desk`, described."""
    found = []
    for name in SCORING_ATTRIBUTES:
        want, got = _normalise(getattr(desk, name)), _normalise(getattr(core, name))
        if want != got:
            found.append(f"{name}: app.config={want!r} DESK_CONFIG={got!r}")
    return found


def test_desk_config_equals_app_config_on_every_scoring_attribute() -> None:
    assert mismatches(C, DESK_CONFIG) == [], (
        "baskfy_core.desk_config.DESK_CONFIG has drifted from app/config.py. The desk is the "
        "authority: fix DESK_CONFIG, then bump DESK_SCORE_VERSION."
    )


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("MIN_MEDIAN_DAILY_VALUE", 4e7),
        ("MAX_CIRCUITS_3M", 6),
        ("MAX_CIRCUITS_3M", 5.0),  # a type change is a change: 5 and 5.0 are not the same config
        ("REJECT_SERIES", {"BE"}),
        ("EXCLUDED_SYMBOLS", {"SGBDE31III", "SGBMAR29"}),
        ("MOMENTUM_BLEND", dict(reversed(list(C.MOMENTUM_BLEND.items())))),
        ("SHARPE_BLEND", {**C.SHARPE_BLEND, "one_year": 0.20}),
        ("STOP_MAX", 0.13),
    ],
)
def test_a_change_to_the_desks_config_fails_the_parity_check(
    monkeypatch: pytest.MonkeyPatch, name: str, changed: object
) -> None:
    """Proof the check above bites: change one desk value and it reports exactly that one."""
    monkeypatch.setattr(C, name, changed)
    reported = mismatches(C, DESK_CONFIG)
    assert [line.split(":")[0] for line in reported] == [name]


class _Recorder:
    """Stands in for a config and remembers every attribute read from it."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.read: set[str] = set()

    def __getattr__(self, name: str) -> object:
        self.read.add(name)
        return getattr(self._inner, name)


def _exercise(cfg: object) -> None:
    days = fixture.trading_days()
    scan = momentum_scan.build(fixture.bars(), days[-1], days, cfg=cfg, carried=fixture.carried())
    core_score.required(cfg)
    core_score.score(pd.DataFrame(scan.frame.to_dicts()), cfg)


def test_every_attribute_the_book_reads_is_compared() -> None:
    recorder = _Recorder(C)
    _exercise(recorder)
    assert recorder.read, "the recorder saw nothing; the exercise did not reach the scorer"
    assert recorder.read <= set(SCORING_ATTRIBUTES), sorted(recorder.read - set(SCORING_ATTRIBUTES))
    # the scan and the score between them read every filter and both blends
    assert {
        "EXCLUDED_SYMBOLS",
        "REJECT_SERIES",
        "MIN_MEDIAN_DAILY_VALUE",
        "MAX_AWAY_FROM_HIGH",
        "MAX_CIRCUITS_3M",
        "PENALTY_CIRCUITS_1Y",
        "MOMENTUM_BLEND",
        "SHARPE_BLEND",
    } <= recorder.read


@pytest.mark.parametrize(
    ("label", "bound"),
    [
        ("scoring.score", lambda: scoring.score.__globals__["C"]),
        ("scan_source.generate", lambda: scan_source.generate.__globals__["C"]),
    ],
)
def test_the_book_binds_app_config_where_it_scores(label: str, bound: Callable[[], object]) -> None:
    """The comparison is only meaningful if the book's own scoring path reads `app.config`."""
    assert bound() is C, label
