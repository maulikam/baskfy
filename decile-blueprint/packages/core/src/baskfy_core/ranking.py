"""Ranking modes, scopes, preferences, presets, and the desk-score adapter.

Product direction (docs/ranking/PLAN.md, 13 Sep 2026): Baskfy ranks with explainable presets,
not only a longer Sort By dropdown. This module is the contract for that engine.

Hard rules
----------
* Desk SCORE / A-F come from :func:`baskfy_core.score.score` only. Reimplementing them in the
  screener is forbidden — subtle drift in windows, percentiles or rounding would invent a second
  book.
* Stock quality rank and portfolio selection stay separate. Nothing here reads holdings.
* Excess return versus a *common* index is not a rank key (identical subtraction does not reorder).
  Sector-relative and residual momentum can reorder; those land in research leaves, not here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, SupportsFloat

import pandas as pd

from baskfy_core.desk_config import DESK_CONFIG, DeskConfig
from baskfy_core.reference_export import FACTOR_COLUMN_MAP
from baskfy_core.score import ScoringConfig, score
from baskfy_core.universes import UNIVERSE_BY_SLUG

#: Sort By key for the book's Momentum Quality Score (docs/ranking/PLAN.md Phase 1.2).
DESK_SCORE_KEY: Final = "desk_score"

#: Result-value keys for A-F explainability when ``sort_by=desk_score``.
DESK_SCORE_EXPLAIN_COLUMNS: Final[tuple[str, ...]] = (
    "desk_a_trend",
    "desk_b_momentum",
    "desk_c_sharpe",
    "desk_d_consistency",
    "desk_e_liquidity",
    "desk_f_penalty",
    "desk_reject",
    "desk_eligible",
)

_NIFTY_FNO_MASK: Final = UNIVERSE_BY_SLUG["nifty-fno"].mask_value

#: ``factor_daily`` (or export) columns the desk scorer needs after rename.
DESK_SCORE_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "series",
    "close",
    "marketcap_cr",
    "median_vol_12m",
    "rsi_1m",
    "vol_12m",
    "beta_12m",
    "circuits_3m",
    "circuits_12m",
    "pos_days_3m",
    "pos_days_6m",
    "ma_20",
    "ma_50",
    "ma_100",
    "ma_200",
    "away_high_1y",
    "universe_mask",
    "ret_1m",
    "ret_3m",
    "ret_6m",
    "ret_9m",
    "ret_12m",
    "sharpe_1m",
    "sharpe_3m",
    "sharpe_6m",
    "sharpe_9m",
    "sharpe_12m",
)


#: Book scoring knobs. Phase 1 kept a hand copy of ``kite-momentum-rebalancer/app/config.py`` here
#: that no test tied to the desk; the values now live once, in :mod:`baskfy_core.desk_config`,
#: where ``kite-momentum-rebalancer/tests/test_desk_config_parity.py`` holds them to the desk.
#: The old name stays so nothing importing it breaks.
DeskScoringDefaults = DeskConfig

#: Default config for screener ``sort_by=desk_score``. Duck-types as :class:`ScoringConfig`.
DEFAULT_DESK_SCORING_CONFIG: Final = DESK_CONFIG


class FactorPreference(StrEnum):
    """How a factor should be used when ranking or filtering.

    * ``higher`` / ``lower`` — monotonic sort preference (Sort By direction still overrides).
    * ``target_range`` — larger is not better; prefer a band (e.g. ATR-normalised extension).
    * ``eligibility`` — gate only; never a sort key by itself.
    """

    HIGHER = "higher"
    LOWER = "lower"
    TARGET_RANGE = "target_range"
    ELIGIBILITY = "eligibility"


class RankingMode(StrEnum):
    """docs/ranking/PLAN.md §modes.

    * ``single`` — one metric; extra factors must be off.
    * ``sequential`` — primary then tie-breakers (ORDER BY f1, f2, f3). Continuous scores often
      leave secondary keys unused except on exact ties — that is intentional and must stay visible.
    * ``composite`` — sum of per-factor ranks (docs/01 §2.12 / docs/06 today). Default.
    """

    SINGLE = "single"
    SEQUENTIAL = "sequential"
    COMPOSITE = "composite"


class RankingScope(StrEnum):
    """Where percentiles / row numbers are computed.

    * ``filtered_results`` — today's docs/06 order: filters, then rank survivors.
    * ``fixed_universe`` — rank across the selected universe (after apply_filters_on), then apply
      screen filters so adding a filter does not silently rewrite scores.
    * ``within_sector`` — reserved until sector membership is a first-class column.
    """

    FILTERED_RESULTS = "filtered_results"
    FIXED_UNIVERSE = "fixed_universe"
    WITHIN_SECTOR = "within_sector"


DEFAULT_RANKING_MODE: Final = RankingMode.COMPOSITE
DEFAULT_RANKING_SCOPE: Final = RankingScope.FILTERED_RESULTS


@dataclass(frozen=True, slots=True)
class DeskScoreBreakdown:
    """Explainable desk score for one symbol — total, A-F, eligibility."""

    symbol: str
    score: float | None
    rank: float | None
    a_trend: float | None
    b_momentum: float | None
    c_sharpe: float | None
    d_consistency: float | None
    e_liquidity: float | None
    f_penalty: float | None
    reject: str
    eligible: bool


def factor_daily_to_desk_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename ``factor_daily``-shaped columns to the desk export names ``score`` expects.

    Unknown columns are left alone. Required desk columns that are still missing after rename are
    the caller's problem — :func:`score` will fail loudly rather than invent zeros.
    """
    rename = {
        internal: export
        for export, internal in FACTOR_COLUMN_MAP.items()
        if internal in frame.columns and export not in frame.columns
    }
    out = frame.rename(columns=rename)
    return out


