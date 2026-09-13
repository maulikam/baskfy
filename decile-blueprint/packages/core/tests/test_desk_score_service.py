"""`desk_score_service.score_day` is the book's SCORE, not a second one (docs/ranking/PLAN.md C2).

The spec is the desk's own path, so the parity test below transcribes it from
`kite-momentum-rebalancer/app/scan_source.py` (`generate` then `as_desk_frame`) and
`app/scoring.py` (`score`) line for line, and compares every row. The desk's tree runs the same
comparison against its real modules in `tests/test_desk_score_parity.py`; this one keeps the gate
inside the decile suite, where a core change is made.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _desk_score_fixture as fixture

from baskfy_core import desk_score_service as service
from baskfy_core import momentum_scan, ranking, score
from baskfy_core.desk_config import DESK_CONFIG, SCORING_ATTRIBUTES, DeskConfig, canonical

CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "baskfy_core"

#: Every token `score.apply_filters` can write, read from its source so a new one is not missed.
_FILTERS = (CORE_SRC / "score.py").read_text().split("def apply_filters")[1].split("\ndef ")[0]
REJECT_TOKENS = tuple(sorted(set(re.findall(r'"([A-Za-z0-9&_]+);"', _FILTERS))))

#: DESK_SCORE_VERSION -> sha256 over score.py, momentum_scan.py and canonical(DESK_CONFIG).
#: When this test fails, the scoring formula, the scan or the book's configuration changed: bump
#: DESK_SCORE_VERSION in desk_score_service.py and pin the new fingerprint here under the new
#: version. Never re-pin the old version to a new fingerprint - stored rows name that version.
PINNED_FINGERPRINTS: dict[str, str] = {
    "desk-score-2026.09.13": "72aefca21426adbab56884b312db996e9bf93aa3a515f1718d7510b68ca1a89d",
}


@pytest.fixture(scope="module")
def days() -> list[dt.date]:
    return fixture.trading_days()


@pytest.fixture(scope="module")
def result(days: list[dt.date]) -> pd.DataFrame:
    return service.score_day(fixture.bars(), days[-1], days, fixture.carried())


@pytest.fixture(scope="module")
def book(days: list[dt.date]) -> pd.DataFrame:
    """The desk's path, transcribed: `scan_source.generate`, `as_desk_frame`, `scoring.score`."""
    scan = momentum_scan.build(
        fixture.bars(), days[-1], days, cfg=DESK_CONFIG, carried=fixture.carried()
    )
    frame = pd.DataFrame(scan.frame.to_dicts())
    frame = frame.drop_duplicates(subset="symbol").reset_index(drop=True)
    return score.score(frame, DESK_CONFIG)


# ---------------------------------------------------------------------------------------------
# The fixture is what the gate says it is
# ---------------------------------------------------------------------------------------------
def test_fixture_is_a_real_cross_section(days: list[dt.date]) -> None:
    bars = fixture.bars()
    assert bars["symbol"].n_unique() >= 40
    assert len(days) >= 300


def test_fixture_trips_every_reject_token(book: pd.DataFrame) -> None:
    assert len(REJECT_TOKENS) == 7  # the regex above found every filter in score.py
    seen = {token for reasons in book["reject"] for token in reasons.split(";") if token}
    assert seen == set(REJECT_TOKENS)


def test_fixture_has_a_score_tie_and_enough_ranked_names(book: pd.DataFrame) -> None:
    ranked = book[book["reject"] == ""]
    assert len(ranked) >= 20
    assert ranked["SCORE"].duplicated().any()


# ---------------------------------------------------------------------------------------------
# G2: row-for-row parity with the book
# ---------------------------------------------------------------------------------------------
def _half_up(value: float, places: int) -> float:
    from decimal import ROUND_HALF_UP, Decimal  # noqa: PLC0415

    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _missing(value: object) -> bool:
    return value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value))


