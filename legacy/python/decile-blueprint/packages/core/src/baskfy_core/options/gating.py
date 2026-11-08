"""``options_gates(sleeve)`` — the one function that ANDs the four switches (``02`` Track B).

A real options order needs **four** flags at once for its sleeve: ``DRY_RUN=false``,
``OPTIONS_ENABLED``, ``INTRADAY_ENABLED`` and ``BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED``.
Every other combination — fifteen of sixteen, per sleeve — is ``PAPER``. The desk
(``app/options_gates.py``) and the worker (``baskfy_worker.options``) read their own environment
and call this, so the rule exists once and the 16-row table is asserted once
(``test_options_gating.py``) and again at each wrapper.

The gateway switches it returns (DECISIONS-OP **OP2.3**)
------------------------------------------------------
``dry_run`` is ``True`` whenever the mode is ``PAPER``. The two product switches depend on
whether the sleeve's own execution flag is on:

* **Execution flag off** (the state this run ships in): the paper gates — ``options_enabled`` and
  ``intraday_enabled`` both ``True`` under ``dry_run=True``. This is what lets a paper confirm
  run the *real* gateway path (guards → risk → rate limit → journal) and be simulated by its
  dry-run branch (``02`` Track A, OP10), rather than being refused by the product gate before the
  code under rehearsal is reached. ``dry_run=True`` is what guarantees no broker call —
  non-negotiable 1, asserted by the gateway's own suite.
* **Execution flag on**: the desk's real ``OPTIONS_ENABLED`` / ``INTRADAY_ENABLED`` are passed
  through. A half-flipped configuration (the sleeve flag on, a desk switch off) is then refused
  by the **gateway itself**, leg by leg — the defence in depth ``06`` OP13 asks for — instead of
  quietly simulating something the operator believes is live.

There is no auto-execute input here and none may be added (PACK.3, ``02`` Track C §3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from baskfy_core.options.config import Mode, Sleeve, SleeveGroup, group_of

#: The env var behind each sleeve group's execution flag (``02`` Track B), system-only.
EXECUTION_FLAG_ENV: Final[Mapping[SleeveGroup, str]] = {
    SleeveGroup.O1M: "BASKFY_OPTIONS_O1M_EXECUTION_ENABLED",
    SleeveGroup.O1W: "BASKFY_OPTIONS_O1W_EXECUTION_ENABLED",
    SleeveGroup.O2: "BASKFY_OPTIONS_O2_EXECUTION_ENABLED",
    SleeveGroup.O3: "BASKFY_OPTIONS_O3_EXECUTION_ENABLED",
}

#: The three operational flags — they move no money (PACK.11), and stay false by default.
OPERATIONAL_FLAG_ENV: Final[tuple[str, ...]] = (
    "BASKFY_OPTIONS_MONITOR_ENABLED",
    "BASKFY_OPTIONS_COLLECT_ENABLED",
    "BASKFY_OPTIONS_SCAN_ENABLED",
)

#: The desk's two existing product switches, which the options book shares (``02`` Track B).
DESK_SWITCH_ENV: Final[tuple[str, ...]] = ("OPTIONS_ENABLED", "INTRADAY_ENABLED")

#: The six system-only ceilings of ``02`` "Ceilings", by ``OptionsCeilings`` field.
CEILING_ENV: Final[Mapping[str, str]] = {
    "risk_per_trade_inr_max": "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX",
    "risk_pct_max": "BASKFY_OPTIONS_RISK_PCT_MAX",
    "max_lots_max": "BASKFY_OPTIONS_MAX_LOTS_MAX",
    "book_daily_loss_inr_max": "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
    "book_monthly_loss_inr_max": "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
    "hard_exit_latest": "BASKFY_OPTIONS_HARD_EXIT_LATEST",
}


@dataclass(frozen=True, slots=True)
class OptionsFlags:
    """The switches as one process read them at startup. Every default is the safe one."""

    dry_run: bool = True
    options_enabled: bool = False
    intraday_enabled: bool = False
    o1m_execution_enabled: bool = False
    o1w_execution_enabled: bool = False
    o2_execution_enabled: bool = False
    o3_execution_enabled: bool = False

    def execution_enabled(self, sleeve: Sleeve) -> bool:
        """The sleeve's own execution flag; O3-A and O3-B share ``O3``'s."""
        group = group_of(sleeve)
        if group is SleeveGroup.O1M:
            return self.o1m_execution_enabled
        if group is SleeveGroup.O1W:
            return self.o1w_execution_enabled
        if group is SleeveGroup.O2:
            return self.o2_execution_enabled
        return self.o3_execution_enabled


@dataclass(frozen=True, slots=True)
class OptionsGate:
    """What a sleeve may do right now: its mode and the switches its gateway is handed."""

    sleeve: Sleeve
    mode: Mode
    dry_run: bool
    options_enabled: bool
    intraday_enabled: bool

    @property
    def simulated(self) -> bool:
        """Whether a confirm now would be journalled ``simulated=true``."""
        return self.mode is Mode.PAPER


def options_gates(sleeve: Sleeve, flags: OptionsFlags) -> OptionsGate:
    """``LIVE`` iff all four switches are on for ``sleeve``; ``PAPER`` otherwise (``02`` §B)."""
    execution = flags.execution_enabled(sleeve)
    live = not flags.dry_run and flags.options_enabled and flags.intraday_enabled and execution
    if live:
        return OptionsGate(sleeve, Mode.LIVE, False, True, True)
    if not execution:
        # The paper gates: dry_run forces the gateway's simulated branch (OP2.3).
        return OptionsGate(sleeve, Mode.PAPER, True, True, True)
    return OptionsGate(sleeve, Mode.PAPER, True, flags.options_enabled, flags.intraday_enabled)
