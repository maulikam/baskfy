from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from baskfy_api.opportunities import (
    MAX_RESULTS,
    MAX_UNIVERSE,
    OptionsAnalysis,
    ProviderState,
    SwingScan,
)
from baskfy_core.opportunities.config import DataStatus, QuoteRules
from baskfy_core.opportunities.options import MODEL_NOTE, OptionCandidate
from baskfy_core.opportunities.ranking import ScoreBreakdown
from baskfy_core.opportunities.swing import SwingOpportunity
from baskfy_core.opportunities.validation import ValidationResult

JsonDict = dict[str, object]
_Q2: Final = Decimal("0.01")
_PCT: Final = 100.0


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def score_json(score: ScoreBreakdown) -> JsonDict:
    return {
        "total": score.total,
        "components": [
            {
                "key": c.key,
                "label": c.label,
                "value": c.value,
                "weight": round(c.weight, 4),
                "contribution": c.contribution,
            }
            for c in score.components
        ],
    }


def opportunity_json(
    opp: SwingOpportunity, scanned_at: dt.datetime, status: DataStatus, benchmark: str
) -> JsonDict:
    ev = opp.evaluation
    return {
        "id": opp.id,
        "instrument_id": opp.instrument_id,
        "symbol": opp.symbol,
        "name": opp.name,
        "exchange": "NSE",
        "sector": opp.sector,
        "setup": ev.setup.value,
        "status": ev.status.value,
        "actionable": opp.actionable and status is DataStatus.FRESH,
        "close": opp.close,
        "evidence": [
            {
                "key": e.key,
                "label": e.label,
                "value": _round(e.value),
                "threshold": _round(e.threshold),
                "unit": e.unit,
                "passed": e.passed,
            }
            for e in ev.evidence
        ],
        "trigger": {
            "condition": ev.trigger_condition,
            "level": opp.trigger_level,
            "status": ev.status.value,
            "triggered_on": opp.data_as_of if ev.status.value == "TRIGGERED" else None,
        },
        "entry": {"basis": ev.entry_basis, "price": opp.entry},
        "invalidation": opp.invalidation,
        "targets": [
            {"label": label, "price": price, "r_multiple": r} for label, price, r in opp.targets
        ],
        "risk_per_share": opp.risk_per_share,
        "reward_per_share": opp.reward_per_share,
        "risk_reward": opp.risk_reward,
        "horizon": {"min_sessions": ev.horizon[0], "max_sessions": ev.horizon[1]},
        "liquidity": {
            "avg_turnover_cr_20d": opp.avg_turnover_cr,
            "avg_volume_20d": opp.avg_volume,
        },
        "volatility": {"atr_14": opp.atr, "atr_pct": opp.atr_pct, "hv_20_pct": opp.hv_pct},
        "relative_strength": {
            "benchmark": benchmark,
            "excess_return_pct": opp.rs_excess_pct,
            "lookback": opp.rs_lookback,
        },
        "score": score_json(opp.score),
        "data_as_of": opp.data_as_of,
        "scanned_at": iso(scanned_at),
        "limitations": list(opp.limitations),
    }


def scan_json(scan: SwingScan, benchmark: str = "nifty-500") -> JsonDict:
    fresh = scan.freshness
    counts = scan.result.counts
    return {
        "scan_id": scan.scan_id,
        "scanned_at": iso(scan.scanned_at),
        "data_as_of": fresh.data_as_of,
        "data_status": fresh.status.value,
        "stale_reason": fresh.reason,
        "universe": scan.universe,
        "counts": {
            "universe": counts.universe,
            "evaluated": counts.evaluated,
            "insufficient_history": counts.insufficient_history,
            "qualified": counts.qualified,
            "pending": counts.pending,
            "triggered": counts.triggered,
        },
        "warnings": list(scan.warnings),
        "opportunities": [
            opportunity_json(o, scan.scanned_at, fresh.status, benchmark)
            for o in scan.result.opportunities
        ],
    }


def provider_json(state: ProviderState) -> JsonDict:
    return {"available": state.available, "reason": state.reason, "dependency": state.dependency}


