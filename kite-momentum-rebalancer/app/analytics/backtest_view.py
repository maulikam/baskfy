"""Chart geometry for the regime backtest page.

Pure: takes the stored results dict, returns pixel-space geometry. No I/O, no rendering,
so the arithmetic that decides where a mark lands is testable on its own.

WHY CHARTS AT ALL
The comparison table has fourteen columns and seven candidates. It contains the answer to
"which variant should I actually run" but does not show it. The scatter does: return on
one axis, the drawdown you had to sit through on the other, so a variant that wins on both
is visibly up and to the left rather than a row you have to read across.

Palette validated against the page surface (#111a2e) with the dataviz validator: lightness
band, chroma floor, CVD separation, normal-vision floor and contrast all pass. Colour
encodes the DESIGN FAMILY, which is a property of the variant, never its rank — a filter
that changed which variants were shown must not repaint the survivors.
"""
from __future__ import annotations

from typing import Mapping, Sequence

# Design families. The decision is not "which of 14 rows" but "does an overlay earn its
# complexity", so the families are the axis that actually matters.
FAMILIES = {
    "none":   {"label": "No overlay",  "color": "#2f9fb8"},
    "binary": {"label": "Binary rule", "color": "#c97d1e"},
    "tiered": {"label": "Tiered",      "color": "#7b6ad0"},
}

FAMILY_OF = {
    "A": "none",
    "B_raw": "binary", "B_buf": "binary", "C_raw": "binary", "C_buf": "binary",
    "E": "tiered", "F": "tiered",
}

# Ablations answer a different question (which input contributes) and are not candidates
# to run, so they are charted separately rather than crowding the decision.
def is_candidate(key: str) -> bool:
    return key in FAMILY_OF


def _nice_step(span: float, target: int = 5) -> float:
    """A round tick step near span/target: 1, 2, 2.5 or 5 times a power of ten."""
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    mag = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 0.1
    while mag * 10 <= raw:
        mag *= 10
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= raw:
            return mag * m
    return mag * 10