def score_universe(frame: pd.DataFrame, cfg: ScoringConfig) -> pd.DataFrame:
    """Run the book's scorer on a desk-shaped frame.

    The input may use either export names or ``factor_daily`` names; both are accepted so the
    screener and the desk share one path.
    """
    desk = factor_daily_to_desk_frame(frame)
    return score(desk, cfg)


def breakdowns(scored: pd.DataFrame) -> list[DeskScoreBreakdown]:
    """One explainable row per symbol from a frame that ``score`` has already annotated."""
    rows: list[DeskScoreBreakdown] = []
    for row in scored.itertuples(index=False):
        reject = str(getattr(row, "reject", "") or "")
        total = getattr(row, "SCORE", None)
        rows.append(
            DeskScoreBreakdown(
                symbol=str(row.symbol),
                score=None if pd.isna(total) else float(total),
                rank=_optional_float(getattr(row, "rank", None)),
                a_trend=_optional_float(getattr(row, "A_trend", None)),
                b_momentum=_optional_float(getattr(row, "B_momentum", None)),
                c_sharpe=_optional_float(getattr(row, "C_sharpe", None)),
                d_consistency=_optional_float(getattr(row, "D_consistency", None)),
                e_liquidity=_optional_float(getattr(row, "E_liquidity", None)),
                f_penalty=_optional_float(getattr(row, "F_penalty", None)),
                reject=reject,
                eligible=reject == "" and not pd.isna(total),
            )
        )
    return rows


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if pd.isna(number) else number
    if isinstance(value, str) and value.strip() == "":
        return None
    if not isinstance(value, str | SupportsFloat):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def is_desk_score_factor(key: str) -> bool:
    """True for the computed desk SCORE Sort By key (not a SQL RANK expression)."""
    return key == DESK_SCORE_KEY


def ensure_nifty_fno_flag(frame: pd.DataFrame) -> pd.DataFrame:
    """Derive ``is_nifty_fno`` from ``universe_mask`` when the export flag is absent."""
    if "is_nifty_fno" in frame.columns:
        return frame
    if "universe_mask" not in frame.columns:
        raise ValueError("desk SCORE needs is_nifty_fno or universe_mask on the survivor frame")
    out = frame.copy()
    out["is_nifty_fno"] = (
        (out["universe_mask"].fillna(0).astype("int64").to_numpy() & _NIFTY_FNO_MASK)
        .astype(bool)
        .astype(int)
    )
    return out


def rerank_survivors_by_desk_score(
    survivors: pd.DataFrame,
    *,
    cfg: ScoringConfig | None = None,
    direction: str = "desc",
    limit: int | None = None,
    as_of: dt.date | None = None,
) -> tuple[pd.DataFrame, list[DeskScoreBreakdown]]:
    """Order filtered survivors by the book's SCORE and attach A-F explain columns.

    Pure: no database, no network, no clock. Percentiles inside ``score`` are over this
    survivor set (after the screener's own filters), matching how the book scores a scan.
    """
    if direction not in ("asc", "desc"):
        raise ValueError(f"direction must be 'asc' or 'desc', got {direction!r}")
    scoring_cfg: ScoringConfig = cfg if cfg is not None else DEFAULT_DESK_SCORING_CONFIG
    frame = survivors.copy()
    if "date" not in frame.columns:
        if as_of is None:
            raise ValueError("as_of is required when survivors have no date column")
        frame["date"] = as_of.isoformat()
    frame = ensure_nifty_fno_flag(frame)
    scored = score_universe(frame, scoring_cfg)

    reject = scored["reject"].fillna("").astype(str)
    eligible_mask = reject == ""
    ascending = direction == "asc"
    # mergesort is stable so equal SCORE keeps input order (instrument_id from SQL).
    eligible = scored.loc[eligible_mask].sort_values(
        "SCORE", ascending=ascending, kind="mergesort", na_position="last"
    )
    rejected = scored.loc[~eligible_mask]
    ordered = pd.concat([eligible, rejected], ignore_index=True)
    ordered = ordered.head(limit).copy() if limit is not None else ordered.copy()

    ordered["sorting_factor"] = ordered["SCORE"]
    ordered["desk_a_trend"] = ordered["A_trend"] if "A_trend" in ordered.columns else pd.NA
    ordered["desk_b_momentum"] = ordered["B_momentum"] if "B_momentum" in ordered.columns else pd.NA
    ordered["desk_c_sharpe"] = ordered["C_sharpe"] if "C_sharpe" in ordered.columns else pd.NA
    ordered["desk_d_consistency"] = (
        ordered["D_consistency"] if "D_consistency" in ordered.columns else pd.NA
    )
    ordered["desk_e_liquidity"] = (
        ordered["E_liquidity"] if "E_liquidity" in ordered.columns else pd.NA
    )
    ordered["desk_f_penalty"] = ordered["F_penalty"] if "F_penalty" in ordered.columns else pd.NA
    ordered["desk_reject"] = ordered["reject"].fillna("").astype(str)
    ordered["desk_eligible"] = ordered["desk_reject"] == ""
    ordered["screen_rank"] = range(1, len(ordered) + 1)

    return ordered, breakdowns(ordered)


#: Named presets — Phase 1 ships the list; weighted composites land with validation.
RANKING_PRESETS: Final[dict[str, str]] = {
    "desk_quality": "Weekly book's Momentum Quality Score (A-F) via baskfy_core.score",
    "path_quality": "Positive-days and path measures (promote only with drawdown pairing)",
    "trend_structure": "MA stack and MA distance (distance is target-range, not higher-is-better)",
    "participation": "Volume expansion versus a preceding baseline",
}