def test_score_day_equals_the_books_path_row_for_row(
    result: pd.DataFrame, book: pd.DataFrame
) -> None:
    assert result["symbol"].tolist() == book["symbol"].tolist()
    assert result["reject"].tolist() == book["reject"].tolist()
    got_rows = result.to_dict("records")
    want_rows = book.to_dict("records")
    for got, want in zip(got_rows, want_rows, strict=True):
        symbol = want["symbol"]
        if want["reject"]:
            assert _missing(got["score"]), symbol
            assert _missing(got["score_rank"]), symbol
            continue
        assert got["score"] == want["SCORE"], symbol
        assert got["score_rank"] == int(want["rank"]), symbol
        for source, target in service.COMPONENT_COLUMNS.items():
            expected = float(want[source])
            assert got[target] == _half_up(expected, 4), (symbol, source)
            # and rounding moved nothing by more than the storage step
            assert abs(float(got[target]) - expected) <= 0.5e-4 + 1e-12


def test_ties_keep_the_books_order(result: pd.DataFrame, book: pd.DataFrame) -> None:
    twins = result.set_index("symbol").loc[["TWINA", "TWINB"]]
    assert twins["score"].nunique() == 1
    assert (
        twins["score_rank"].tolist()
        == book.set_index("symbol").loc[["TWINA", "TWINB"], "rank"].astype(int).tolist()
    )


def test_every_row_is_stamped_and_keyed(result: pd.DataFrame, days: list[dt.date]) -> None:
    assert list(result.columns) == list(service.OUTPUT_COLUMNS)
    assert (result["score_version"] == service.DESK_SCORE_VERSION).all()
    assert (result["date"] == days[-1]).all()
    ids = dict(fixture.bars().select("symbol", "instrument_id").unique().iter_rows())
    assert result["instrument_id"].tolist() == [ids[s] for s in result["symbol"]]
    assert not result["instrument_id"].duplicated().any()


def test_a_symbol_without_carried_columns_is_not_scanned(result: pd.DataFrame) -> None:
    """The scan's inner join drops it, in the book and therefore here."""
    assert "NOCARRY" not in set(result["symbol"])


def test_series_is_required_in_carried(days: list[dt.date]) -> None:
    """Without it BE/BZ names would be ranked (a NULL series is never in REJECT_SERIES)."""
    with pytest.raises(ValueError, match="series"):
        service.score_day(fixture.bars(), days[-1], days, fixture.carried().drop("series"))


def test_a_symbol_naming_two_instruments_is_refused(days: list[dt.date]) -> None:
    bars = fixture.bars()
    clone = bars.filter(pl.col("symbol") == "MOM01").with_columns(
        pl.lit(9999, dtype=bars.schema["instrument_id"]).alias("instrument_id")
    )
    with pytest.raises(ValueError, match="MOM01"):
        service.score_day(pl.concat([bars, clone]), days[-1], days, fixture.carried())


def test_a_day_with_no_scan_rows_is_an_empty_frame(days: list[dt.date]) -> None:
    empty_carried = fixture.carried().filter(pl.col("symbol") == "nobody")
    out = service.score_day(fixture.bars(), days[-1], days, empty_carried)
    assert out.empty
    assert list(out.columns) == list(service.OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------------------------
# G5: rejected names are never ranked
# ---------------------------------------------------------------------------------------------
def test_reject_rows_carry_null_score_and_null_rank(result: pd.DataFrame) -> None:
    rejected = result[result["reject"] != ""]
    assert len(rejected) >= 7
    assert rejected["score"].isna().all()
    assert rejected["score_rank"].isna().all()
    parts = [*service.COMPONENT_COLUMNS.values()]
    assert rejected[parts].isna().all().all()


def test_reject_free_rows_are_ranked_one_to_n_without_gaps(result: pd.DataFrame) -> None:
    eligible = result[result["reject"] == ""]
    assert sorted(eligible["score_rank"].tolist()) == list(range(1, len(eligible) + 1))
    by_rank = eligible.sort_values("score_rank")
    assert by_rank["score"].is_monotonic_decreasing


def test_reject_projection_never_ranks_a_rejected_row_even_if_the_input_does() -> None:
    """The projection's own rule, on a hand-built frame where a rejected row arrives ranked."""
    scored = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC"],
            "reject": ["", "illiquid;", ""],
            "SCORE": [71.25, 60.0, np.nan],
            "rank": [1.0, 2.0, 2.0],
            **{c: [1.00005, 2.0, np.nan] for c in service.COMPONENT_COLUMNS},
        }
    )
    out = service.project(scored, {"AAA": 1, "BBB": 2, "CCC": 3}, dt.date(2026, 9, 11))
    assert out.loc[1, ["score", "score_rank"]].isna().all()
    # half-up at storage precision, not banker's rounding
    assert out.loc[0, "score"] == 71.3
    assert out.loc[0, "a_trend"] == 1.0001
    # an unrejected row with no SCORE keeps the book's rank and stores a NULL score
    rows = out.to_dict("records")
    assert _missing(rows[2]["score"])
    assert rows[2]["score_rank"] == 2


