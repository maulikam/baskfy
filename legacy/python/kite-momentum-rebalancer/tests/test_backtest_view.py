"""Backtest chart geometry: axes, families, and label placement that stays readable."""
from __future__ import annotations

import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app import main as M
from app.analytics import backtest_view as BV


def variant(cagr, dd, sharpe=1.0, label="v", status="ok"):
    return {"CAGR": cagr, "max_drawdown_pct": dd, "sharpe": sharpe,
            "label": label, "status": status}


SAMPLE = {
    "variants": {
        "A": variant(20.59, -70.45, 0.962, "No regime overlay"),
        "B_raw": variant(23.71, -37.31, 1.486, "Binary M50 50-DMA, raw"),
        "B_buf": variant(20.11, -48.44, 1.224, "Binary M50 50-DMA, buffer"),
        "C_raw": variant(17.16, -40.82, 1.005, "Binary M50 200-DMA, raw"),
        "C_buf": variant(16.95, -40.82, 0.990, "Binary M50 200-DMA, buffer"),
        "E": variant(12.46, -45.98, 0.982, "Three-index tiered"),
        "F": variant(15.83, -49.97, 1.002, "Full tiered model"),
        "abl_breadth": variant(8.42, -50.0, 0.8, "Ablation: breadth only"),
    },
    "limitations": {"breadth": "INSUFFICIENT for this window"},
}


# =====================================================================================
# axes
# =====================================================================================
@pytest.mark.parametrize("lo,hi", [(0, 70.45), (0, 3), (0, 0.4), (-5, 25)])
def test_an_axis_covers_its_data(lo, hi):
    a = BV.axis(lo, hi)
    assert a["min"] <= lo and a["max"] >= hi
    assert a["ticks"][0] == a["min"] and a["ticks"][-1] == a["max"]


def test_axis_ticks_are_evenly_spaced():
    t = BV.axis(0, 70.45)["ticks"]
    gaps = {round(b - a, 6) for a, b in zip(t, t[1:])}
    assert len(gaps) == 1


def test_a_degenerate_range_does_not_divide_by_zero():
    a = BV.axis(5, 5)
    assert a["max"] > a["min"]


# =====================================================================================
# families — colour follows the entity, never the ranking
# =====================================================================================
def test_every_candidate_has_a_family():
    for k in BV.FAMILY_OF:
        assert BV.FAMILY_OF[k] in BV.FAMILIES


def test_ablations_are_not_candidates():
    """They answer 'which input contributes', not 'which should I run'."""
    assert not BV.is_candidate("abl_breadth")


def test_colour_does_not_depend_on_rank():
    """The same variant keeps its colour when the field around it changes."""
    full = BV.risk_return_scatter(SAMPLE["variants"])
    fewer = dict(SAMPLE["variants"])
    del fewer["B_raw"]          # remove the winner
    reduced = BV.risk_return_scatter(fewer)
    colour = {m["key"]: m["color"] for m in full["marks"]}
    for m in reduced["marks"]:
        assert m["color"] == colour[m["key"]]


def test_the_best_ratio_is_marked():
    s = BV.risk_return_scatter(SAMPLE["variants"])
    assert [m["key"] for m in s["marks"] if m["best"]] == ["B_raw"]


def test_tiered_variants_are_flagged_as_proxy():
    s = BV.risk_return_scatter(SAMPLE["variants"])
    proxy = {m["key"] for m in s["marks"] if m["proxy"]}
    assert proxy == {"E", "F"}


def test_ablations_are_excluded_from_the_decision_scatter():
    s = BV.risk_return_scatter(SAMPLE["variants"])
    assert "abl_breadth" not in {m["key"] for m in s["marks"]}


# =====================================================================================
# label placement — the part that silently ruins a chart
# =====================================================================================
def placement(variants):
    return BV.risk_return_scatter(variants)["marks"]


def test_no_two_labels_overlap():
    marks = placement(SAMPLE["variants"])
    clashes = [(a["key"], b["key"]) for a, b in itertools.combinations(marks, 2)
               if BV._hits(a, b)]
    assert clashes == []


def test_no_label_sits_on_top_of_another_mark():
    """A dot printed over a number reads as a corrupted figure, not as two elements."""
    marks = placement(SAMPLE["variants"])
    clashes = [(m["key"], o["key"]) for m in marks for o in marks
               if o is not m and BV._overlap(BV._box(m), BV._mark_box(o))]
    assert clashes == []


