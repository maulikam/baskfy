"""The FO book's switches on the desk (docs/fno/02 Track B, FO6).

`fno_gates(sleeve)` reads the desk's four switches for that sleeve — `DRY_RUN`, `OPTIONS_ENABLED`,
`BASKFY_FNO_CARRY_ENABLED` and `BASKFY_FNO_<F1|F2|F3>_EXECUTION_ENABLED` — at the moment it is
called, and hands them to `baskfy_core.fno.gating.fno_gates`, the one function that ANDs them.
`LIVE` needs all four; every other combination is `PAPER`. `INTRADAY_ENABLED` is not read: an FO
order is NRML, never MIS.

`product_gates(sleeve)` is the `ProductGates` callable the FO gateway is built with (FO8). In
PAPER it always carries `dry_run=True`, so the gateway's dry-run branch simulates every leg and
nothing reaches a broker (non-negotiable 1), whatever `DRY_RUN` says. `intraday_enabled` is False
in every row.

Nothing here places an order. There is no unattended-entry input and none may be added.
"""
from __future__ import annotations

from baskfy_core.fno.config import FoSleeve
from baskfy_core.fno.gating import FnoFlags, FnoGate
from baskfy_core.fno.gating import fno_gates as _fno_gates
from baskfy_execution.gateway import ProductGates

from . import config as C


def fno_flags() -> FnoFlags:
    """The desk's switches, read off `app.config` now (a test may monkeypatch them)."""
    return FnoFlags(
        dry_run=bool(C.DRY_RUN),
        options_enabled=bool(C.OPTIONS_ENABLED),
        fno_carry_enabled=bool(C.FNO_CARRY_ENABLED),
        f1_execution_enabled=bool(C.FNO_F1_EXECUTION_ENABLED),
        f2_execution_enabled=bool(C.FNO_F2_EXECUTION_ENABLED),
        f3_execution_enabled=bool(C.FNO_F3_EXECUTION_ENABLED),
    )


def fno_gates(sleeve: FoSleeve) -> FnoGate:
    """`PAPER` unless all four switches are on for `sleeve`."""
    return _fno_gates(sleeve, fno_flags())


def product_gates(sleeve: FoSleeve) -> ProductGates:
    """The switches this sleeve's gateway is handed, read at the moment of the order."""
    gate = fno_gates(sleeve)
    return ProductGates(
        dry_run=gate.dry_run,
        intraday_enabled=False,
        options_enabled=gate.options_enabled,
        fno_carry_enabled=gate.fno_carry_enabled,
    )
