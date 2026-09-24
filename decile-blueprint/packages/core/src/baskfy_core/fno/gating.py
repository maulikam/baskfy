"""``fno_gates(sleeve)`` — the one function that ANDs the FO run's four switches (``02`` Track B).

A real FO order needs **four** flags at once for its sleeve: ``DRY_RUN=false``,
``OPTIONS_ENABLED``, ``BASKFY_FNO_CARRY_ENABLED`` and ``BASKFY_FNO_<F1|F2>_EXECUTION_ENABLED``.
Every other combination — fifteen of sixteen, per sleeve — is ``PAPER``. **``INTRADAY_ENABLED``
is not one of them and must not become one**: an FO order is NRML, never MIS, so the gate this
module hands the gateway carries ``intraday_enabled=False`` in every row. The desk
(``app/fno_gates.py``) reads its own environment and calls this, so the rule exists once
(``options_gates()`` and ``swing_gates()`` are the precedents; DECISIONS-OP OP2.3).

The gateway switches it returns
-------------------------------
``dry_run`` is ``True`` whenever the mode is ``PAPER``. The two derivative switches depend on the
sleeve's own execution flag, exactly as OP2.3 settled for the options book:

* **Execution flag off** (the state this run ships in): ``options_enabled`` and
  ``fno_carry_enabled`` both ``True`` under ``dry_run=True``, so a paper confirm runs the real
  gateway path — the carry branch, the covered-overnight guard, risk, the journal — and is
  simulated by its dry-run branch **regardless of ``DRY_RUN``**. ``dry_run=True`` is what
  guarantees no broker call.
* **Execution flag on** but not all four: the real ``OPTIONS_ENABLED`` and
  ``BASKFY_FNO_CARRY_ENABLED`` pass through under ``dry_run=True``, so a half-flipped configuration
  is refused by the gateway itself rather than quietly simulated.

There is no unattended-entry input here and none may be added (``02`` Track B and Track C §3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from baskfy_core.fno.config import FoSleeve, FoSleeveGroup, group_of
from baskfy_core.options.config import Mode

#: The env var behind each sleeve group's execution flag (``02`` Track B), system-only.
EXECUTION_FLAG_ENV: Final[Mapping[FoSleeveGroup, str]] = {
    FoSleeveGroup.F1: "BASKFY_FNO_F1_EXECUTION_ENABLED",
    FoSleeveGroup.F2: "BASKFY_FNO_F2_EXECUTION_ENABLED",
}

#: The carry switch: NRML on NFO for ``fo_plan`` orders, and F2's GTT (``02`` §1). A LOCKED_KEY.
CARRY_FLAG_ENV: Final = "BASKFY_FNO_CARRY_ENABLED"

#: The operational flags — they move no money, and stay false by default.
OPERATIONAL_FLAG_ENV: Final[tuple[str, ...]] = (
    "BASKFY_FNO_SCAN_ENABLED",
    "BASKFY_FNO_MONITOR_ENABLED",
)

#: The desk's existing switch the FO book shares. ``INTRADAY_ENABLED`` is deliberately absent.
DESK_SWITCH_ENV: Final[tuple[str, ...]] = ("OPTIONS_ENABLED",)


@dataclass(frozen=True, slots=True)
class FnoFlags:
    """The switches as one process read them. Every default is the safe one."""

    dry_run: bool = True
    options_enabled: bool = False
    fno_carry_enabled: bool = False
    f1_execution_enabled: bool = False
    f2_execution_enabled: bool = False

    def execution_enabled(self, sleeve: FoSleeve) -> bool:
        """The sleeve's own execution flag; ``F1N`` and ``F1B`` share ``F1``'s."""
        if group_of(sleeve) is FoSleeveGroup.F2:
            return self.f2_execution_enabled
        return self.f1_execution_enabled


@dataclass(frozen=True, slots=True)
class FnoGate:
    """What a sleeve may do right now: its mode and the switches its gateway is handed."""

    sleeve: FoSleeve
    mode: Mode
    dry_run: bool
    options_enabled: bool
    fno_carry_enabled: bool
    #: Always ``False``: an FO order is NRML, never MIS (``02`` Track B).
    intraday_enabled: bool = False

    @property
    def simulated(self) -> bool:
        """Whether a confirm now would be journalled ``simulated=true``."""
        return self.mode is Mode.PAPER


def fno_gates(sleeve: FoSleeve, flags: FnoFlags) -> FnoGate:
    """``LIVE`` iff all four switches are on for ``sleeve``; ``PAPER`` otherwise (``02`` §B)."""
    execution = flags.execution_enabled(sleeve)
    live = not flags.dry_run and flags.options_enabled and flags.fno_carry_enabled and execution
    if live:
        return FnoGate(sleeve, Mode.LIVE, False, True, True)
    if not execution:
        # The paper gates: dry_run forces the gateway's simulated branch whatever DRY_RUN says.
        return FnoGate(sleeve, Mode.PAPER, True, True, True)
    return FnoGate(sleeve, Mode.PAPER, True, flags.options_enabled, flags.fno_carry_enabled)
