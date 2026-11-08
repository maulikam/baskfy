"""The stored desk SCORE is this desk's SCORE, row for row (docs/ranking/PLAN.md C2, leaf 2.B).

`baskfy_core.desk_score_service.score_day` is what the nightly worker will write into
`desk_score_daily`, and the screener will rank by that table without ever re-scoring. So the one
question that matters is whether it equals what `/analyze` computes from a generated scan. This
test answers it with the desk's real modules — `app.scan_source.generate` (its database read
replaced by the shared fixture's bars, nothing else), `app.scan_source.as_desk_frame` and
`app.scoring.score`, all bound to `app.config` — against `score_day`, which uses core's
`DESK_CONFIG`. `test_desk_config_parity.py` holds those two configs equal; this holds the outputs.
"""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import pytest

from app import scan_source
from app import scoring
from baskfy_core import desk_score_service as service

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "decile-blueprint" / "packages" / "core" / "tests"
)
sys.path.insert(0, str(FIXTURE_DIR))
import _desk_score_fixture as fixture  # noqa: E402


def _half_up(value: float, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


@pytest.fixture(scope="module")
def book() -> pd.DataFrame:
    days = fixture.trading_days()
    with pytest.MonkeyPatch.context() as patch:
        # The only substitution: where the desk would read the screener's Postgres.
        patch.setattr(scan_source, "_fetch", lambda as_of, symbols: (fixture.bars(), days))
        generated = scan_source.generate(days[-1], fixture.carried())
    return scoring.score(scan_source.as_desk_frame(generated))


@pytest.fixture(scope="module")
def stored() -> pd.DataFrame:
    days = fixture.trading_days()
    return service.score_day(fixture.bars(), days[-1], days, fixture.carried())


def test_the_fixture_exercises_ranked_rejected_and_tied_rows(book: pd.DataFrame) -> None:
    assert book["symbol"].nunique() >= 40
    ranked = book[book["reject"] == ""]
    assert len(ranked) >= 20 and ranked["SCORE"].duplicated().any()
    tokens = {t for reasons in book["reject"] for t in reasons.split(";") if t}
    assert tokens == {
        "below50&200DMA",
        "neg3M&6M",
        "circuits",
        "illiquid",
        "far_from_high",
        "T2T_series",
        "excluded_instrument",
    }


def test_score_day_is_the_desks_score_row_for_row(book: pd.DataFrame, stored: pd.DataFrame) -> None:
    assert stored["symbol"].tolist() == book["symbol"].tolist()
    for got, want in zip(stored.itertuples(index=False), book.itertuples(index=False), strict=True):
        assert got.reject == want.reject, want.symbol
        if want.reject:
            assert pd.isna(got.score) and pd.isna(got.score_rank), want.symbol
            continue
        assert got.score == want.SCORE, want.symbol
        assert got.score_rank == int(want.rank), want.symbol
        for source, target in service.COMPONENT_COLUMNS.items():
            assert getattr(got, target) == _half_up(getattr(want, source), 4), (want.symbol, source)


def test_the_basket_the_desk_would_build_reads_the_same_ranks(
    book: pd.DataFrame, stored: pd.DataFrame
) -> None:
    """The order `build_plan` selects in (`sort_values("rank")` over unrejected rows) is unchanged."""
    desk_order = book[book["reject"] == ""].sort_values("rank")["symbol"].tolist()
    stored_order = stored[stored["reject"] == ""].sort_values("score_rank")["symbol"].tolist()
    assert stored_order == desk_order
