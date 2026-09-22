"""The options book's switches on the desk (docs/options/02 Track B, OP2).

`options_gates(sleeve)` reads the desk's four switches for that sleeve — `DRY_RUN`,
`OPTIONS_ENABLED`, `INTRADAY_ENABLED` and `BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED` — at the
moment it is called, and hands them to `baskfy_core.options.gating.options_gates`, the one
function that ANDs them (the worker has the same reader over its own environment). `LIVE` needs
all four; every other combination is `PAPER`.

`product_gates(sleeve)` is the `ProductGates` callable OP10's gateway will be built with. In PAPER
it always carries `dry_run=True`, so the gateway's dry-run branch simulates every leg and nothing
reaches a broker (non-negotiable 1). DECISIONS-OP OP2.3 says why the product switches it carries
depend on the sleeve's own flag.

Nothing here places an order. There is no auto-execute input and none may be added (PACK.3).
"""
from __future__ import annotations

from baskfy_core.options.config import Sleeve
from baskfy_core.options.gating import OptionsFlags, OptionsGate
from baskfy_core.options.gating import options_gates as _options_gates
from baskfy_execution.gateway import ProductGates

from . import config as C


def options_flags() -> OptionsFlags:
    """The desk's switches, read off `app.config` now (a test may monkeypatch them)."""
    return OptionsFlags(
        dry_run=bool(C.DRY_RUN),
        options_enabled=bool(C.OPTIONS_ENABLED),
        intraday_enabled=bool(C.INTRADAY_ENABLED),
        o1m_execution_enabled=bool(C.OPTIONS_O1M_EXECUTION_ENABLED),
        o1w_execution_enabled=bool(C.OPTIONS_O1W_EXECUTION_ENABLED),
        o2_execution_enabled=bool(C.OPTIONS_O2_EXECUTION_ENABLED),
        o3_execution_enabled=bool(C.OPTIONS_O3_EXECUTION_ENABLED),
    )


def options_gates(sleeve: Sleeve) -> OptionsGate:
    """`PAPER` unless all four switches are on for `sleeve`."""
    return _options_gates(sleeve, options_flags())


def product_gates(sleeve: Sleeve) -> ProductGates:
    """The switches this sleeve's gateway is handed, read at the moment of the order."""
    gate = options_gates(sleeve)
    return ProductGates(
        dry_run=gate.dry_run,
        intraday_enabled=gate.intraday_enabled,
        options_enabled=gate.options_enabled,
    )
