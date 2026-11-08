"""M12's gate, as a harness rather than an anecdote.

`DESK-PARITY.md` was written from a one-off analysis. A parity result nobody can re-run is not a
gate — it cannot tell you whether the engine drifted between M12 and M19, which is exactly the
question the modules after it keep raising. This reproduces the comparison from the committed
corpus and the live database, and prints the delta tables rule 7 reads.

    uv run python reconciliation/desk_parity.py

THE ORACLE
----------
`kite-momentum-rebalancer/data/uploads/scan_1787143663_Investing_001__1_.csv`: 271 real rows,
dated 2026-08-18 — the weekly export a real portfolio was scored, ordered and stopped against.

`sample_scan.csv` is excluded and stays excluded. `tests/make_sample_scan.py` generates it, its own
docstring says to replace it with a real export, and it carries invented rows built to trip filter
branches. Comparing a price engine against invented prices measures nothing.

WHAT IS HELD CONSTANT, AND WHY THAT IS HONEST
---------------------------------------------
Four groups of columns are carried through from the upload rather than computed, because nothing in
this repository can compute them yet: `marketcap` (an optional *input*; nothing fetches
fundamentals), `beta` (needs a year of NIFTY 50 levels; `index_snapshot_daily` holds thirty days),
`circuits_*` (docs/05 §12 is INFERRED) and `is_nifty_fno` (index membership, not a factor).

Holding them constant is what makes the comparison mean something: every remaining difference is
attributable to the factor engine, which is the thing under test. The harness prints the list, so
the concession is visible in the output and not only in this docstring.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import polars as pl

from baskfy_core import momentum_scan
from baskfy_core import score as core_score

ROOT = Path(__file__).resolve().parent.parent
ORACLE = ROOT.parent / "kite-momentum-rebalancer" / "data" / "uploads"
ORACLE_CSV = ORACLE / "scan_1787143663_Investing_001__1_.csv"
DSN = "postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy"

NEAR_TIE = 3  # a rank move of three places or less is what near-tied scores do

#: Carried through from the upload. See the module docstring.
HELD_CONSTANT = (
    "marketcap",
    "beta",
    "circuits_three_months",
    "circuits_one_year",
    "is_nifty_fno",
)


class _DeskConfig:
    """The desk's scoring configuration, imported rather than restated.

    Restating the weights here would let this harness pass while the desk scored differently, which
    is the one failure mode a parity gate must not have.
    """

    def __init__(self) -> None:
        sys.path.insert(0, str(ROOT.parent / "kite-momentum-rebalancer"))
        from app import config as C  # noqa: PLC0415 — importable only after the line above

        self._c = C

    def __getattr__(self, name: str) -> object:
        return getattr(self._c, name)


def read_oracle() -> pd.DataFrame:
    with ORACLE_CSV.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    frame = pd.DataFrame(rows)
    for col in frame.columns:
        if col not in {"symbol", "name", "series", "date"}:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


async def _fetch(symbols: list[str], as_of: dt.date) -> tuple[pl.DataFrame, list[dt.date]]:
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    engine = create_async_engine(DSN)
    try:
        async with engine.connect() as conn:
            bars = (
                await conn.execute(
                    text(
                        "select i.symbol, b.instrument_id, b.date, b.open, b.high, b.low, "
                        "b.close, b.volume, b.close_raw, b.volume_raw "
                        "from ohlcv_daily b join instrument i on i.id = b.instrument_id "
                        "where i.symbol = any(:syms) and b.date <= :as_of "
                        "order by i.symbol, b.date"
                    ),
                    {"syms": symbols, "as_of": as_of},
                )
            ).fetchall()
            days = (
                await conn.execute(
                    text(
                        "select distinct date from ohlcv_daily where date <= :as_of order by date"
                    ),
                    {"as_of": as_of},
                )
            ).fetchall()
    finally:
        await engine.dispose()

    def col(i: int) -> list[float]:
        return [float(r[i]) if r[i] is not None else 0.0 for r in bars]

    frame = pl.DataFrame(
        {
            "symbol": [r[0] for r in bars],
            "instrument_id": [r[1] for r in bars],
            "date": [r[2] for r in bars],
            "open": col(3),
            "high": col(4),
            "low": col(5),
            "close": col(6),
            "volume": col(7),
            "close_raw": col(8),
            "volume_raw": col(9),
        }
    )
    return frame, [d[0] for d in days]


def generate(oracle: pd.DataFrame, as_of: dt.date, cfg: object) -> pd.DataFrame:
    """Build the scan through the real `MomentumScan` contract, not a copy of it.

    This used to inline the generation. Going through `baskfy_core.momentum_scan` means the gate
    tests the code the desk actually runs — a harness that reproduces the pipeline instead of
    calling it can pass while the pipeline is broken.
    """
    symbols = sorted(oracle["symbol"].tolist())
    bars, trading_days = asyncio.run(_fetch(symbols, as_of))
    if bars.is_empty():
        raise SystemExit(
            "no bars in ohlcv_daily for the oracle's symbols — is the database seeded?"
        )

    carried = pl.DataFrame(oracle[["symbol", "series", *HELD_CONSTANT]].to_dict(orient="records"))
    scan = momentum_scan.build(bars, as_of, trading_days, cfg=cfg, carried=carried)
    print(f"screen_run_id: {scan.screen_run_id}  (definition + as_of + data_version)")
    return pd.DataFrame(scan.frame.to_dicts())


def top_n(frame: pd.DataFrame, cfg: object, n: int = 25) -> pd.DataFrame:
    # `score()` merges SCORE and rank back onto the caller's row order rather than sorting, so the
    # ordering has to be taken from `rank` here. Sorting on SCORE instead would silently reorder
    # ties differently from the desk, which is precisely what this harness is meant to detect.
    scored = core_score.score(core_score.apply_filters(frame.copy(), cfg), cfg)
    ordered = scored.dropna(subset=["rank"]).sort_values("rank")
    return ordered.head(n)[["symbol", "SCORE"]].reset_index(drop=True)


def main() -> int:
    cfg = _DeskConfig()
    oracle = read_oracle()
    as_of = dt.date.fromisoformat(str(oracle["date"].iloc[0]))
    print(f"oracle: {ORACLE_CSV.name} — {len(oracle)} rows, as of {as_of}")
    print(f"held constant from the upload: {', '.join(HELD_CONSTANT)}\n")

    generated = generate(oracle, as_of, cfg)
    print(f"generated: {len(generated)} rows through the merged factor engine\n")

    a = top_n(oracle, cfg)
    b = top_n(generated, cfg)
    only_oracle = [s for s in a["symbol"] if s not in set(b["symbol"])]
    only_generated = [s for s in b["symbol"] if s not in set(a["symbol"])]

    print(f"top-25 membership shared: {25 - len(only_oracle)} / 25")
    print(f"only in the uploaded scan : {only_oracle or '—'}")
    print(f"only in the generated scan: {only_generated or '—'}\n")

    rank_a = {s: i + 1 for i, s in enumerate(a["symbol"])}
    rank_b = {s: i + 1 for i, s in enumerate(b["symbol"])}
    deltas = [
        (s, rank_a[s], rank_b[s], rank_b[s] - rank_a[s])
        for s in rank_a
        if s in rank_b and rank_a[s] != rank_b[s]
    ]
    print(
        f"rank deltas within the shared set: {len(deltas)}, "
        f"of which {sum(1 for d in deltas if abs(d[3]) <= NEAR_TIE)} are ±3 or less"
    )
    for sym, ra, rb, d in sorted(deltas, key=lambda x: -abs(x[3]))[:10]:
        print(f"  {sym:<14} #{ra:>2} → #{rb:<2}  ({d:+d})")

    empty = not only_oracle and not only_generated and not deltas
    print(f"\nRULE 7: the top-25 delta table is {'EMPTY — M13 opens' if empty else 'NOT empty'}.")
    return 0 if empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
