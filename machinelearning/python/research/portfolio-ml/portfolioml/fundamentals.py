"""PM1: point-in-time fundamental features. Frames in, frames out. A snapshot taken at
``available_on`` sees only filings available by then, so a restatement changes history only from
the day it was published."""

from __future__ import annotations

import polars as pl

FEATURES = [
    "rev_q_yoy", "rev_accel", "ttm_rev_yoy", "ttm_pat_yoy", "ebitda_margin_ttm", "pat_margin_ttm",
    "ebitda_margin_chg4q", "pat_margin_chg4q", "debt_equity", "roce", "cfo_pat", "pat_ttm_positive", "is_bank",
]
# EPS growth is deliberately absent: filed EPS is not restated for bonuses and splits, so it steps
# on a bonus issue. PAT growth carries the same signal without the artefact.


def _is_bank(row: dict) -> float | None:
    """From the filing's own format (PM1.12); a name with no filing has no value, as every feature here."""
    url = row.get("source_url")
    return None if url is None else float(url.rsplit("/", 1)[-1].startswith(("BANKING_", "INTEGRATED_FILING_BANKING_")))


def usable(filings: pl.DataFrame, lagged: bool = False) -> pl.DataFrame:
    """PM1.13: Screener's lagged rows stay out unless asked for, and never sit beside an NSE row of the same quarter."""
    if "pit_quality" not in filings.columns:
        return filings
    filed = filings.filter(pl.col("pit_quality") != "lagged")
    if not lagged:
        return filed
    return pl.concat([filed, filings.filter(pl.col("pit_quality") == "lagged").join(filed.select("instrument_id", "period_end").unique(), on=["instrument_id", "period_end"], how="anti")])


def _ttm(q: dict[int, dict], i: int, col: str) -> float | None:
    vals = [q.get(k, {}).get(col) for k in range(i - 3, i + 1)]
    return None if any(v is None for v in vals) else sum(vals)


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b == 0 else a / b


def _growth(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b <= 0 else a / b - 1


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _index(period_end) -> int:
    return period_end.year * 4 + (period_end.month - 1) // 3


def _chosen(known: dict[tuple[int, str], dict]) -> dict[int, dict]:
    """One reporting basis throughout: a year-on-year across a basis change is not a comparison."""
    periods = sorted({i for i, _ in known})[-8:]
    con = sum((i, "consolidated") in known for i in periods)
    basis = "consolidated" if con * 2 >= len(periods) else "standalone"
    return {i: row for (i, kind), row in known.items() if kind == basis}


def _features(q: dict[int, dict]) -> dict[str, float | None]:
    i = max(q)
    latest = q[i]
    rev, pat, ebitda = (_ttm(q, i, c) for c in ("revenue", "pat", "ebitda"))
    rev_4, pat_4, ebitda_4 = (_ttm(q, i - 4, c) for c in ("revenue", "pat", "ebitda"))
    dep = _ttm(q, i, "depreciation")
    q_yoy = _growth(latest.get("revenue"), q.get(i - 4, {}).get("revenue"))
    prior_yoy = _growth(q.get(i - 1, {}).get("revenue"), q.get(i - 5, {}).get("revenue"))
    sheet = next((q[k] for k in range(i, i - 3, -1) if q.get(k, {}).get("total_equity") is not None), None)
    debt, equity = (sheet["total_debt"], sheet["total_equity"]) if sheet else (None, None)
    capital = None if debt is None or equity is None else debt + equity
    ebit = None if ebitda is None or dep is None else ebitda - dep
    year_end = next((k for k in range(i, i - 4, -1) if k % 4 == 0 and q.get(k, {}).get("cfo") is not None), None)
    return {
        "rev_q_yoy": q_yoy,
        "rev_accel": _diff(q_yoy, prior_yoy),
        "ttm_rev_yoy": _growth(rev, rev_4),
        "ttm_pat_yoy": _growth(pat, pat_4),
        "ebitda_margin_ttm": _ratio(ebitda, rev),
        "pat_margin_ttm": _ratio(pat, rev),
        "ebitda_margin_chg4q": _diff(_ratio(ebitda, rev), _ratio(ebitda_4, rev_4)),
        "pat_margin_chg4q": _diff(_ratio(pat, rev), _ratio(pat_4, rev_4)),
        "debt_equity": _ratio(debt, equity),
        "roce": _ratio(ebit, capital),
        "cfo_pat": None if year_end is None else _ratio(q[year_end]["cfo"], _ttm(q, year_end, "pat")),
        "pat_ttm_positive": None if pat is None else float(pat > 0),
        "is_bank": _is_bank(latest),
    }


def snapshots(filings: pl.DataFrame) -> pl.DataFrame:
    """One row per (instrument_id, available_on): the features as they stood that day."""
    out: list[dict] = []
    for (iid,), g in filings.sort("available_on", "filed_at").group_by(["instrument_id"], maintain_order=True):
        known: dict[tuple[int, str], dict] = {}
        rows = g.to_dicts()
        for pos, r in enumerate(rows):
            known[(_index(r["period_end"]), r["period_type"])] = r
            if pos + 1 < len(rows) and rows[pos + 1]["available_on"] == r["available_on"]:
                continue
            out.append({"instrument_id": iid, "available_on": r["available_on"], **_features(_chosen(known))})
    schema = {"instrument_id": pl.Int64, "available_on": pl.Date, **dict.fromkeys(FEATURES, pl.Float64)}
    return pl.DataFrame(out, schema=schema)


def join_features(panel: pl.DataFrame, snaps: pl.DataFrame) -> pl.DataFrame:
    """Each (instrument_id, date) takes the newest snapshot with ``available_on <= date``."""
    joined = panel.sort("date").join_asof(
        snaps.sort("available_on"), left_on="date", right_on="available_on", by="instrument_id",
        strategy="backward",
    )
    return joined.with_columns(
        (pl.col("date") - pl.col("available_on")).dt.total_days().cast(pl.Float32).alias("days_since_filing")
    ).drop("available_on")


def coverage_by_year(joined: pl.DataFrame) -> pl.DataFrame:
    """Share of rows with a TTM figure, by year (PM1 AC2)."""
    return joined.group_by(pl.col("date").dt.year().alias("year")).agg(
        pl.len().alias("rows"), pl.col("pat_margin_ttm").is_not_null().mean().alias("ttm_coverage")
    ).sort("year")