def candidate_json(c: OptionCandidate, status: DataStatus) -> JsonDict:
    lot = Decimal(c.lot_size)
    return {
        "id": c.id,
        "strategy": c.strategy.value,
        "expiry": c.expiry,
        "dte": c.dte,
        "lot_size": c.lot_size,
        "lots": c.lots,
        "actionable": c.actionable and status is DataStatus.FRESH,
        "legs": [
            {
                "tradingsymbol": leg.quote.contract.tradingsymbol,
                "strike": leg.strike,
                "option_type": leg.option_type.value,
                "side": leg.side.value,
                "bid": leg.quote.bid,
                "ask": leg.quote.ask,
                "exec_price": leg.exec_price,
                "quote_age_s": leg.quote.quote_age_s,
                "spread_pct": leg.quote.spread_pct,
                "volume": leg.quote.contract.volume or 0,
                "oi": leg.quote.contract.oi or 0,
                "iv": None if leg.iv is None else round(leg.iv * _PCT, 2),
                "delta": _round(leg.delta, 4),
                "greeks_source": "MODEL_BLACK76" if leg.iv is not None else None,
            }
            for leg in c.legs
        ],
        "net_debit_per_unit": c.net_debit_per_unit.quantize(_Q2),
        "net_debit_per_lot": (c.net_debit_per_unit * lot).quantize(_Q2),
        "max_loss_per_unit": c.max_loss_per_unit.quantize(_Q2),
        "max_loss_per_lot": c.max_loss_per_lot.quantize(_Q2),
        "max_profit_per_unit": (
            None if c.max_profit_per_unit is None else c.max_profit_per_unit.quantize(_Q2)
        ),
        "max_profit_per_lot": (
            None if c.max_profit_per_lot is None else c.max_profit_per_lot.quantize(_Q2)
        ),
        "breakevens": list(c.breakevens),
        "costs": {"entry_inr": c.entry_cost_inr, "round_trip_inr": c.round_trip_cost_inr},
        "max_loss_after_costs_per_lot": c.max_loss_after_costs_per_lot.quantize(_Q2),
        "payoff": [{"underlying": p.underlying, "pnl_per_lot": p.pnl_per_lot} for p in c.payoff],
        "score": score_json(c.score),
        "limitations": list(c.limitations),
    }


def options_json(symbol: str, analysis: OptionsAnalysis) -> JsonDict:
    result = analysis.result
    return {
        "provider": provider_json(analysis.provider),
        "underlying": {
            "symbol": symbol.upper(),
            "spot": analysis.spot,
            "spot_as_of": iso(analysis.spot_as_of),
        },
        "analyzed_at": iso(analysis.analyzed_at),
        "data_status": analysis.status.value,
        "candidates": (
            []
            if result is None
            else [candidate_json(c, analysis.status) for c in result.candidates]
        ),
        "rejected": (
            []
            if result is None
            else [{"tradingsymbol": r.tradingsymbol, "reason": r.reason} for r in result.rejected]
        ),
        "warnings": list(analysis.warnings) + ([] if result is None else list(result.warnings)),
        "model_note": MODEL_NOTE,
    }


def validation_json(result: ValidationResult) -> JsonDict:
    trades = len(result.trades)
    return {
        "available": True,
        "reason": None if trades else "No triggered signals in the period for this universe.",
        "setup": result.setup.value,
        "period": {"start": result.start, "end": result.end},
        "assumptions": list(result.assumptions),
        "trades": trades,
        "wins": result.wins,
        "skipped_gap": result.skipped_gap,
        "hit_rate_pct": _round(result.hit_rate_pct),
        "avg_return_pct": _round(result.avg_return_pct, 3),
        "avg_r": _round(result.avg_r, 3),
        "expectancy_r": _round(result.avg_r, 3),
        "max_adverse_r": _round(result.max_adverse_r, 3),
        "by_year": [
            {"year": y.year, "trades": y.trades, "avg_r": _round(y.avg_r, 3)}
            for y in result.by_year
        ],
    }


def unavailable_validation(setup: str, start: dt.date, end: dt.date, reason: str) -> JsonDict:
    return {
        "available": False,
        "reason": reason,
        "setup": setup,
        "period": {"start": start, "end": end},
        "assumptions": [],
        "trades": 0,
        "wins": 0,
        "skipped_gap": 0,
        "hit_rate_pct": None,
        "avg_return_pct": None,
        "avg_r": None,
        "expectancy_r": None,
        "max_adverse_r": None,
        "by_year": [],
    }


def quote_rules_json(rules: QuoteRules) -> JsonDict:
    return {
        "max_spread_pct": rules.max_spread_pct,
        "max_quote_age_s": rules.max_quote_age_s,
        "min_oi": rules.min_oi,
        "min_volume": rules.min_volume,
    }


LIMITS: Final[JsonDict] = {"max_results": MAX_RESULTS, "max_universe": MAX_UNIVERSE}
