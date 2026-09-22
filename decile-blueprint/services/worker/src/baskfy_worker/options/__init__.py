"""The worker's side of the options run (``docs/options``): gates, the NFO master, partitions.

The worker places nothing (law 2). It reads the switches so a session's ``mode`` and a scan's
label say what the desk would say, and it keeps the market-data tables the desk and the scans read.

``options_gates(sleeve)`` here and ``app/options_gates.py`` on the desk are two readers of two
environments around **one** rule, ``baskfy_core.options.gating.options_gates`` (``06`` OP2).
"""

from __future__ import annotations

from baskfy_core.options.config import OptionsCeilings, Sleeve
from baskfy_core.options.gating import OptionsFlags, OptionsGate
from baskfy_core.options.gating import options_gates as _options_gates
from baskfy_worker.settings import WorkerSettings, get_worker_settings


def _desk_true(value: str) -> bool:
    """The desk's parse for ``OPTIONS_ENABLED`` / ``INTRADAY_ENABLED``: on only when ``true``."""
    return value.strip().lower() == "true"


def _desk_dry_run(value: str) -> bool:
    """The desk's parse for ``DRY_RUN``: dry unless exactly ``false`` (``app/config.py``)."""
    return value.strip().lower() != "false"


def options_flags(settings: WorkerSettings | None = None) -> OptionsFlags:
    """The switches as this process read them at startup."""
    s = settings or get_worker_settings()
    return OptionsFlags(
        dry_run=_desk_dry_run(s.options_desk_dry_run),
        options_enabled=_desk_true(s.options_desk_options_enabled),
        intraday_enabled=_desk_true(s.options_desk_intraday_enabled),
        o1m_execution_enabled=s.options_o1m_execution_enabled,
        o1w_execution_enabled=s.options_o1w_execution_enabled,
        o2_execution_enabled=s.options_o2_execution_enabled,
        o3_execution_enabled=s.options_o3_execution_enabled,
    )


def options_gates(sleeve: Sleeve, settings: WorkerSettings | None = None) -> OptionsGate:
    """``PAPER`` unless all four switches are on for ``sleeve`` (``02`` Track B)."""
    return _options_gates(sleeve, options_flags(settings))


def options_ceilings(settings: WorkerSettings | None = None) -> OptionsCeilings:
    """``02`` "Ceilings" as the pure core takes them."""
    s = settings or get_worker_settings()
    return OptionsCeilings(
        risk_per_trade_inr_max=s.options_risk_per_trade_inr_max,
        risk_pct_max=s.options_risk_pct_max,
        max_lots_max=s.options_max_lots_max,
        book_daily_loss_inr_max=s.options_book_daily_loss_inr_max,
        book_monthly_loss_inr_max=s.options_book_monthly_loss_inr_max,
        hard_exit_latest=s.options_hard_exit_latest,
    )
