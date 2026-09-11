"""`01` §6's numbers are a transcription, so something has to check the transcription.

`baskfy_core.twt.published` exists because three consumers read the study's answers and none of
them should be allowed to copy them out by hand. That only helps if the module itself is checked
against the study's own file — otherwise it is the same transcription with one extra step, and a
digit that drifted here would quietly become the new truth.

The file is `packages/core/tests/fixtures/twt/golden_metrics.json`, TW2's committed byte-for-byte
copy of `research/tight-close/out/final_metrics.json`. `test_twt_goldens.py` proves the copy
whenever `research/` is present; this module proves `published.py` against the copy, and the two
together close the loop.

The four figures that are **not** in `final_metrics.json` — the two gate-off numbers and the two
that describe the shipped ₹5 crore floor — come from `01` §7's sensitivity table, so they are
asserted against `docs/twt/01-method.md` itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.published import PUBLISHED

FIXTURES: Final = Path(__file__).resolve().parent / "fixtures" / "twt"
METHOD: Final = Path(__file__).resolve().parents[4] / "docs" / "twt" / "01-method.md"


def golden() -> dict[str, float | str]:
    payload = json.loads((FIXTURES / "golden_metrics.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


#: `(the field on PUBLISHED, the key in the study's own file)`.
TRANSCRIBED: Final = (
    ("start", "start"),
    ("end", "end"),
    ("years", "years"),
    ("cagr_pct", "cagr_pct"),
    ("max_drawdown_pct", "max_dd_pct"),
    ("calmar", "calmar"),
    ("sharpe", "sharpe"),
    ("trades", "trades"),
    ("win_rate_pct", "win_rate_pct"),
    ("profit_factor", "profit_factor"),
    ("avg_win_pct", "avg_win_pct"),
    ("avg_loss_pct", "avg_loss_pct"),
    ("avg_trade_pct", "avg_ret_pct"),
    ("avg_hold_sessions", "avg_hold"),
    ("exposure_pct", "exposure_pct"),
    ("in_sample_cagr_pct", "is_cagr"),
    ("in_sample_dd_pct", "is_dd"),
    ("out_of_sample_cagr_pct", "oos_cagr"),
    ("out_of_sample_dd_pct", "oos_dd"),
)


@pytest.mark.parametrize(("field", "key"), TRANSCRIBED, ids=[pair[0] for pair in TRANSCRIBED])
def test_the_record_matches_the_studys_own_metrics_file(field: str, key: str) -> None:
    here = getattr(PUBLISHED, field)
    there = golden()[key]
    assert here == type(here)(there), f"published.{field} says {here}; the study says {there}"


def test_the_final_equity_is_the_studys_own_rupees() -> None:
    """Money as a decimal string, house rule 9, even for a number nobody adds anything to."""
    assert float(PUBLISHED.final_equity_inr) == golden()["final_equity"]


def _row_of(marker: str) -> str:
    """The `01` §7 line containing `marker`, with the document's typographic minus made ASCII.

    The prose uses U+2212 MINUS SIGN, because it is prose; a float does not. Normalising here
    rather than writing the sign into the test keeps the assertion about the *number*.
    """
    text = METHOD.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if marker in line)
    return row.replace("\u2212", "-")


def test_the_gate_off_row_is_the_one_01_7_names() -> None:
    """`gate 30 / 35 / 45 / 50 % · none | 18.6 / 20.3 / 13.5 / 14.4 · 17.2 | ... · -43`.

    The gate-off pair is the only argument for the breadth gate, so it is read off the document
    rather than remembered.
    """
    row = _row_of("| gate 30 / 35 / 45 / 50")
    assert f"· {PUBLISHED.gate_off_cagr_pct}" in row
    assert f"· {int(PUBLISHED.gate_off_max_drawdown_pct)}" in row


def test_the_shipped_floors_row_is_the_one_01_7_names() -> None:
    """`no liquidity filter / **turnover ≥ ₹5 cr** | 19.5 / **22.5** | -28 / **-27** | 169 / 169`.

    This is the row TW9's run over the plant should be read against, because `01` §6's headline
    was produced at the research's ₹2 crore floor and the sleeve ships at ₹5 crore. A drift
    measured against the headline contains the floor, and DECISIONS-TW **TW9.3** says by how much.
    """
    numbers = re.findall(r"-?\d+(?:\.\d+)?", _row_of("turnover ≥ ₹5 cr"))
    assert str(PUBLISHED.shipped_floor_cagr_pct) in numbers
    assert str(int(PUBLISHED.shipped_floor_max_drawdown_pct)) in numbers
    assert str(PUBLISHED.shipped_floor_trades) in numbers


def test_nothing_here_is_a_setting() -> None:
    """A measurement that leaked into the config would be a threshold nobody decided on."""
    for group in ("scan", "entry", "sizing", "exits", "breadth", "costs", "backtest"):
        section = getattr(DEFAULT_TWT_CONFIG, group)
        assert not hasattr(section, "cagr_pct")
        assert not hasattr(section, "trades")
