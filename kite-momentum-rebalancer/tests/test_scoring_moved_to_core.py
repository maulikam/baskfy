"""The Momentum Quality Score moved to `baskfy_core.score` at M15, and did not change (P3.1).

M15's acceptance is that the moved code produce **byte-identical** output over the M12 corpus.
That was verified directly at the time, by running the pre-move file out of git alongside the
moved one: both scans, `score()` and `audit()`, `DataFrame.equals` on every cell.

A pre/post comparison cannot survive the move, so this is its durable form — a digest of the
scored frame, committed. It answers the question that actually matters afterwards: *has the score
changed since the move?* If this fails, the strategy's output moved, and that must be deliberate.

The corpus lives under `data/uploads/`, which is untracked (it holds real scans), so these skip
where it is absent rather than pretending to have checked.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from app import scoring

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "scoring_golden.json"
UPLOADS = pathlib.Path("data/uploads")


def _cases() -> list[str]:
    if not GOLDEN.is_file():
        return []
    return sorted(json.loads(GOLDEN.read_text()))


@pytest.mark.parametrize("name", _cases())
def test_the_score_is_unchanged_since_it_moved_to_core(name: str) -> None:
    path = UPLOADS / name
    if not path.is_file():
        pytest.skip(f"{name} is not present; data/uploads is untracked")
    expected = json.loads(GOLDEN.read_text())[name]
    df = scoring.score(scoring.load_scan(str(path)))
    assert (int(df.shape[0]), int(df.shape[1])) == (expected["rows"], expected["cols"])
    digest = hashlib.sha256(df.to_csv(index=False, float_format="%.10g").encode()).hexdigest()
    assert digest == expected["sha256"], (
        "the scored frame changed. If that was deliberate, regenerate "
        "tests/fixtures/scoring_golden.json and say why in the commit — this is the strategy's "
        "output, not an implementation detail."
    )
    ranked = df.dropna(subset=["rank"]).sort_values("rank").head(10)["symbol"].tolist()
    assert ranked == expected["top10"]


def test_the_desk_scores_through_core() -> None:
    """The move is real: the desk's scoring is a binding, not a second copy of the formulas."""
    import baskfy_core.score as core

    assert scoring.score.__module__ == "app.scoring"
    assert core.score.__doc__ is not None
    # The desk's wrapper delegates; the formulas exist once, in core.
    src = pathlib.Path(scoring.__file__).read_text()
    assert "_core.score(u, C)" in src
    assert "A_trend" not in src, "a scoring formula is back in the desk's module"