def test_projection_refuses_a_symbol_with_no_instrument() -> None:
    scored = pd.DataFrame({"symbol": ["AAA"], "reject": [""], "SCORE": [1.0], "rank": [1.0]})
    with pytest.raises(ValueError, match="AAA"):
        service.project(scored, {}, dt.date(2026, 9, 11))


# ---------------------------------------------------------------------------------------------
# G3: the version cannot drift silently
# ---------------------------------------------------------------------------------------------
def fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("score.py", "momentum_scan.py"):
        digest.update(name.encode())
        digest.update((CORE_SRC / name).read_bytes())
    digest.update(canonical(DESK_CONFIG).encode())
    return digest.hexdigest()


def test_version_is_pinned_to_score_scan_and_config() -> None:
    current = fingerprint()
    pinned = PINNED_FINGERPRINTS.get(service.DESK_SCORE_VERSION)
    assert pinned == current, (
        "score.py, momentum_scan.py or DESK_CONFIG changed since DESK_SCORE_VERSION "
        f"{service.DESK_SCORE_VERSION!r} was pinned: bump DESK_SCORE_VERSION and pin "
        f"{current!r} under the new version in PINNED_FINGERPRINTS."
    )


def test_version_fingerprint_sees_a_config_change() -> None:
    changed = DeskConfig(MIN_MEDIAN_DAILY_VALUE=4e7)
    assert canonical(changed) != canonical(DESK_CONFIG)
    reordered = DeskConfig(MOMENTUM_BLEND=dict(reversed(DESK_CONFIG.MOMENTUM_BLEND.items())))
    assert canonical(reordered) != canonical(DESK_CONFIG)


def test_version_constant_has_the_contract_shape() -> None:
    assert re.fullmatch(r"desk-score-\d{4}\.\d{2}\.\d{2}(\.\d+)?", service.DESK_SCORE_VERSION)
    assert len(service.DESK_SCORE_VERSION) <= 32  # desk_score_daily.score_version varchar(32)


# ---------------------------------------------------------------------------------------------
# DESK_CONFIG in core, and the screener's hand copy retired
# ---------------------------------------------------------------------------------------------
def test_desk_config_carries_every_scoring_attribute() -> None:
    for name in SCORING_ATTRIBUTES:
        assert hasattr(DESK_CONFIG, name), name


def test_ranking_defaults_are_desk_config() -> None:
    assert ranking.DEFAULT_DESK_SCORING_CONFIG is DESK_CONFIG
    assert canonical(ranking.DeskScoringDefaults()) == canonical(DESK_CONFIG)


# ---------------------------------------------------------------------------------------------
# Law 1
# ---------------------------------------------------------------------------------------------
FORBIDDEN = (
    re.compile(r"^\s*(from|import)\s+(sqlalchemy|httpx|requests|asyncpg|psycopg|redis|celery)"),
    re.compile(r"^\s*(from|import)\s+(os|pathlib|socket|subprocess|asyncio|io)\b"),
    re.compile(r"\.(now|today|utcnow)\(\)"),
    re.compile(r"\bopen\("),
    re.compile(r"\b(read_csv|read_parquet|write_csv|to_csv)\b"),
)


@pytest.mark.parametrize("name", ["desk_score_service.py", "desk_config.py"])
def test_law1_service_touches_nothing(name: str) -> None:
    offenders = [
        f"{name}:{number}: {line.strip()}"
        for number, line in enumerate((CORE_SRC / name).read_text().splitlines(), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]
    assert offenders == []