def axis(lo: float, hi: float, target: int = 5) -> dict:
    """Padded, round-numbered axis covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    step = _nice_step(hi - lo, target)
    start = step * (int(lo / step) - (1 if lo % step else 0)) if lo else 0.0
    end = step * (int(hi / step) + (1 if hi % step else 0))
    ticks, t = [], start
    while t <= end + step * 1e-9:
        ticks.append(round(t, 6))
        t += step
    return {"min": start, "max": end, "step": step, "ticks": ticks}


# A label is TWO lines — the variant key and its numbers — so the box it occupies is
# about 26px tall, not one line-height. Treating it as one line is what let C_raw, C_buf
# and F print on top of each other.
LABEL_W = 104.0
LABEL_ABOVE = 11.0
LABEL_BELOW = 17.0
NUDGE = 27.0


def _box(m: dict) -> tuple[float, float, float, float]:
    x0 = m["lx"] if m["anchor"] == "start" else m["lx"] - LABEL_W
    return x0, m["ly"] - LABEL_ABOVE, x0 + LABEL_W, m["ly"] + LABEL_BELOW


MARK_R = 9.0            # dot radius plus its surface ring


def _mark_box(m: dict) -> tuple[float, float, float, float]:
    return m["x"] - MARK_R, m["y"] - MARK_R, m["x"] + MARK_R, m["y"] + MARK_R


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _hits(a: dict, b: dict) -> bool:
    return _overlap(_box(a), _box(b))


def _hits_any_mark(m: dict, marks: list[dict]) -> bool:
    """A label must clear every DOT too, not just other labels.

    Missing this put variant F's mark on top of C_raw's numbers, which read as a corrupted
    figure rather than as two overlapping elements.
    """
    box = _box(m)
    return any(_overlap(box, _mark_box(o)) for o in marks if o is not m)


def _place_labels(marks: list[dict], *, top: float, bottom: float,
                  left: float = 4.0, right: float = 716.0) -> None:
    """Give every mark a readable label position, nudging collisions apart.

    Variants can land almost exactly on top of each other — C_raw and C_buf differ by
    0.2% CAGR at identical drawdown — and overlapping text makes both unreadable. The
    anchor side is chosen FIRST, because a label flipped to the left of its mark occupies
    a completely different box, and resolving overlaps before knowing that measures the
    wrong rectangle. Nudged labels get a leader line so the association stays clear.
    """
    def side(m, anchor):
        m["anchor"] = anchor
        m["lx"] = m["x"] + 11 if anchor == "start" else m["x"] - 11

    placed: list[dict] = []
    for m in marks:
        home = m["y"] + 4
        # Preferred side first, but BOTH are tried at every height before moving further
        # away. Extending only rightwards drove labels far down through a dense cluster
        # when the space immediately left of the mark was empty.
        sides = ("start", "end") if m["x"] < 560 else ("end", "start")
        best = None
        for attempt in range(14):
            delta = ((attempt + 1) // 2) * NUDGE * (1 if attempt % 2 == 0 else -1)
            m["ly"] = home + delta
            if m["ly"] - LABEL_ABOVE < top or m["ly"] + LABEL_BELOW > bottom:
                continue
            for anchor in sides:
                side(m, anchor)
                x0, _, x1, _ = _box(m)
                if x0 < left or x1 > right:
                    continue
                if not any(_hits(m, p) for p in placed) and not _hits_any_mark(m, marks):
                    best = (m["ly"], anchor)
                    break
            if best:
                break
        if best:
            m["ly"], _ = best
            side(m, best[1])
        m["leader"] = abs(m["ly"] - home) > 1
        placed.append(m)


def risk_return_scatter(variants: Mapping[str, Mapping], *,
                        width: int = 720, height: int = 380,
                        pad_l: int = 62, pad_b: int = 46,
                        pad_t: int = 18, pad_r: int = 22) -> dict | None:
    """CAGR against the drawdown you had to sit through. Up and left is better."""
    pts = [(k, v) for k, v in variants.items()
           if is_candidate(k) and v.get("status") == "ok"]
    if not pts:
        return None

    xs = [abs(float(v["max_drawdown_pct"])) for _, v in pts]
    ys = [float(v["CAGR"]) for _, v in pts]
    ax = axis(0, max(xs))
    ay = axis(min(0, min(ys)), max(ys))
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    def sx(x): return pad_l + (x - ax["min"]) / (ax["max"] - ax["min"]) * pw
    def sy(y): return pad_t + (1 - (y - ay["min"]) / (ay["max"] - ay["min"])) * ph

    best = max(pts, key=lambda kv: float(kv[1]["CAGR"]) / max(abs(float(kv[1]["max_drawdown_pct"])), 1e-9))

    marks = []
    for k, v in pts:
        fam = FAMILY_OF[k]
        marks.append({
            "key": k, "label": v.get("label", k),
            "family": fam, "color": FAMILIES[fam]["color"],
            "cagr": round(float(v["CAGR"]), 2),
            "dd": round(abs(float(v["max_drawdown_pct"])), 2),
            "x": round(sx(abs(float(v["max_drawdown_pct"]))), 1),
            "y": round(sy(float(v["CAGR"])), 1),
            "best": k == best[0],
            # Tiered variants ran with breadth disabled, so their marks carry a caveat
            # rather than being presented as a like-for-like measurement.
            "proxy": bool(v.get("note")) or fam == "tiered",
        })
    marks.sort(key=lambda m: m["x"])
    _place_labels(marks, top=pad_t, bottom=pad_t + ph,
                  left=4.0, right=width - 4.0)

    return {
        "width": width, "height": height,
        "pad_l": pad_l, "pad_t": pad_t, "pad_b": pad_b, "pad_r": pad_r,
        "plot_w": pw, "plot_h": ph,
        "x": ax, "y": ay,
        "xticks": [{"v": t, "px": round(sx(t), 1)} for t in ax["ticks"]],
        "yticks": [{"v": t, "px": round(sy(t), 1)} for t in ay["ticks"]],
        "zero_y": round(sy(0), 1) if ay["min"] < 0 < ay["max"] else None,
        "marks": marks,
        "families": [{"key": k, **v} for k, v in FAMILIES.items()],
    }


def ranked_bars(variants: Mapping[str, Mapping], metric: str, *,
                keys: Sequence[str] | None = None, width: int = 720,
                row_h: int = 26, pad_l: int = 148, pad_r: int = 62) -> dict | None:
    """One measure, sorted. The ranking IS the reading."""
    rows = [(k, v) for k, v in variants.items()
            if v.get("status") == "ok" and metric in v
            and (k in keys if keys is not None else is_candidate(k))]
    if not rows:
        return None
    rows.sort(key=lambda kv: float(kv[1][metric]), reverse=True)

    vals = [float(v[metric]) for _, v in rows]
    hi = max(max(vals), 0.0) or 1.0
    lo = min(min(vals), 0.0)
    span = hi - lo or 1.0
    bw = width - pad_l - pad_r
    zero = pad_l + (0 - lo) / span * bw

    bars = []
    for i, (k, v) in enumerate(rows):
        val = float(v[metric])
        x = pad_l + (min(val, 0) - lo) / span * bw
        w = abs(val) / span * bw
        fam = FAMILY_OF.get(k, "tiered")
        bars.append({
            "key": k, "label": v.get("label", k), "value": round(val, 3),
            "y": i * row_h, "x": round(x, 1), "w": round(max(w, 1.5), 1),
            "color": FAMILIES[fam]["color"] if k in FAMILY_OF else "#5b6b8c",
            "family": fam,
        })
    return {"width": width, "height": len(bars) * row_h + 8, "row_h": row_h,
            "pad_l": pad_l, "zero": round(zero, 1), "bars": bars, "metric": metric}


import math

# Direct-labelled at the right edge: the three that define the decision. The rest stay in
# their family colour, so seven lines read as three bands rather than as spaghetti.
CURVE_CALLOUTS = ("A", "B_raw", "F")

# Weeks in the trailing mean used for the exposure panel (~1 year).
EXPOSURE_WINDOW = 52


def _log_ticks(lo: float, hi: float) -> list[float]:
    """1-2-5 decade ticks spanning [lo, hi]."""
    out, dec = [], 10 ** math.floor(math.log10(max(lo, 1e-9)))
    while dec <= hi * 10:
        for m in (1, 2, 5):
            v = dec * m
            if lo <= v <= hi:
                out.append(v)
        dec *= 10
    return out or [lo, hi]


def curves(data: Mapping, *, width: int = 720, eq_h: int = 300, dd_h: int = 190,
           ex_h: int = 150, pad_l: int = 58, pad_r: int = 74, pad_t: int = 14,
           pad_b: int = 26) -> dict | None:
    """Equity (log) and drawdown (linear) over the whole window.

    Equity is logarithmic because a linear axis over twenty-one years of compounding
    devotes most of its height to the last few years and makes the early drawdowns — the
    ones that decide whether a design is survivable — invisible.
    """
    c = data.get("curves") or {}
    dates, series = c.get("dates") or [], c.get("series") or {}
    if len(dates) < 2 or not series:
        return None

    keys = [k for k in series if is_candidate(k)]
    if not keys:
        return None

    eq_vals = [v for k in keys for v in series[k]["equity"] if v]
    dd_vals = [v for k in keys for v in series[k]["drawdown"] if v is not None]
    if not eq_vals or not dd_vals:
        return None
    lo, hi = max(min(eq_vals), 1e-6), max(eq_vals)
    # A perfectly flat curve — a variant that sat in cash all window at a 0% cash rate —
    # collapses the log range to zero and divides by it. Widen instead of crashing.
    if hi <= lo:
        lo, hi = lo / 2.0, hi * 2.0
    worst = min(dd_vals)

    pw = width - pad_l - pad_r
    eph, dph = eq_h - pad_t - pad_b, dd_h - pad_t - pad_b
    xph = ex_h - pad_t - pad_b
    n = len(dates) - 1
    llo, lhi = math.log10(lo), math.log10(hi)

    def px(i): return pad_l + i / n * pw
    def eqy(v): return pad_t + (1 - (math.log10(v) - llo) / (lhi - llo)) * eph
    def ddy(v): return pad_t + (-v / -worst) * dph if worst else pad_t
    def exy(v): return pad_t + (1 - v / 100.0) * xph

    def smooth(vals, window=EXPOSURE_WINDOW):
        """Trailing mean of the exposure series.

        Drawn raw, a binary variant switching between 0% and 100% across 1,116 weeks in
        580 pixels is a picket fence: every switch is half a pixel wide and the panel
        reads as noise. The question this panel answers is how invested each design was
        over time, and a trailing mean answers it legibly. The whipsaw COUNT is reported
        separately, so nothing is hidden by smoothing — only moved to where it is
        readable.
        """
        out, acc = [], []
        for v in vals or []:
            if v is None:
                out.append(None)
                continue
            acc.append(v)
            if len(acc) > window:
                acc.pop(0)
            out.append(sum(acc) / len(acc))
        return out

    def path(vals, fn):
        out, pen = [], "M"
        for i, v in enumerate(vals):
            if v is None or (fn is eqy and v <= 0):
                pen = "M"
                continue
            out.append(f"{pen}{px(i):.1f},{fn(v):.1f}")
            pen = "L"
        return " ".join(out)

    lines = []
    for k in sorted(keys, key=lambda k: k not in CURVE_CALLOUTS):
        fam = FAMILY_OF[k]
        eq, dd = series[k]["equity"], series[k]["drawdown"]
        ex = series[k].get("exposure")
        last = next((v for v in reversed(eq) if v), None)
        lines.append({
            "key": k, "family": fam, "color": FAMILIES[fam]["color"],
            "equity": path(eq, eqy), "drawdown": path(dd, ddy),
            "exposure": path(smooth(ex), exy) if ex else "",
            "avg_exposure": (round(sum(x for x in ex if x is not None)
                                   / max(len([x for x in ex if x is not None]), 1), 1)
                             if ex else None),
            "callout": k in CURVE_CALLOUTS,
            "final": round(last, 0) if last else None,
            "label_y": round(eqy(last), 1) if last else None,
        })
    _spread([l for l in lines if l["callout"]], pad_t, pad_t + eph)

    year_ix = [i for i, d in enumerate(dates)
               if d.endswith("-01-01") or (i and dates[i - 1][:4] != d[:4])]
    step = max(1, len(year_ix) // 8)

    return {
        "width": width, "eq_h": eq_h, "dd_h": dd_h, "ex_h": ex_h,
        "has_exposure": any(l["exposure"] for l in lines),
        "exposure_window": EXPOSURE_WINDOW,
        "ex_ticks": [{"v": v, "px": round(exy(v), 1)} for v in (0, 50, 100)],
        "pad_l": pad_l, "pad_r": pad_r, "pad_t": pad_t,
        "eq_plot": eph, "dd_plot": dph,
        "lines": lines,
        "eq_ticks": [{"v": v, "px": round(eqy(v), 1),
                      "label": f"{v:,.0f}"} for v in _log_ticks(lo, hi)],
        "dd_ticks": [{"v": v, "px": round(ddy(v), 1), "label": f"{v:.0f}%"}
                     for v in _dd_ticks(worst)],
        "xticks": [{"px": round(px(i), 1), "label": dates[i][:4]}
                   for i in year_ix[::step]],
        "start": dates[0], "end": dates[-1], "worst": round(worst, 2),
    }


def _dd_ticks(worst: float) -> list[float]:
    step = 10 if worst > -45 else 20
    out, v = [0.0], -step
    while v > worst - step:
        out.append(float(v))
        v -= step
    return out


def _spread(lines: list[dict], top: float, bottom: float, gap: float = 13.0) -> None:
    """Keep right-edge callouts from printing over each other."""
    have = [l for l in lines if l["label_y"] is not None]
    have.sort(key=lambda l: l["label_y"])
    for i in range(1, len(have)):
        if have[i]["label_y"] - have[i - 1]["label_y"] < gap:
            have[i]["label_y"] = have[i - 1]["label_y"] + gap
    for l in have:
        l["label_y"] = min(max(l["label_y"], top + 6), bottom)


# Sequential ramp for magnitude: single hue, monotone lightness, validated with
# validateOrdinal against the page surface.
#
# It sits in the LIGHT half of the hue deliberately. The cells carry their numbers inside
# them, and a ramp spanning dark-to-light needs the ink to flip partway along or one end
# becomes unreadable — a darker ramp measured 2.42:1 for dark ink at its low end. Every
# step here clears 4.5:1 against one ink, so the text never changes colour mid-grid.
RAMP = ("#8577d0", "#9a8ce4", "#b0a4f2", "#c5bcf8", "#dad3fc")
RAMP_INK = "#0b1220"


def _ramp_color(v: float, lo: float, hi: float) -> str:
    if hi <= lo:
        return RAMP[len(RAMP) // 2]
    i = int((v - lo) / (hi - lo) * (len(RAMP) - 1) + 0.5)
    return RAMP[max(0, min(len(RAMP) - 1, i))]


def robustness(data: Mapping, metric: str = "CAGR") -> dict | None:
    """The parameter grid as heatmaps, one per R4 exposure setting.

    Reported, not ranked. The question a heatmap answers here is "does the result depend
    on where these knobs are set", and a flat grid is as informative as a varied one.
    """
    rows = data.get("robustness")
    if not rows:
        return None
    vals = [float(r[metric]) for r in rows]
    lo, hi = min(vals), max(vals)
    buffers = sorted({r["buffer_bps"] for r in rows})
    confirms = sorted({r["confirm_days"] for r in rows})

    grids = []
    for r4 in sorted({r["r4_exposure"] for r in rows}):
        cells = []
        for b in buffers:
            for c in confirms:
                hit = next((r for r in rows if r["buffer_bps"] == b
                            and r["confirm_days"] == c and r["r4_exposure"] == r4), None)
                if not hit:
                    continue
                cells.append({
                    "buffer": b, "confirm": c,
                    "value": round(float(hit[metric]), 2),
                    "dd": round(float(hit["max_drawdown_pct"]), 1),
                    "whip": round(float(hit["whipsaws_per_year"]), 2),
                    "turnover": round(float(hit["annual_turnover_pct"]), 0),
                    "color": _ramp_color(float(hit[metric]), lo, hi),
                })
        grids.append({"r4": r4, "cells": cells})

    # Two things the grid says that a reader should not have to spot by eye.
    signatures = [tuple(c["value"] for c in g["cells"]) for g in grids]
    r4_inert = len(signatures) > 1 and len(set(signatures)) == 1

    return {"grids": grids, "buffers": buffers, "confirms": confirms,
            "metric": metric, "min": round(lo, 2), "max": round(hi, 2),
            "spread": round(hi - lo, 2), "ramp": list(RAMP), "ink": RAMP_INK,
            # Every R4 setting producing an identical grid means the tier was never
            # reached in this window, so the knob decided nothing.
            "r4_inert": r4_inert,
            # A narrow spread means the result is not a tuning artefact — which also
            # means it cannot be tuned into a different answer.
            "narrow": (hi - lo) < 2.0}


def timelines(data: Mapping, *, width: int = 720, height: int = 118,
              pad_l: int = 40, pad_r: int = 16, pad_t: int = 12,
              pad_b: int = 24) -> list[dict] | None:
    """Exposure through each event window, as a step.

    Exposure IS the tier, expressed as the quantity that actually matters, so it needs no
    colour key of its own: a step down to 40% is what R3 MEANS. Held as a step rather than
    interpolated, because the exposure was constant between weekly evaluations and a
    sloped line would imply a gradual exit that never happened.
    """
    wins = data.get("timelines")
    if not wins:
        return None
    out = []
    for w in wins:
        entry = {"label": w["label"], "start": w["start"], "end": w["end"],
                 "width": width, "height": height, "pad_l": pad_l, "pad_t": pad_t,
                 "unavailable": w.get("unavailable")}
        rows = w.get("rows") or []
        if entry["unavailable"] or not rows:
            entry["unavailable"] = entry["unavailable"] or "no evaluations in this window"
            out.append(entry)
            continue

        n = max(len(rows) - 1, 1)
        pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

        def px(i): return pad_l + i / n * pw
        def py(v): return pad_t + (1 - v / 100.0) * ph

        d = [f"M{px(0):.1f},{py(rows[0]['exposure']):.1f}"]
        for i, r in enumerate(rows[1:], start=1):
            d.append(f"L{px(i):.1f},{py(rows[i - 1]['exposure']):.1f}")
            d.append(f"L{px(i):.1f},{py(r['exposure']):.1f}")
        entry["path"] = " ".join(d)
        entry["area"] = (f"{entry['path']} L{px(len(rows) - 1):.1f},{py(0):.1f} "
                         f"L{px(0):.1f},{py(0):.1f} Z")

        changes = []
        for i, r in enumerate(rows):
            if i and r["tier"] != rows[i - 1]["tier"]:
                changes.append({"x": round(px(i), 1), "tier": r["tier"],
                                "date": r["date"], "exposure": r["exposure"]})
        entry["changes"] = changes
        entry["ticks"] = [{"v": v, "px": round(py(v), 1)} for v in (0, 50, 100)]
        entry["first"], entry["last"] = rows[0]["date"], rows[-1]["date"]
        entry["min_exposure"] = min(r["exposure"] for r in rows)
        out.append(entry)
    return out


def subperiods(data: Mapping, *, bar_w: int = 190) -> dict | None:
    """The overlay against no overlay in windows chosen because they hurt.

    Both measures are shown because they point opposite ways: the tiered model cut the
    drawdown in every window and gave up return in every window. Showing only one would
    argue a case rather than present the trade.
    """
    rows = data.get("subperiods")
    if not rows:
        return None
    usable = [r for r in rows if not r.get("unavailable") and r.get("variants")]
    if not usable:
        return None
    keys = list(usable[0]["variants"])
    cagr_max = max(abs(float(v["CAGR"])) for r in usable for v in r["variants"].values())
    dd_max = max(abs(float(v["max_drawdown_pct"]))
                 for r in usable for v in r["variants"].values())

    out = []
    for r in usable:
        entry = {"label": r["label"], "start": r["start"], "bars": []}
        for k in keys:
            v = r["variants"][k]
            fam = FAMILY_OF.get(k, "tiered")
            entry["bars"].append({
                "key": k, "color": FAMILIES[fam]["color"],
                "cagr": round(float(v["CAGR"]), 2),
                "dd": round(float(v["max_drawdown_pct"]), 2),
                "cagr_w": round(abs(float(v["CAGR"])) / cagr_max * bar_w, 1),
                "dd_w": round(abs(float(v["max_drawdown_pct"])) / dd_max * bar_w, 1),
            })
        out.append(entry)
    return {"rows": out, "keys": keys, "bar_w": bar_w,
            "cagr_max": round(cagr_max, 1), "dd_max": round(dd_max, 1)}


def build(data: Mapping | None) -> dict | None:
    """Everything the backtest page needs to draw itself."""
    if not data or not data.get("variants"):
        return None
    v = data["variants"]
    abl = [k for k in v if k.startswith("abl_")]
    return {
        "scatter": risk_return_scatter(v),
        "curves": curves(data),
        "robustness": robustness(data),
        "timelines": timelines(data),
        "subperiods": subperiods(data),
        "sharpe": ranked_bars(v, "sharpe"),
        "ablation": ranked_bars(v, "CAGR", keys=abl, pad_l=196),
        "families": [{"key": k, **f} for k, f in FAMILIES.items()],
        # Restated next to the charts, not only in a limitations block: a reader deciding
        # from these marks has to know the tiered ones were measured without breadth.
        "breadth_caveat": str(data.get("limitations", {}).get("breadth", "")),
    }
