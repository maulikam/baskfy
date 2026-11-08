"""PM2: point-in-time shareholding features. Frames in, frames out. A snapshot taken at
``available_on`` sees only filings available by then."""

from __future__ import annotations

import polars as pl

PCT = ["promoter_pct", "promoter_pledged_pct", "fii_pct", "dii_pct", "mf_pct", "public_pct"]
FEATURES = [
    *PCT, *[f"{c}_chg1q" for c in PCT], *[f"{c}_chg4q" for c in PCT],
    "pledge_rising", "pledge_falling", "promoter_buying",
]
PROMOTER_BUY_PT = 0.5


def _index(period_end) -> int:
    return period_end.year * 4 + (period_end.month - 1) // 3


def _change(q: dict[int, dict], i: int, lag: int, col: str) -> float | None:
    a, b = q[i].get(col), q.get(i - lag, {}).get(col)
    return None if a is None or b is None else a - b


def _flag(on: bool | None) -> float | None:
    return None if on is None else float(on)


def _features(q: dict[int, dict]) -> dict[str, float | None]:
    i = max(q)
    out: dict[str, float | None] = {c: q[i].get(c) for c in PCT}
    for c in PCT:
        out[f"{c}_chg1q"] = _change(q, i, 1, c)
        out[f"{c}_chg4q"] = _change(q, i, 4, c)
    pledge, promoter = out["promoter_pledged_pct_chg1q"], out["promoter_pct_chg1q"]
    out["pledge_rising"] = _flag(None if pledge is None else pledge > 0)
    out["pledge_falling"] = _flag(None if pledge is None else pledge < 0)
    out["promoter_buying"] = _flag(None if promoter is None else promoter >= PROMOTER_BUY_PT)
    return out


def snapshots(filings: pl.DataFrame) -> pl.DataFrame:
    """One row per (instrument_id, available_on): the features as they stood that day."""
    out: list[dict] = []
    for (iid,), g in filings.sort("available_on", "filed_at").group_by(["instrument_id"], maintain_order=True):
        known: dict[int, dict] = {}
        rows = g.to_dicts()
        for pos, r in enumerate(rows):
            known[_index(r["period_end"])] = r
            if pos + 1 < len(rows) and rows[pos + 1]["available_on"] == r["available_on"]:
                continue
            out.append({"instrument_id": iid, "available_on": r["available_on"], **_features(known)})
    schema = {"instrument_id": pl.Int64, "available_on": pl.Date, **dict.fromkeys(FEATURES, pl.Float64)}
    return pl.DataFrame(out, schema=schema)


def join_features(panel: pl.DataFrame, snaps: pl.DataFrame) -> pl.DataFrame:
    """Each (instrument_id, date) takes the newest snapshot with ``available_on <= date``."""
    return panel.sort("date").join_asof(
        snaps.sort("available_on"), left_on="date", right_on="available_on", by="instrument_id",
        strategy="backward",
    ).drop("available_on")


def coverage_by_year(joined: pl.DataFrame) -> pl.DataFrame:
    """Share of rows with a promoter figure, by year (PM2 AC)."""
    return joined.group_by(pl.col("date").dt.year().alias("year")).agg(
        pl.len().alias("rows"), pl.col("promoter_pct").is_not_null().mean().alias("shp_coverage")
    ).sort("year")
