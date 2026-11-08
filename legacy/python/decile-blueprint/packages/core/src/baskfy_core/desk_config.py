"""The weekly book's scoring and scan configuration, frozen in core (docs/ranking/PLAN.md C2).

WHY THIS EXISTS
---------------
`baskfy_core.score` takes its configuration as an argument because the desk's `app/config.py`
reads the environment at import, and core touches nothing (Law 1). That is right for the desk,
which binds its own module. It left every *other* caller — the screener's `sort_by=desk_score`,
and now the nightly `desk_score_daily` writer — needing the same eleven values without being
allowed to import the desk. Phase 1 answered with a hand copy (`ranking.DeskScoringDefaults`)
that nothing tied to the desk; this module is that copy made accountable:

* `kite-momentum-rebalancer/tests/test_desk_config_parity.py` asserts every attribute
  `momentum_scan.build`, `score.required` and `score.score` read equals `app/config.py`, **and**
  records which attributes those functions actually read, so a new knob cannot be added to the
  scorer without this object and that test learning about it.
* `packages/core/tests/test_desk_score_service.py` pins a fingerprint of `score.py`,
  `momentum_scan.py` and :func:`canonical` of this object to `DESK_SCORE_VERSION`, so a change here
  that is not also a version bump fails a test instead of silently rewriting stored scores.

Changing a value here is a product decision about the live book. Change `app/config.py` first
(the desk is the authority — it is what was traded), then this, then the version.

ORDER IS PART OF THE VALUE
--------------------------
`MOMENTUM_BLEND` and `SHARPE_BLEND` are dicts whose *iteration order* matters twice: `required`
lists the return columns in blend order, and `score` sums `w * percentile` in blend order, and a
float sum taken in a different order can differ in the last bit — which a 1-dp rounding can turn
into a different SCORE and a different rank. The parity test compares item lists, not dicts.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Final

from baskfy_core.score import ScoringConfig


def _momentum_blend() -> dict[str, float]:
    return {
        "one_month": 0.10,
        "three_months": 0.30,
        "six_months": 0.30,
        "nine_months": 0.15,
        "one_year": 0.15,
    }


def _sharpe_blend() -> dict[str, float]:
    return {
        "three_months": 0.35,
        "six_months": 0.35,
        "nine_months": 0.15,
        "one_year": 0.15,
    }


@dataclass(slots=True)
class DeskConfig:
    """Satisfies :class:`baskfy_core.score.ScoringConfig` — the same attributes, the desk's values.

    Not frozen, and deliberately: `ScoringConfig` declares plain (settable) attributes, which a
    frozen dataclass does not satisfy (the reason `test_momentum_scan._Cfg` gives). Treat the
    instance as a constant — nothing in the tree assigns to it, and the version fingerprint is
    taken over its values.

    Types mirror the desk's module exactly (``MAX_CIRCUITS_3M = 5`` is an ``int`` there), so a
    comparison against ``app.config`` is a comparison of values, not of coercions.
    """

    EXCLUDED_SYMBOLS: Collection[str] = frozenset({"SGBDE31III"})
    REJECT_SERIES: Collection[str] = frozenset({"BE", "BZ"})
    MIN_MEDIAN_DAILY_VALUE: float = 5e7
    MAX_AWAY_FROM_HIGH: float = -30.0
    MAX_CIRCUITS_3M: float = 5
    PENALTY_CIRCUITS_1Y: float = 8
    MOMENTUM_BLEND: dict[str, float] = field(default_factory=_momentum_blend)
    SHARPE_BLEND: dict[str, float] = field(default_factory=_sharpe_blend)
    STOP_VOL_MULT: float = 2.2
    STOP_MIN: float = 0.08
    STOP_MAX: float = 0.12


#: The one instance every non-desk caller scores with.
DESK_CONFIG: Final[DeskConfig] = DeskConfig()

#: Every attribute of :class:`ScoringConfig`, in declaration order — what the parity test compares.
SCORING_ATTRIBUTES: Final[tuple[str, ...]] = tuple(ScoringConfig.__annotations__)


def canonical(cfg: ScoringConfig) -> str:
    """A stable text form of a scoring config, for fingerprinting.

    Sets are sorted (their order means nothing); dicts keep their item order (theirs does — see
    the module docstring); numbers keep their Python repr so ``5`` and ``5.0`` read differently,
    because the desk's module would too.
    """
    payload: dict[str, object] = {}
    for name in SCORING_ATTRIBUTES:
        value = getattr(cfg, name)
        if isinstance(value, dict):
            payload[name] = [[key, repr(weight)] for key, weight in value.items()]
        elif isinstance(value, (set, frozenset)):
            payload[name] = sorted(str(item) for item in value)
        else:
            payload[name] = repr(value)
    return json.dumps(payload, separators=(",", ":"))
