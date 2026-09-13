"""The weekly book's Momentum Quality Score for one day, as the book computes it (PLAN.md C2).

WHAT "AS THE BOOK COMPUTES IT" MEANS
------------------------------------
Phase 1 scored the screener's *filtered survivors*, so the 1/99 clips and every percentile inside
`score.score` ran over a different population than the desk's, and the same symbol on the same
day carried two SCOREs. This module runs the desk's own path, step for step, and nothing else:

1. `momentum_scan.build(bars, as_of, trading_days, cfg=DESK_CONFIG, carried=carried)` over the
   whole `nse_cash` scan — `kite-momentum-rebalancer/app/scan_source.py:generate`;
2. into pandas through `to_dicts()` and `drop_duplicates(subset="symbol")` —
   `app/scan_source.py:as_desk_frame`, copied rather than approximated, because a `to_pandas()`
   infers different dtypes for nullable integers and `is_nifty_fno == 1` is a dtype-sensitive
   comparison;
3. `score.score(frame, DESK_CONFIG)` — `app/scoring.py:score`.

Two tests hold it there: `packages/core/tests/test_desk_score_service.py` against a transcription
of those three steps, and `kite-momentum-rebalancer/tests/test_desk_score_parity.py` against the
desk's real modules, `app.scan_source` and `app.scoring` with `app.config`.

WHAT IT ADDS, AND ONLY THAT
---------------------------
* `instrument_id`, looked up from the as-of bars, because the table is keyed by it and the scan is
  keyed by symbol. A symbol that names two instruments on one day is refused, not guessed: the
  book would keep whichever row `drop_duplicates` saw first, and a stored score must not depend on
  the order a query returned rows in.
* storage precision (house rule 8), half-up: `score` 1 dp, A-F and `ext_over_20dma` 4 dp.
* `score_version`, so a stored row names the formula that produced it.

THE `carried` CONTRACT (for the nightly writer)
-----------------------------------------------
`carried` holds `symbol` plus :data:`CARRIED_INPUT_COLUMNS`. The desk takes them from its newest
uploaded scan (`app/main.py:_carried_columns`); the worker takes them from the as-of day's
`factor_daily` row — `series`, `marketcap_cr`, `beta_12m`, `circuits_3m`, `circuits_12m`, and
`universe_mask & nifty-fno` as 0/1 — see docs/DECISIONS-MERGE.md "Ranking 2.B". `series` is not
in `momentum_scan.CARRIED_COLUMNS`, but the desk carries it anyway and so must the worker: the bars
carry no series, the engine's column is then NULL, and a NULL series is never in `REJECT_SERIES`,
so BE/BZ trade-to-trade names would be scored and ranked instead of rejected. It is required here
for that reason. A symbol with bars but no carried row is not in the scan and gets no row.

Pure: no database, no network, no disk, no clock (Law 1).
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import pandas as pd
import polars as pl

from baskfy_core import momentum_scan
from baskfy_core.desk_config import DESK_CONFIG
from baskfy_core.score import score

#: Bumped whenever `score.py`, `momentum_scan.py` or `DESK_CONFIG` changes. The fingerprint that
#: ties this string to those three is pinned in `tests/test_desk_score_service.py`.
DESK_SCORE_VERSION: Final = "desk-score-2026.09.13"

#: The book scores the whole cash market, not an index (`momentum_scan.build`'s default).
DESK_SCORE_UNIVERSE: Final = "nse_cash"

#: What `carried` must hold besides `symbol`: the desk's `_carried_columns` contract.
CARRIED_INPUT_COLUMNS: Final[tuple[str, ...]] = ("series", *momentum_scan.CARRIED_COLUMNS)

#: `score.score` column → `desk_score_daily` column.
COMPONENT_COLUMNS: Final[dict[str, str]] = {
    "A_trend": "a_trend",
    "B_momentum": "b_momentum",
    "C_sharpe": "c_sharpe",
    "D_consistency": "d_consistency",
    "E_liquidity": "e_liquidity",
    "F_penalty": "f_penalty",
    "ext_over_20dma": "ext_over_20dma",
}

#: Storage precision per numeric output column, matching migration 0047's numeric scales.
STORAGE_PLACES: Final[dict[str, int]] = {
    "score": 1,
    **dict.fromkeys(COMPONENT_COLUMNS.values(), 4),
}

#: The frame `score_day` returns, in this order.
OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "symbol",
    "date",
    "score",
    "score_rank",
    *COMPONENT_COLUMNS.values(),
    "reject",
    "score_version",
)


def score_day(
    bars: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    carried: pl.DataFrame,
) -> pd.DataFrame:
    """Every scanned instrument's desk SCORE, rank, A-F and reject reason for ``as_of``.

    ``bars`` is the long adjusted history `momentum_scan.build` needs (``symbol``,
    ``instrument_id``, ``date``, OHLC, ``volume``, ``close_raw``, ``volume_raw``) for the NSE cash
    instruments to score. Rejected rows are returned with ``score`` and ``score_rank`` NA and a
    non-empty ``reject``; unrejected rows are ranked 1..n exactly as the book ranks them,
    including its tie order.
    """
    missing = [c for c in ("symbol", *CARRIED_INPUT_COLUMNS) if c not in carried.columns]
    if missing:
        raise ValueError(
            f"`carried` is missing {missing}; see desk_score_service.CARRIED_INPUT_COLUMNS"
        )

    instrument_ids = _instrument_ids(bars, as_of)
    scan = momentum_scan.build(
        bars,
        as_of,
        trading_days,
        cfg=DESK_CONFIG,
        carried=carried,
        universe=DESK_SCORE_UNIVERSE,
    )
    if scan.frame.is_empty():
        return _empty()

    desk = pd.DataFrame(scan.frame.to_dicts()).drop_duplicates(subset="symbol")
    scored = score(desk.reset_index(drop=True), DESK_CONFIG)
    return project(scored, instrument_ids, as_of)


def project(scored: pd.DataFrame, instrument_ids: dict[str, int], as_of: dt.date) -> pd.DataFrame:
    """Shape `score.score`'s output into `desk_score_daily` rows, at storage precision.

    Separate from :func:`score_day` so the projection's own rules — rejected rows unranked,
    precision, a symbol with no instrument refused — are testable on a hand-built frame.
    """
    unknown = sorted(set(scored["symbol"]) - set(instrument_ids))
    if unknown:
        raise ValueError(f"scored symbols with no instrument_id on {as_of}: {unknown}")

    reject = scored["reject"].astype(str)
    rejected = reject != ""
    out = pd.DataFrame(
        {
            "instrument_id": scored["symbol"].map(instrument_ids).astype("int64"),
            "symbol": scored["symbol"].astype(str),
            "date": as_of,
        }
    )
    out["score"] = _stored(scored["SCORE"].where(~rejected), STORAGE_PLACES["score"])
    out["score_rank"] = scored["rank"].where(~rejected).astype("Int64")
    for source, target in COMPONENT_COLUMNS.items():
        values = scored[source] if source in scored.columns else pd.Series(pd.NA, index=out.index)
        out[target] = _stored(values.where(~rejected), STORAGE_PLACES[target])
    out["reject"] = reject
    out["score_version"] = DESK_SCORE_VERSION
    return out.loc[:, list(OUTPUT_COLUMNS)].reset_index(drop=True)


def _instrument_ids(bars: pl.DataFrame, as_of: dt.date) -> dict[str, int]:
    pairs = bars.filter(pl.col("date") == as_of).select("symbol", "instrument_id").unique()
    shared = pairs.group_by("symbol").len().filter(pl.col("len") > 1)["symbol"].to_list()
    if shared:
        raise ValueError(
            f"symbols naming more than one instrument on {as_of}: {sorted(shared)}. The book "
            f"scores by symbol; pass one exchange's instruments (NSE cash) so each is unique."
        )
    return {str(s): int(i) for s, i in pairs.iter_rows()}


def _stored(values: pd.Series, places: int) -> pd.Series:
    """Half-up to ``places`` decimals (precision.py's rule), NA where missing or not finite.

    Not `precision.quantise`, which only knows the 0/2/4/10 places of `factor_daily`; SCORE is
    stored at one.
    """
    exponent = Decimal(1).scaleb(-places)
    rounded: list[float | None] = []
    for value in values.tolist():
        if value is None or pd.isna(value):
            rounded.append(None)
            continue
        number = Decimal(str(value))
        rounded.append(
            float(number.quantize(exponent, rounding=ROUND_HALF_UP)) if number.is_finite() else None
        )
    return pd.Series(rounded, index=values.index, dtype="Float64")


def _empty() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in OUTPUT_COLUMNS})
