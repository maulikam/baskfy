"""The `MomentumScan` contract — M13 §1, the CSV cord's replacement.

`(as_of, universe) → the desk's columns, under the export's exact names, at the export's
precision.` This is the seam the whole merge was built toward: the desk has always been fed a
weekly CSV exported by hand from momoindiascreener.in, and this produces the same thirty columns
from the merged engine instead.

WHAT "THE DESK'S COLUMNS" MEANS PRECISELY
-----------------------------------------
Not a list restated here. `baskfy_core.score.required(cfg)` is the authority — the desk's own
`load_scan` validates against it — and this module asks it, so a column added to the score cannot
silently go missing from a generated scan.

FOUR COLUMNS ARE CARRIED, NOT COMPUTED
---------------------------------------
`marketcap`, `beta`, `circuits_*` and `is_nifty_fno` cannot be computed from bars alone (see
`reconciliation/DESK-PARITY.md`). `carried` supplies them, and :meth:`MomentumScan.provenance`
names them in every scan this module produces, so a plan built on a generated scan carries the
record of what was borrowed rather than measured.

RE-RESOLVABILITY (M13 §3)
--------------------------
Every scan carries a `screen_run_id`: a digest over the definition, the as-of date and the data
version. Two scans with the same id were built from the same inputs, and an old plan can name the
inputs that produced it. It is deliberately *not* a digest of the output — an output digest tells
you two runs differed without telling you why, and the point is to resolve inputs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from dataclasses import dataclass
from typing import Final

import polars as pl

from .factors import DEFAULT_FACTOR_CONFIG, FactorConfig, compute_factors
from .reference_export import EXPORT_ENCODING, FACTOR_COLUMN_MAP
from .score import ScoringConfig, required

#: An overnight close-to-close move this large is a split or a bonus, not a price move. NSE's
#: 20% circuit band is the ceiling for a genuine one-day move in a liquid name, and the smallest
#: adjustment ratio in ordinary use (5:4 bonus) is a 20% step, so 35% separates the two cleanly.
SPLIT_SIGNATURE: Final = 0.35

#: Columns no bar series can produce. See the module docstring.
CARRIED_COLUMNS: Final[tuple[str, ...]] = (
    "marketcap",
    "beta",
    "circuits_three_months",
    "circuits_one_year",
    "is_nifty_fno",
)

_EXPORT_NAME: Final[dict[str, str]] = {v: k for k, v in FACTOR_COLUMN_MAP.items()}


def screen_run_id(definition: str, as_of: dt.date, data_version: int) -> str:
    """A stable id for one set of inputs. Same inputs, same id, on any machine."""
    digest = hashlib.sha256(f"{definition}|{as_of.isoformat()}|{data_version}".encode())
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class MomentumScan:
    """One scan, and the record of how it was made."""

    as_of: dt.date
    universe: str
    frame: pl.DataFrame
    screen_run_id: str
    data_version: int
    carried: tuple[str, ...]
    suspect_symbols: tuple[str, ...]

    def provenance(self) -> dict[str, object]:
        """What a plan should record about the scan that built it."""
        return {
            "screen_run_id": self.screen_run_id,
            "as_of": self.as_of.isoformat(),
            "universe": self.universe,
            "data_version": self.data_version,
            "carried_columns": list(self.carried),
            "rows": self.frame.height,
            "suspect_symbols": list(self.suspect_symbols),
        }

    def to_csv(self) -> bytes:
        """Exactly what the desk's `load_scan` reads: UTF-8 with a BOM, export column names.

        The BOM is not decoration. `load_scan` opens with `encoding="utf-8-sig"` because the real
        export carries one, and a generated scan that omitted it would differ from an uploaded one
        in its very first byte — which is the kind of difference that shows up as a mangled first
        column name three layers away.
        """
        buffer = io.BytesIO()
        self.frame.write_csv(buffer)
        return buffer.getvalue().decode("utf-8").encode(EXPORT_ENCODING)


def unadjusted_symbols(bars: pl.DataFrame, threshold: float = SPLIT_SIGNATURE) -> tuple[str, ...]:
    """Symbols whose history contains a price step no market makes — a missing adjustment.

    This is not a heuristic looking for something subtle. `corporate_action` holds four rows, so
    most splits and bonuses in the bar history were never applied, and each one leaves a cliff.

    It matters more than a wrong number in a rank. Measured on the 2026-08-18 scan, **every**
    symbol whose `away_from_high_one_year` disagreed with the export by more than ten points had
    one of these steps — and fourteen of them were rejected outright by the `far_from_high` filter,
    because an unadjusted pre-split high makes a stock look 80% below a peak it never reached.
    Contamination that only moved scores would blur the ranking; this removes tradeable names from
    the universe entirely. `NEEDS-MAULIK.md` item 4.
    """
    if bars.is_empty():
        return ()
    stepped = (
        bars.sort(["symbol", "date"])
        .with_columns(pl.col("close").shift(1).over("symbol").alias("_prev"))
        .filter(pl.col("_prev").is_not_null() & (pl.col("_prev") > 0))
        .filter(((pl.col("close") / pl.col("_prev")) - 1).abs() > threshold)
    )
    return tuple(sorted(set(stepped["symbol"].to_list())))


def build(  # noqa: PLR0913 — every one is an input to the scan; a bag would hide them
    bars: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    *,
    cfg: ScoringConfig,
    carried: pl.DataFrame,
    universe: str = "nse_cash",
    data_version: int = 1,
    definition: str = "desk_momentum_v1",
    benchmark: pl.DataFrame | None = None,
    factor_config: FactorConfig = DEFAULT_FACTOR_CONFIG,
) -> MomentumScan:
    """Generate one scan the desk would accept.

    ``carried`` must hold ``symbol`` plus :data:`CARRIED_COLUMNS`; it is required rather than
    defaulted for the same reason ``build_plan``'s ``tradeable`` is (M15, docs/04 §2): a scan
    silently missing `beta` would score, rank and trade, just differently — and nothing would say
    so. Forgetting it is a ``ValueError`` here rather than a quiet reordering later.
    """
    missing_carried = [c for c in ("symbol", *CARRIED_COLUMNS) if c not in carried.columns]
    if missing_carried:
        raise ValueError(f"`carried` is missing {missing_carried}; see CARRIED_COLUMNS")

    computed = compute_factors(bars, as_of, trading_days, benchmark, factor_config).frame

    # `volume` names two different quantities. On a bar it is a share count; in the export it is
    # exchange turnover in rupees (docs/13 §2 finding 5, which is why `FACTOR_COLUMN_MAP` sends
    # `vol_day_val` there). The bar column is dropped so the export's meaning wins — the desk does
    # not read either one, but a frame carrying both under one name is a trap for whoever does.
    renames = {c: _EXPORT_NAME[c] for c in computed.columns if c in _EXPORT_NAME}
    shadowed = [dst for src, dst in renames.items() if dst != src and dst in computed.columns]
    computed = computed.drop(shadowed).rename(renames)

    # Whatever the caller carries wins wherever both sides have it, and the losing column is
    # dropped rather than suffixed. A join that keeps both silently picks one — and it picked the
    # engine's, whose `series` is empty and whose `beta` comes from thirty days of index history
    # rather than the year the desk was traded on. Sixteen symbols were rejected for a null
    # `series` before this line existed, which is how a suffix becomes a trading difference.
    overlapping = [c for c in carried.columns if c != "symbol" and c in computed.columns]
    frame = computed.drop(overlapping).join(carried, on="symbol", how="inner")
    frame = frame.with_columns(pl.lit(as_of.isoformat()).alias("date"))

    columns = required(cfg)
    absent = [c for c in columns if c not in frame.columns]
    if absent:
        raise ValueError(
            f"the engine produced no {absent} — the desk's `load_scan` would reject this scan. "
            f"Either the factor engine lost a column or `FACTOR_COLUMN_MAP` no longer names it."
        )

    return MomentumScan(
        as_of=as_of,
        universe=universe,
        frame=frame.select(columns).sort("symbol"),
        screen_run_id=screen_run_id(definition, as_of, data_version),
        data_version=data_version,
        carried=CARRIED_COLUMNS,
        # Restricted to the scan's own rows. `bars` can carry the whole instrument table — the
        # desk's generate path fetches every symbol it has — and a warning reading "452 of 271
        # symbols" is worse than no warning: it is visibly wrong, so it gets ignored.
        suspect_symbols=tuple(
            s for s in unadjusted_symbols(bars) if s in set(frame["symbol"].to_list())
        ),
    )
