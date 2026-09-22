"""Lots from a risk budget; margin as a ceiling, never a source (``04`` §7).

The order of operations is the spec's: a budget from the sleeve's own capital (capped by the env
ceiling), the first-live multiplier on top, lots = floor(budget / risk per lot) capped by the
sleeve's and the system's max lots — and only then a margin check the plan must fit *under*
(Track C §10: no sizing from margin). A sleeve at ₹0 plans one lot in paper and nothing live
(PACK.6).

Risk per lot per structure is the worst case known to the rupee before entry plus the method's
flat reserve: the condor's ``(width - credit) * lot`` (condor ``04`` §6.1), O2's premium at the
stop (``04`` §4.6), O3's debit (``04`` §5.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from baskfy_core.options.config import Mode, OptionsCeilings, SizingConfig, SizingMode

_HUNDRED = Decimal(100)


class SizingReject(StrEnum):
    REJECTED_NO_LOT_SIZE = "REJECTED_NO_LOT_SIZE"
    REJECTED_NO_SLEEVE_CAPITAL = "REJECTED_NO_SLEEVE_CAPITAL"
    REJECTED_BUDGET = "REJECTED_BUDGET"
    REJECTED_PREMIUM_CAP = "REJECTED_PREMIUM_CAP"


def risk_per_lot_condor(
    wing_width_points: Decimal, credit_points: Decimal, lot_size: int, reserve_per_lot_inr: Decimal
) -> Decimal:
    """O1: ``(wing_width - credit) * lot_size + reserve`` (condor ``04`` §6.1-6.2)."""
    return (wing_width_points - credit_points) * lot_size + reserve_per_lot_inr


def risk_per_lot_long(
    planned_entry: Decimal, stop_frac: Decimal, lot_size: int, reserve_per_lot_inr: Decimal
) -> Decimal:
    """O2: ``E_planned * stop_frac * lot_size + reserve`` (``04`` §4.6)."""
    return planned_entry * stop_frac * lot_size + reserve_per_lot_inr


def gap_through_long(entry: Decimal, lot_size: int) -> Decimal:
    """O2's true worst case per lot, shown on every plan: the whole premium (``04`` §4.6)."""
    return entry * lot_size


def risk_per_lot_debit_spread(
    debit: Decimal, lot_size: int, reserve_per_lot_inr: Decimal
) -> Decimal:
    """O3: ``debit * lot_size + reserve`` (``04`` §5.5)."""
    return debit * lot_size + reserve_per_lot_inr


@dataclass(frozen=True, slots=True)
class Sizing:
    """The sizing's answer. ``lots == 0`` iff ``rejection`` is set."""

    lots: int
    sizing_mode: SizingMode
    risk_budget_inr: Decimal
    #: The R the journal divides by: the budget in force, or one lot's risk in paper-one-lot.
    r_inr: Decimal
    risk_per_lot_inr: Decimal
    half_size: bool
    rejection: SizingReject | None
    message: str


def _reject(
    code: SizingReject, message: str, risk_per_lot: Decimal, budget: Decimal = Decimal(0)
) -> Sizing:
    return Sizing(
        lots=0,
        sizing_mode=SizingMode.BUDGET,
        risk_budget_inr=budget,
        r_inr=budget,
        risk_per_lot_inr=risk_per_lot,
        half_size=False,
        rejection=code,
        message=message,
    )


def first_live(real_journal_rows: int, config: SizingConfig) -> bool:
    """``04`` §7.5: fewer than ``first_live_trades`` rows with ``simulated=false``."""
    return real_journal_rows < config.first_live_trades


