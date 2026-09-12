"""``python -m baskfy_worker.reconcile_cli`` — Prompt 19 §4's report, as a command.

The checks themselves are tested in ``packages/core/tests/test_reconcile.py``. This file covers
the command: its exit code, its two output formats, symbol filtering, the committed report
staying current, and the ``--bars`` recomputation path — which nothing in this repository can
feed with real history, and which is therefore exercised against a synthetic panel.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl
import pytest

from baskfy_core.factors import compute_factors
from baskfy_core.reconcile import reconcile
from baskfy_core.reference_export import EXPORT_COLUMNS, read_export
from baskfy_worker.reconcile_cli import main, recompute_against_bars, render

REPO_ROOT = Path(__file__).resolve().parents[3]
# `packages/core/tests` is a test-support tree, not an installed package; the cross-validation
# corpus lives there and this module reuses it rather than building a second synthetic panel.
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "tests"))

from factor_corpus import corpus_calendar, corpus_frame  # noqa: E402

COMMITTED_REPORT = REPO_ROOT / "reconciliation" / "REPORT.md"
AS_OF = dt.date(2026, 8, 18)


class TestTheCommand:
    def test_it_exits_zero_on_the_committed_export(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "0 disagreement(s) over tolerance." in out
        assert "UNRESOLVED" in out, "the two INFERRED areas must always be surfaced"

    def test_it_says_that_no_factor_was_recomputed_from_bars(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The single most important honesty line in the report."""
        main([])
        assert "no factor was recomputed from bars in this run" in capsys.readouterr().out

    def test_symbol_filtering(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--symbols", "CUPID", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        sharpe = next(c for c in payload["checks"] if c["name"] == "sharpe_identity")
        assert sharpe["cells"] == 5  # one instrument x five windows
        # The distributional check must decline rather than invent a disagreement from one row.
        turnover = next(c for c in payload["checks"] if c["name"] == "turnover_is_exchange_value")
        assert turnover["clean"]
        assert "below the 30" in turnover["notes"][0]

    def test_an_unknown_symbol_is_an_error_not_an_empty_clean_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--symbols", "NOTALISTING"]) == 2
        assert "none of" in capsys.readouterr().err

    def test_json_payload_is_serialisable_and_complete(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["as_of"] == AS_OF.isoformat()
        assert payload["disagreements"] == 0
        names = {c["name"] for c in payload["checks"]}
        assert {"skip_month_momentum", "circuit_detection_rule"} <= names

    def test_the_committed_report_is_current(self) -> None:
        """Same reason ``openapi.json`` has a staleness gate: a stale artefact is a lie.

        Every input is committed, so this is deterministic.
        """
        assert COMMITTED_REPORT.is_file(), f"{COMMITTED_REPORT} is missing; run `make reconcile`"
        export = read_export(
            REPO_ROOT / "tests" / "fixtures" / "reference-screen-export-2026-08-18.csv"
        )
        expected = render(reconcile(export), limit=25) + "\n"
        assert COMMITTED_REPORT.read_text(encoding="utf-8") == expected, (
            "reconciliation/REPORT.md is stale — re-run `make reconcile`"
        )


def _synthetic_export(bars: pl.DataFrame, calendar: list[dt.date]) -> pl.DataFrame:
    """An export-shaped frame built from our *own* computed factors.

    Circular by design: it makes the recomputation path's plumbing testable (joining, mapping,
    rounding, tolerance) without pretending to validate the formulas, which is what the reference
    export is for.
    """
    computed = compute_factors(bars, AS_OF, calendar).frame
    symbols = dict(zip(bars["instrument_id"].to_list(), bars["symbol"].to_list(), strict=True))
    suffix = {12: "one_year", 9: "nine_months", 6: "six_months", 3: "three_months", 1: "one_month"}

    rows: list[dict[str, object]] = []
    for row in computed.iter_rows(named=True):
        record: dict[str, object] = dict.fromkeys(EXPORT_COLUMNS, 0)
        record["symbol"] = symbols[row["instrument_id"]]
        record["name"] = symbols[row["instrument_id"]]
        record["series"] = "EQ"
        record["date"] = AS_OF.isoformat()
        record["close"] = row["close"]
        for months, label in suffix.items():
            record[f"absolute_return_{label}"] = row[f"ret_{months}m"]
            record[f"sharpe_return_{label}"] = row[f"sharpe_{months}m"]
            record[f"rsi_{label}"] = row[f"rsi_{months}m"]
            record[f"volatility_{label}"] = row[f"vol_{months}m"]
            record[f"circuits_{label}"] = row[f"circuits_{months}m"]
            record[f"positive_days_percent_{label}"] = row[f"pos_days_{months}m"]
        record["beta"] = row["beta_12m"]
        record["high_one_year"] = row["high_1y"]
        record["high_all_time"] = row["high_ath"]
        record["away_from_high_one_year"] = row["away_high_1y"]
        record["away_from_high_all_time"] = row["away_high_ath"]
        for k in (20, 50, 100, 200):
            record[f"ma_{k}"] = row[f"ma_{k}"]
        record["median_volume_one_year"] = row["median_vol_12m"]
        rows.append(record)
    return pl.DataFrame(rows, strict=False)


class TestRecomputationMode:
    """docs/13 §5 step 2's plumbing. NEVER RUN AGAINST REAL HISTORY — see the module docstring."""

    @staticmethod
    @pytest.fixture(scope="class")
    def panel() -> tuple[pl.DataFrame, pl.DataFrame]:
        bars = corpus_frame()
        return bars, _synthetic_export(bars, corpus_calendar(bars))

    def test_a_matching_panel_reconciles_with_no_disagreements(
        self, panel: tuple[pl.DataFrame, pl.DataFrame]
    ) -> None:
        bars, export = panel
        result = recompute_against_bars(export, bars)
        assert result.cells > 0
        assert not result.disagreements, result.disagreements[:5]

    def test_one_wrong_cell_is_reported(self, panel: tuple[pl.DataFrame, pl.DataFrame]) -> None:
        bars, export = panel
        broken = export.with_columns(
            pl.when(pl.col("symbol") == export["symbol"][0])
            .then(pl.col("absolute_return_one_year") + 1.0)
            .otherwise(pl.col("absolute_return_one_year"))
            .alias("absolute_return_one_year")
        )
        result = recompute_against_bars(broken, bars)
        assert len(result.disagreements) == 1
        assert result.disagreements[0].column == "absolute_return_one_year"

    def test_a_symbol_with_no_bars_is_reported_not_skipped(
        self, panel: tuple[pl.DataFrame, pl.DataFrame]
    ) -> None:
        bars, export = panel
        thinner = bars.filter(pl.col("symbol") != export["symbol"][0])
        result = recompute_against_bars(export, thinner)
        assert any("had no bars" in line for line in result.unresolved)

    def test_the_skip_month_columns_are_named_as_unreconcilable(
        self, panel: tuple[pl.DataFrame, pl.DataFrame]
    ) -> None:
        bars, export = panel
        result = recompute_against_bars(export, bars)
        assert any("ret_12m_minus_1m" in line for line in result.unresolved)