def test_labels_stay_inside_the_canvas():
    s = BV.risk_return_scatter(SAMPLE["variants"])
    for m in s["marks"]:
        x0, _, x1, _ = BV._box(m)
        assert x0 >= 0 and x1 <= s["width"], f"{m['key']} label leaves the canvas"


def test_near_identical_points_are_separated():
    """C_raw and C_buf differ by 0.2% CAGR at the same drawdown."""
    marks = {m["key"]: m for m in placement(SAMPLE["variants"])}
    assert not BV._hits(marks["C_raw"], marks["C_buf"])


def test_a_displaced_label_gets_a_leader_line():
    marks = placement(SAMPLE["variants"])
    for m in marks:
        moved = abs(m["ly"] - (m["y"] + 4)) > 1
        assert m["leader"] == moved


def test_placement_survives_many_coincident_points():
    """Degenerate input must not loop forever or throw."""
    v = {k: variant(15.0, -40.0, 1.0) for k in BV.FAMILY_OF}
    marks = placement(v)
    assert len(marks) == len(BV.FAMILY_OF)


# =====================================================================================
# ranked bars
# =====================================================================================
def test_bars_are_sorted_descending():
    b = BV.ranked_bars(SAMPLE["variants"], "sharpe")
    vals = [x["value"] for x in b["bars"]]
    assert vals == sorted(vals, reverse=True)


def test_bar_widths_are_proportional():
    b = BV.ranked_bars(SAMPLE["variants"], "sharpe")
    top, bottom = b["bars"][0], b["bars"][-1]
    assert top["w"] > bottom["w"]


def test_a_bar_is_never_invisible():
    v = {"A": variant(20.0, -70.0, 0.0001), "B_raw": variant(23.0, -37.0, 1.5)}
    for bar in BV.ranked_bars(v, "sharpe")["bars"]:
        assert bar["w"] >= 1.5


def test_ablation_bars_use_the_requested_keys():
    b = BV.ranked_bars(SAMPLE["variants"], "CAGR", keys=["abl_breadth"])
    assert [x["key"] for x in b["bars"]] == ["abl_breadth"]


# =====================================================================================
# build + page
# =====================================================================================
def test_build_returns_nothing_without_results():
    assert BV.build(None) is None
    assert BV.build({}) is None


def test_build_carries_the_breadth_caveat():
    assert "INSUFFICIENT" in BV.build(SAMPLE)["breadth_caveat"]


def test_a_variant_marked_unavailable_is_skipped():
    v = dict(SAMPLE["variants"])
    v["F"] = {"status": "NOT AVAILABLE", "reason": "needs stock-level holdings"}
    assert "F" not in {m["key"] for m in BV.risk_return_scatter(v)["marks"]}


def test_the_page_renders_the_charts():
    page = TestClient(M.app).get("/regime/backtest").text
    assert "Which design earns its complexity" in page
    assert "<svg" in page


