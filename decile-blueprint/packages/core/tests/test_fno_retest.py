"""``docs/fno/04`` §6 and ``06`` FO9 — the quarterly re-test, and its golden.

**The golden** (``06`` FO9: "a drift is a bug in the port, not a new finding") needs the research
data, 1.9 GB outside the repo. Point ``BASKFY_FNO_RESEARCH_DIR`` at it (``~/baskfy-research/fno``)
and the panels are rebuilt from the raw day files through :func:`panels_from_contracts`, the
same function the worker applies to ``fo_contract_daily``. Without it the golden is skipped
loudly, never silently passed.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl
import pytest

from baskfy_core.fno import research as r
from baskfy_core.fno import retest as rt

RESEARCH_DIR = os.environ.get("BASKFY_FNO_RESEARCH_DIR")

#: Every family of ``RESEARCH.md``'s verdict table, by its key there (A is split by variant).
TABLE_FAMILIES = {"B4", "B1", "B2", "B3", "C1", "C2", "A0", "A1", "A1R", "A2", "A3", "A4", "E"}


class TestTheRegistry:
    def test_every_research_family_is_registered_plus_f1_and_f2(self) -> None:
        assert TABLE_FAMILIES | {"B4_LOSS_CLOSE", "F2"} == rt.FAMILY_KEYS

    def test_b4_is_the_research_run(self) -> None:
        b4 = next(f for f in rt.FAMILIES if f.key == "B4")
        assert b4.condor == r.B4
        assert b4.scope is rt.Scope.INDEX_OPTIONS

    def test_f2_is_the_spec_with_rolls_costed(self) -> None:
        f2 = next(f for f in rt.FAMILIES if f.key == "F2")
        assert f2.futures == (r.FuturesFamily.A0_LONG, r.F2_SPEC)
        assert r.F2_SPEC.charge_rolls

    def test_the_caveat_is_07_section_4_verbatim(self) -> None:
        assert rt.TIER_2E_CAVEAT == (
            "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per "
            "crossing (0.03 % for futures), not measured. `n` legs were modelled because they "
            "did not trade."
        )


class TestWhenItRuns:
    @pytest.mark.parametrize("month", [1, 4, 7, 10])
    def test_due_in_the_quarter_months(self, month: int) -> None:
        assert rt.retest_due(dt.date(2027, month, 2), None)

    @pytest.mark.parametrize("month", [2, 3, 5, 6, 8, 9, 11, 12])
    def test_never_in_another_month(self, month: int) -> None:
        assert not rt.retest_due(dt.date(2027, month, 2), None)

    def test_once_a_month(self) -> None:
        assert not rt.retest_due(dt.date(2027, 1, 9), dt.date(2027, 1, 2))
        assert rt.retest_due(dt.date(2027, 4, 3), dt.date(2027, 1, 2))


class TestSlippage:
    def test_index_needs_both_underlyings_at_twenty_sessions(self) -> None:
        b4 = next(f for f in rt.FAMILIES if f.key == "B4")
        one = rt.family_slippage(b4, {"NIFTY": (0.01, 25)})
        assert one.source is rt.SlippageSource.ASSUMED
        short = rt.family_slippage(b4, {"NIFTY": (0.01, 25), "BANKNIFTY": (0.02, 19)})
        assert short.source is rt.SlippageSource.ASSUMED
        both = rt.family_slippage(b4, {"NIFTY": (0.01, 25), "BANKNIFTY": (0.02, 20)})
        assert (both.source, both.pct) == (rt.SlippageSource.MEASURED, 0.02)  # the worse one

    def test_stock_families_need_twenty_names(self) -> None:
        b1 = next(f for f in rt.FAMILIES if f.key == "B1")
        few = {f"S{i}": (0.02, 30) for i in range(19)}
        assert rt.family_slippage(b1, few).source is rt.SlippageSource.ASSUMED
        enough = {f"S{i}": (0.01 * (1 + i % 3), 30) for i in range(20)}
        got = rt.family_slippage(b1, enough)
        assert got.source is rt.SlippageSource.MEASURED
        assert got.pct == pytest.approx(0.02)

    def test_futures_are_always_assumed(self) -> None:
        f2 = next(f for f in rt.FAMILIES if f.key == "F2")
        measured = {f"S{i}": (0.01, 99) for i in range(40)}
        assert rt.family_slippage(f2, measured).source is rt.SlippageSource.ASSUMED


def _day(day: dt.date) -> pl.DataFrame:
    near, far, week = dt.date(2026, 9, 29), dt.date(2026, 10, 27), dt.date(2026, 10, 6)
    third = dt.date(2026, 11, 24)
    rows = []
    for expiry in (near, far, third):
        rows.append(("FUTSTK", "ABC", expiry, 0.0, "XX", 100.0, 10, 10))
    for expiry in (near, far, third):
        rows.append(("OPTSTK", "ABC", expiry, 100.0, "CE", 5.0, 10, 0))
    rows.append(("OPTSTK", "ABC", near, 110.0, "CE", 1.0, 0, 0))  # no OI, no volume
    rows.append(("FUTIDX", "NIFTY", near, 0.0, "XX", 23000.0, 10, 10))
    rows.append(("OPTIDX", "NIFTY", near, 23000.0, "PE", 90.0, 10, 5))
    rows.append(("OPTIDX", "NIFTY", week, 23000.0, "PE", 40.0, 10, 5))  # a weekly: no future
    rows.append(("OPTIDX", "FINNIFTY", near, 23000.0, "PE", 40.0, 10, 5))
    return pl.DataFrame(
        [
            {
                "trade_date": day,
                "instrument": i,
                "symbol": s,
                "expiry": e,
                "strike": k,
                "option_type": t,
                "open": p,
                "high": p,
                "low": p,
                "close": p,
                "settle": p,
                "underlying": None,
                "open_interest": oi,
                "oi_change": 0,
                "volume": v,
                "turnover": 1e6,
                "lot_size": 100,
            }
            for i, s, e, k, t, p, oi, v in rows
        ]
    )


class TestPanels:
    def test_futures_keep_every_row_and_options_the_two_nearest_monthlies(self) -> None:
        p = rt.panels_from_contracts(_day(dt.date(2026, 9, 21)))
        assert p.futures.height == 4
        assert "date" in p.futures.columns and "trade_date" not in p.futures.columns
        abc = p.options.filter(pl.col("symbol") == "ABC")
        assert sorted(abc["expiry"].to_list()) == [dt.date(2026, 9, 29), dt.date(2026, 10, 27)]

    def test_a_contract_with_neither_oi_nor_volume_is_dropped(self) -> None:
        p = rt.panels_from_contracts(_day(dt.date(2026, 9, 21)))
        assert p.options.filter(pl.col("strike") == 110.0).is_empty()

    def test_index_options_are_nifty_and_banknifty_monthlies_only(self) -> None:
        p = rt.panels_from_contracts(_day(dt.date(2026, 9, 21)))
        idx = p.options.filter(pl.col("instrument") == "OPTIDX")
        assert idx["symbol"].to_list() == ["NIFTY"]
        assert idx["expiry"].to_list() == [dt.date(2026, 9, 29)]


# --------------------------------------------------------------------------------------------
# The golden: RESEARCH.md reproduces from the day files through the worker's own panel function
# --------------------------------------------------------------------------------------------

needs_data = pytest.mark.skipif(
    not RESEARCH_DIR,
    reason=(
        "BASKFY_FNO_RESEARCH_DIR is not set: FO9's golden (B4 n=100 +0.033R, loss close "
        "+0.022R, F2 n=2,334 +0.017R, B1 -0.032R) was NOT checked. Point it at "
        "~/baskfy-research/fno to run it."
    ),
)


@pytest.fixture(scope="module")
def golden_panels() -> rt.Panels:
    assert RESEARCH_DIR is not None
    futures: list[pl.DataFrame] = []
    options: list[pl.DataFrame] = []
    for day in sorted((Path(RESEARCH_DIR) / "days").glob("*.parquet")):
        p = rt.panels_from_contracts(pl.read_parquet(day))
        futures.append(p.futures)
        options.append(p.options)
    return rt.Panels(pl.concat(futures), pl.concat(options))


@needs_data
def test_the_panels_are_the_research_files_exactly(golden_panels: rt.Panels) -> None:
    assert RESEARCH_DIR is not None
    root = Path(RESEARCH_DIR)
    fk = ["date", "symbol", "expiry"]
    assert golden_panels.futures.sort(fk).equals(pl.read_parquet(root / "futures.parquet").sort(fk))
    ok = [*fk, "strike", "option_type"]
    assert golden_panels.options.sort(ok).equals(pl.read_parquet(root / "options.parquet").sort(ok))


@needs_data
@pytest.mark.parametrize(
    ("key", "n", "net_r", "gross_r"),
    [
        ("B4", 100, 0.033, 0.048),
        ("B4_LOSS_CLOSE", 100, 0.022, 0.038),
        ("F2", 2334, 0.017, None),
        ("B1", 1541, -0.032, 0.027),
    ],
)
def test_research_reproduces(
    golden_panels: rt.Panels, key: str, n: int, net_r: float, gross_r: float | None
) -> None:
    family = next(f for f in rt.FAMILIES if f.key == key)
    got = rt.run_family(family, golden_panels.futures, rt.slice_loader(golden_panels.options))
    assert got.n == n
    assert got.net_r is not None
    assert abs(got.net_r - net_r) <= 0.002
    if gross_r is not None:
        assert got.gross_r is not None
        assert abs(got.gross_r - gross_r) <= 0.002
    assert got.slippage_source is rt.SlippageSource.ASSUMED
    assert got.caveat == rt.TIER_2E_CAVEAT


@needs_data
def test_a_symbol_batch_gives_the_whole_panels_trades(golden_panels: rt.Panels) -> None:
    b1 = next(f for f in rt.FAMILIES if f.key == "B1")
    loader = rt.slice_loader(golden_panels.options)
    whole = rt.run_family(b1, golden_panels.futures, loader, batch=10_000)
    batched = rt.run_family(b1, golden_panels.futures, loader, batch=7)
    assert (batched.n, batched.net_r, batched.gross_r) == (whole.n, whole.net_r, whole.gross_r)