def size(  # noqa: PLR0913 - every input 04 §7 names, by keyword
    *,
    mode: Mode,
    sleeve_capital_inr: Decimal,
    risk_per_trade_pct: Decimal,
    max_lots: int,
    risk_per_lot_inr: Decimal,
    lot_size: int | None,
    real_journal_rows: int,
    config: SizingConfig,
    ceilings: OptionsCeilings,
) -> Sizing:
    """``04`` §7.1-7.3 and §7.5.

    * lot size missing or 0 → ``REJECTED_NO_LOT_SIZE`` (§1.4);
    * capital ₹0: ``PAPER`` → one lot, ``PAPER_ONE_LOT``, R = one lot's risk; ``LIVE`` →
      ``REJECTED_NO_SLEEVE_CAPITAL`` (§7.3, PACK.6);
    * otherwise ``budget = min(capital * pct / 100, RISK_PER_TRADE_INR_MAX)`` — the pct itself
      capped at ``RISK_PCT_MAX`` — times the first-live multiplier while fewer than five real rows
      exist (tagged ``half_size``), and ``lots = floor(budget / risk_per_lot)`` capped by the
      sleeve's ``max_lots`` and ``MAX_LOTS_MAX``; 0 → ``REJECTED_BUDGET`` with the arithmetic.
    """
    if lot_size is None or lot_size <= 0:
        return _reject(
            SizingReject.REJECTED_NO_LOT_SIZE,
            "the master has no lot size for this expiry; nothing is sized from a guess",
            risk_per_lot_inr,
        )
    if risk_per_lot_inr <= 0:
        raise ValueError("risk per lot must be positive — a structure with no risk is a bug")
    if sleeve_capital_inr <= 0:
        if mode is Mode.LIVE:
            return _reject(
                SizingReject.REJECTED_NO_SLEEVE_CAPITAL,
                "sleeve capital is ₹0; a live plan needs capital Maulik has set",
                risk_per_lot_inr,
            )
        return Sizing(
            lots=1,
            sizing_mode=SizingMode.PAPER_ONE_LOT,
            risk_budget_inr=risk_per_lot_inr,
            r_inr=risk_per_lot_inr,
            risk_per_lot_inr=risk_per_lot_inr,
            half_size=False,
            rejection=None,
            message="paper, sleeve capital ₹0: one lot, R = one lot's risk",
        )
    pct = min(risk_per_trade_pct, ceilings.risk_pct_max)
    budget = min(sleeve_capital_inr * pct / _HUNDRED, ceilings.risk_per_trade_inr_max)
    half = first_live(real_journal_rows, config)
    if half:
        budget = budget * config.first_live_risk_multiplier
    raw = int((budget / risk_per_lot_inr).to_integral_value(rounding=ROUND_FLOOR))
    lots = min(raw, max_lots, ceilings.max_lots_max)
    if lots <= 0:
        return _reject(
            SizingReject.REJECTED_BUDGET,
            f"risk budget ₹{budget:.2f} / risk per lot ₹{risk_per_lot_inr:.2f} = {raw} lots",
            risk_per_lot_inr,
            budget,
        )
    return Sizing(
        lots=lots,
        sizing_mode=SizingMode.BUDGET,
        risk_budget_inr=budget,
        r_inr=budget,
        risk_per_lot_inr=risk_per_lot_inr,
        half_size=half,
        rejection=None,
        message=f"₹{budget:.2f} / ₹{risk_per_lot_inr:.2f} per lot → {lots} lots",
    )


def premium_cap_ok(
    lots: int, entry: Decimal, lot_size: int, sleeve_capital_inr: Decimal, premium_cap_pct: Decimal
) -> bool:
    """O2's premium cap: ``lots * E * lot_size ≤ premium_cap_pct % of capital`` when capital > 0
    (``04`` §4.6). With capital ₹0 there is nothing to cap against (paper one lot)."""
    if sleeve_capital_inr <= 0:
        return True
    return lots * entry * lot_size <= sleeve_capital_inr * premium_cap_pct / _HUNDRED


class MarginCode(StrEnum):
    OK = "OK"
    MARGIN_POOL_UNSET = "MARGIN_POOL_UNSET"
    REJECTED_MARGIN = "REJECTED_MARGIN"


@dataclass(frozen=True, slots=True)
class MarginCheck:
    code: MarginCode
    available_inr: Decimal
    message: str

    @property
    def rejected(self) -> bool:
        return self.code is MarginCode.REJECTED_MARGIN


def margin_check(
    *,
    mode: Mode,
    hedged_estimate_inr: Decimal,
    transient_estimate_inr: Decimal | None,
    margin_pool_inr: Decimal,
    margin_in_use_inr: Decimal,
) -> MarginCheck:
    """``04`` §7.4 — the broker's basket estimate must fit under the free pool.

    O1 passes the transient figure too (between the wing and short fills, condor §6.3); O2 and O3
    pass ``None``. With the pool unset (₹0) a paper plan shows the figure and warns
    ``MARGIN_POOL_UNSET``; a live plan is ``REJECTED_MARGIN``.
    """
    available = margin_pool_inr - margin_in_use_inr
    worst = max(hedged_estimate_inr, transient_estimate_inr or Decimal(0))
    if margin_pool_inr <= 0:
        if mode is Mode.PAPER:
            return MarginCheck(
                MarginCode.MARGIN_POOL_UNSET,
                available,
                f"margin pool unset; this plan needs ₹{worst:.2f}",
            )
        return MarginCheck(MarginCode.REJECTED_MARGIN, available, "margin pool unset (live)")
    if worst > available:
        which = "transient" if worst != hedged_estimate_inr else "hedged"
        return MarginCheck(
            MarginCode.REJECTED_MARGIN,
            available,
            f"{which} margin ₹{worst:.2f} > free pool ₹{available:.2f}",
        )
    return MarginCheck(MarginCode.OK, available, f"₹{worst:.2f} within free ₹{available:.2f}")