def test_the_page_survives_missing_results(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    r = TestClient(M.app).get("/regime/backtest")
    assert r.status_code == 200


# =====================================================================================
# equity / drawdown curves
# =====================================================================================
CURVES = {
    "curves": {
        "dates": ["2020-01-03", "2020-01-10", "2020-01-17", "2020-01-24"],
        "series": {
            "A":     {"equity": [100.0, 120.0, 60.0, 90.0],
                      "drawdown": [0.0, 0.0, -50.0, -25.0]},
            "B_raw": {"equity": [100.0, 110.0, 95.0, 130.0],
                      "drawdown": [0.0, 0.0, -13.64, 0.0]},
            "F":     {"equity": [100.0, 105.0, 98.0, 112.0],
                      "drawdown": [0.0, 0.0, -6.67, 0.0]},
            "abl_x": {"equity": [100.0, 100.0, 100.0, 100.0],
                      "drawdown": [0.0, 0.0, 0.0, 0.0]},
        },
    },
    "variants": SAMPLE["variants"],
}


def test_no_curves_without_series():
    assert BV.curves({}) is None
    assert BV.curves({"curves": {"dates": ["2020-01-01"], "series": {}}}) is None


def test_ablations_are_excluded_from_the_curves():
    c = BV.curves(CURVES)
    assert "abl_x" not in {l["key"] for l in c["lines"]}


def test_every_candidate_becomes_a_path():
    c = BV.curves(CURVES)
    assert {l["key"] for l in c["lines"]} == {"A", "B_raw", "F"}
    for l in c["lines"]:
        assert l["equity"].startswith("M") and l["drawdown"].startswith("M")


def test_the_final_value_is_the_last_point():
    c = BV.curves(CURVES)
    finals = {l["key"]: l["final"] for l in c["lines"]}
    assert finals["A"] == 90 and finals["B_raw"] == 130


def test_the_equity_axis_is_logarithmic():
    """Equal ratios must occupy equal vertical distance."""
    c = BV.curves({"curves": {"dates": ["2020-01-01", "2020-01-08", "2020-01-15"],
                              "series": {"A": {"equity": [100.0, 1000.0, 10000.0],
                                               "drawdown": [0.0, 0.0, 0.0]}}}})
    ys = [float(s.split(",")[1]) for s in
          c["lines"][0]["equity"].replace("M", " ").replace("L", " ").split()]
    assert abs((ys[0] - ys[1]) - (ys[1] - ys[2])) < 0.5


def test_the_drawdown_axis_spans_the_worst_point():
    c = BV.curves(CURVES)
    assert c["worst"] == -50.0
    assert min(t["v"] for t in c["dd_ticks"]) <= -50.0


def test_zero_drawdown_sits_at_the_top():
    c = BV.curves(CURVES)
    zero = [t for t in c["dd_ticks"] if t["v"] == 0][0]
    assert zero["px"] == c["pad_t"]


def test_callout_labels_do_not_collide():
    c = BV.curves(CURVES)
    ys = sorted(l["label_y"] for l in c["lines"] if l["callout"])
    assert all(b - a >= 12.9 for a, b in zip(ys, ys[1:]))


def test_a_gap_in_a_series_breaks_the_path_instead_of_bridging_it():
    """A straight line across missing weeks would invent performance."""
    c = BV.curves({"curves": {
        "dates": ["2020-01-01", "2020-01-08", "2020-01-15"],
        "series": {"A": {"equity": [100.0, None, 120.0],
                         "drawdown": [0.0, None, 0.0]}}}})
    assert c["lines"][0]["equity"].count("M") == 2


def test_curves_reach_the_page():
    page = TestClient(M.app).get("/regime/backtest").text
    assert "Growth of ₹100" in page
    assert "Drawdown from peak" in page


def test_the_stored_curves_agree_with_the_reported_drawdown():
    """The chart must not draw a shallower trough than the table reports — which is what
    bucketing weekly by CLOSE instead of by MINIMUM would do."""
    data = json.load(open("data/outputs/regime_backtest.json"))
    if not data.get("curves"):
        pytest.skip("no curves stored")
    for key, s in data["curves"]["series"].items():
        worst = min(x for x in s["drawdown"] if x is not None)
        reported = float(data["variants"][key]["max_drawdown_pct"])
        assert worst <= reported + 0.01, f"{key}: curve {worst} vs reported {reported}"


# =====================================================================================
# robustness heatmap, timelines, subperiods
# =====================================================================================
def grid_row(b, c, r4, cagr, dd=-50.0, turn=200.0, whip=1.0):
    return {"buffer_bps": b, "confirm_days": c, "r4_exposure": r4, "CAGR": cagr,
            "max_drawdown_pct": dd, "annual_turnover_pct": turn,
            "whipsaws_per_year": whip}


def make_grid(offset=0.0):
    """CAGR depends only on (buffer, confirm) plus `offset` when R4 is non-zero, so
    offset=0 makes the two R4 grids genuinely identical."""
    return [grid_row(b, c, r4, 15.0 + bi * 0.3 + ci * 0.1 + (offset if r4 else 0.0))
            for bi, b in enumerate((100, 150, 200))
            for ci, c in enumerate((2, 3, 5))
            for r4 in (0.0, 10.0)]


def test_no_robustness_without_the_flag():
    assert BV.robustness({}) is None
    assert BV.robustness({"robustness": []}) is None


def test_every_combination_becomes_a_cell():
    r = BV.robustness({"robustness": make_grid()})
    assert len(r["grids"]) == 2
    assert all(len(g["cells"]) == 9 for g in r["grids"])


def test_identical_grids_are_reported_as_an_inert_parameter():
    """If every R4 setting scores the same, the tier was never reached and the knob
    decided nothing. That is a finding, not something to leave for the eye."""
    assert BV.robustness({"robustness": make_grid(offset=0.0)})["r4_inert"] is True


def test_a_parameter_that_does_change_things_is_not_called_inert():
    assert BV.robustness({"robustness": make_grid(offset=3.0)})["r4_inert"] is False


def test_a_narrow_spread_is_flagged():
    r = BV.robustness({"robustness": make_grid()})
    assert r["narrow"] is True and r["spread"] < 2.0


def test_a_wide_spread_is_not_flagged():
    rows = make_grid()
    rows[0]["CAGR"] = 40.0
    assert BV.robustness({"robustness": rows})["narrow"] is False


def test_heat_colours_come_from_the_sequential_ramp():
    r = BV.robustness({"robustness": make_grid()})
    for g in r["grids"]:
        for c in g["cells"]:
            assert c["color"] in BV.RAMP


def test_the_ramp_is_readable_with_one_ink():
    """A ramp needing the text colour to flip partway makes one end unreadable."""
    assert BV.robustness({"robustness": make_grid()})["ink"] == BV.RAMP_INK
    assert len(set(BV.RAMP)) == len(BV.RAMP)


# --- timelines -----------------------------------------------------------------------
TL = {"timelines": [
    {"label": "Feb-Jun 2020", "start": "2020-02-01", "end": "2020-06-30", "rows": [
        {"date": "2020-02-07", "raw_tier": "R1", "tier": "R1", "exposure": 100.0,
         "breadth": None},
        {"date": "2020-02-14", "raw_tier": "R3", "tier": "R2", "exposure": 70.0,
         "breadth": None},
        {"date": "2020-02-21", "raw_tier": "R3", "tier": "R3", "exposure": 40.0,
         "breadth": None}]},
    {"label": "Missing window", "start": "1990-01-01", "end": "1990-12-31", "rows": [],
     "unavailable": "index history does not reach this window"},
]}


def test_no_timelines_without_the_flag():
    assert BV.timelines({}) is None


def test_a_window_with_no_data_says_so_instead_of_drawing_nothing():
    t = BV.timelines(TL)[1]
    assert t["unavailable"] and "path" not in t


def test_exposure_is_drawn_as_a_step_not_a_slope():
    """A sloped line would imply a gradual exit that never happened."""
    t = BV.timelines(TL)[0]
    pts = [p for p in t["path"].replace("M", " ").replace("L", " ").split()]
    xs = [float(p.split(",")[0]) for p in pts]
    ys = [float(p.split(",")[1]) for p in pts]
    # every segment is either purely horizontal or purely vertical
    for i in range(1, len(pts)):
        assert xs[i] == xs[i - 1] or ys[i] == ys[i - 1]


def test_full_exposure_is_at_the_top_and_zero_at_the_bottom():
    t = BV.timelines(TL)[0]
    ticks = {tk["v"]: tk["px"] for tk in t["ticks"]}
    assert ticks[100] < ticks[50] < ticks[0]


def test_tier_changes_are_marked():
    t = BV.timelines(TL)[0]
    assert [c["tier"] for c in t["changes"]] == ["R2", "R3"]


def test_the_lowest_exposure_is_reported():
    assert BV.timelines(TL)[0]["min_exposure"] == 40.0


# --- subperiods ----------------------------------------------------------------------
SP = {"subperiods": [
    {"label": "2007-2009 GFC", "start": "2007-01-01",
     "variants": {"A": {"CAGR": 16.5, "max_drawdown_pct": -70.45},
                  "F": {"CAGR": 13.97, "max_drawdown_pct": -49.97}}},
    {"label": "too short", "start": "2026-01-01", "unavailable": "only 12 sessions"},
]}


def test_no_subperiods_without_data():
    assert BV.subperiods({}) is None
    assert BV.subperiods({"subperiods": [{"label": "x", "unavailable": "no"}]}) is None


def test_unusable_subperiods_are_dropped():
    s = BV.subperiods(SP)
    assert [r["label"] for r in s["rows"]] == ["2007-2009 GFC"]


def test_both_measures_are_scaled_against_their_own_maximum():
    """CAGR and drawdown share no scale; scaling them together would make the smaller
    measure invisible."""
    s = BV.subperiods(SP)
    bars = {b["key"]: b for b in s["rows"][0]["bars"]}
    assert bars["A"]["cagr_w"] == s["bar_w"]          # A has the largest CAGR
    assert bars["A"]["dd_w"] == s["bar_w"]            # and the deepest drawdown
    assert bars["F"]["cagr_w"] < s["bar_w"]


def test_subperiod_bars_keep_the_family_colour():
    s = BV.subperiods(SP)
    for b in s["rows"][0]["bars"]:
        assert b["color"] == BV.FAMILIES[BV.FAMILY_OF[b["key"]]]["color"]


def test_the_new_sections_reach_the_page():
    page = TestClient(M.app).get("/regime/backtest").text
    for heading in ("The trade, window by window", "Event timelines",
                    "Parameter sensitivity"):
        assert heading in page


def test_a_plain_save_run_still_produces_every_section():
    """A run without --robustness/--timelines must not blank sections a fuller run made:
    the page would silently lose charts the reader was looking at."""
    src = open("scripts/backtest_regime.py").read()
    block = src.split("if a.save:")[1].split("payload = {")[0]
    assert "if grid is None:" in block and "if timelines is None:" in block


def test_the_heatmap_separates_cells_with_the_surface():
    """Two adjacent months of similar return must read as two cells, not one band. The
    rule lives in the built stylesheet since the per-page style blocks were merged."""
    css = open("app/static/app.css").read().replace(" ", "")
    assert "border-spacing:2px" in css


# --- exposure panel ------------------------------------------------------------------
EXP = {"curves": {"dates": [f"2020-01-{d:02d}" for d in range(1, 11)],
                  "series": {"A": {"equity": [100.0] * 10, "drawdown": [0.0] * 10,
                                   "exposure": [100.0] * 10},
                             "B_raw": {"equity": [100.0] * 10, "drawdown": [0.0] * 10,
                                       "exposure": [0.0, 100.0] * 5}}}}


def test_exposure_is_optional():
    """Results saved before exposure was serialised must still render."""
    c = BV.curves(CURVES)
    assert c["has_exposure"] is False
    assert all(l["exposure"] == "" for l in c["lines"])


def test_exposure_becomes_a_path_when_present():
    c = BV.curves(EXP)
    assert c["has_exposure"] is True
    assert all(l["exposure"].startswith("M") for l in c["lines"])


def test_full_exposure_is_at_the_top_of_its_panel():
    c = BV.curves(EXP)
    ticks = {t["v"]: t["px"] for t in c["ex_ticks"]}
    assert ticks[100] < ticks[50] < ticks[0]


def test_the_average_exposure_is_the_unsmoothed_mean():
    """The headline number must describe the data, not the drawn approximation."""
    c = BV.curves(EXP)
    avg = {l["key"]: l["avg_exposure"] for l in c["lines"]}
    assert avg["A"] == 100.0 and avg["B_raw"] == 50.0


def test_smoothing_flattens_a_binary_switcher():
    """Drawn raw this is a picket fence; the trailing mean is what makes it readable."""
    raw = [0.0, 100.0] * 20
    sm = BV.curves({"curves": {"dates": [f"d{i}" for i in range(40)],
                               "series": {"B_raw": {"equity": [100.0] * 40,
                                                    "drawdown": [0.0] * 40,
                                                    "exposure": raw}}}})
    ys = [float(s.split(",")[1]) for s in
          sm["lines"][0]["exposure"].replace("M", " ").replace("L", " ").split()]
    churn = sum(abs(b - a) for a, b in zip(ys, ys[1:]))
    # Compare against the raw series drawn on the same panel rather than a magic number.
    panel = sm["ex_h"] - sm["pad_t"] - 26
    raw_churn = (len(raw) - 1) * panel
    assert churn < raw_churn / 10, f"{churn:.0f} vs raw {raw_churn:.0f}"


def test_the_smoothing_window_is_stated_on_the_page():
    page = TestClient(M.app).get("/regime/backtest").text
    assert f"{BV.EXPOSURE_WINDOW}-week trailing" in page


def test_the_stale_placeholder_section_is_gone():
    """It listed curves, heatmaps and timelines as 'still to come' while all three
    rendered above it on the same page."""
    assert "Still to come" not in open("app/templates/regime_backtest.html").read()
