"""Render docs/ranking/VALIDATION.md's generated sections from out/ablation.csv. Contract C8.

    cd decile-blueprint && uv run python ../research/ranking-validation/render_validation.py

Nothing in the metrics tables is typed by hand: every number is read from ``out/ablation.csv`` (the
runner's output) and the promotion rule below is applied to it here, in code, so the statuses in the
document are the rule's output and not a reading of the table. The script rewrites only the text
between ``<!-- generated:<name>:begin -->`` and ``<!-- generated:<name>:end -->`` markers; the prose
around them is the document's own.

The rule (gates/ranking-2.F-validation.md G4; docs/ranking/PLAN.md states no numeric rule of its
own), candidate against its control, on the out-of-sample window:

1. OOS net CAGR >= the control's;
2. OOS max drawdown not worse than the control's by more than 2 percentage points;
3. OOS annual turnover not more than 25% above the control's.

A factor is *not testable* — before the rule is applied — when the run could not measure it: its
coverage in the factor cache is 0 (NULL throughout), or the runner recorded a data limit for it in
``note``. A factor that passes is ``validated``; one that fails is ``rejected``; not testable stays
``research``. The registry (``baskfy_core.factor_registry``) and the tests read the status table.

The entry/retention grid (PLAN C8: "the grid's best OOS-stable cell"): a cell is *stable* when it
passes the same rule against the incumbent 20/40 cell in the in-sample window *and* in the
out-of-sample window; the best stable cell is the one with the highest OOS net CAGR (ties: lower
OOS turnover, then the narrower retention). Its ranks are carried to the selection defaults as
absolute ranks — a quality rank is a position in the universe, not a share of the book.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path

from baskfy_core.ranking_presets import PRESET_SPECS

HERE = Path(__file__).resolve().parent
ABLATION = HERE / "out" / "ablation.csv"
DOC = HERE.parent.parent / "docs" / "ranking" / "VALIDATION.md"

#: The promotion rule's three thresholds.
MAX_DD_WORSENING_PTS = 2.0
MAX_TURNOVER_INCREASE = 0.25

#: Rule outcome -> registry ``validation_status``.
REGISTRY_VALUE = {"validated": "validated", "rejected": "rejected", "not testable": "research"}
#: A study's control model (runner docstring: Sortino exists only for 6M/12M, so the 12M+6M Sharpe
#: pair is the fair control; the RSI penalty's "off" row and the regime filter's control are base).
STUDY_CONTROL = {"sortino": "sharpe_12_6", "rsi_penalty": "base", "regime_filter": "base"}
INCUMBENT_CELL = (20, 40)


@dataclass(frozen=True)
class Window:
    cagr: float
    max_dd: float
    turnover: float


@dataclass(frozen=True)
class Check:
    cagr_ok: bool
    dd_ok: bool
    turnover_ok: bool
    d_cagr_pts: float
    d_dd_pts: float
    turnover_ratio: float

    @property
    def passed(self) -> bool:
        return self.cagr_ok and self.dd_ok and self.turnover_ok


def num(value: str) -> float:
    return float(value) if value.strip() else math.nan


def window(row: dict[str, str], prefix: str) -> Window:
    return Window(
        num(row[f"{prefix}_cagr_net"]),
        num(row[f"{prefix}_max_drawdown"]),
        num(row[f"{prefix}_annual_turnover"]),
    )


def check(candidate: Window, control: Window) -> Check:
    d_cagr = (candidate.cagr - control.cagr) * 100
    d_dd = (candidate.max_dd - control.max_dd) * 100  # negative = deeper drawdown
    ratio = candidate.turnover / control.turnover
    # Rounded to the CSV's own precision before comparing, so an identical row is never "worse".
    return Check(
        cagr_ok=round(d_cagr, 8) >= 0,
        dd_ok=round(d_dd, 8) >= -MAX_DD_WORSENING_PTS,
        turnover_ok=round(ratio, 8) <= 1 + MAX_TURNOVER_INCREASE,
        d_cagr_pts=d_cagr,
        d_dd_pts=d_dd,
        turnover_ratio=ratio,
    )


def pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value * 100:.2f}%"


def coverage_text(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.2%}"


def pts(value: float) -> str:
    return f"{value:+.2f}"


def mark(ok: bool) -> str:
    return "pass" if ok else "FAIL"


def md_table(header: list[str], rows: list[list[str]], align: str | None = None) -> str:
    spec = align or "l" + "r" * (len(header) - 1)
    rule = ["---" if a == "l" else "---:" for a in spec]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(rule) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def metrics_section(rows: list[dict[str, str]]) -> str:
    base = rows[0]
    head = (
        f"Source: `research/ranking-validation/out/ablation.csv` ({len(rows)} models, "
        f"`{base['version']}`). Windows: full {base['full_start']} to {base['full_end']}; "
        f"IS {base['is_start']} to {base['is_end']}; OOS {base['oos_start']} to {base['oos_end']} "
        f"(OOS starts from IS's closing NAV). Every model: top {base['max_names']}, "
        f"{float(base['cost_bps_per_side']):g} bps a side. Drawdowns are peak-to-trough; "
        "turnover is one-way, per year. Max-sector weight is `n/a` for every model: the export "
        "has no sector membership."
    )
    main = md_table(
        [
            "model",
            "study",
            "entry/ret",
            "IS CAGR",
            "OOS CAGR",
            "OOS max DD",
            "OOS turnover",
            "full CAGR",
            "full max DD",
            "full turnover",
            "max-sector wt",
        ],
        [
            [
                f"`{r['model']}`",
                r["study"],
                f"{r['entry_rank']}/{r['retention_rank']}",
                pct(num(r["is_cagr_net"])),
                pct(num(r["oos_cagr_net"])),
                pct(num(r["oos_max_drawdown"])),
                f"{num(r['oos_annual_turnover']):.2f}",
                pct(num(r["full_cagr_net"])),
                pct(num(r["full_max_drawdown"])),
                f"{num(r['full_annual_turnover']):.2f}",
                pct(num(r["full_mean_max_sector_weight"])),
            ]
            for r in rows
        ],
        align="llr" + "r" * 8,
    )
    years = sorted(int(k.removeprefix("return_")) for k in base if k.startswith("return_"))
    per_year = md_table(
        ["model", *(str(y) for y in years)],
        [[f"`{r['model']}`", *(pct(num(r[f"return_{y}"])) for y in years)] for r in rows],
    )
    partial = (
        f"{years[0]} starts at the first fill after {base['full_start']}; {years[-1]} ends "
        f"{base['full_end']}."
    )
    return f"{head}\n\n{main}\n\n**Per-year net return** ({partial})\n\n{per_year}"


def factor_verdicts(rows: list[dict[str, str]]) -> list[tuple[dict[str, str], Check, str]]:
    base = window(rows[0], "oos")
    out = []
    for r in rows:
        if r["study"] != "factor":
            continue
        result = check(window(r, "oos"), base)
        coverage = num(r["factor_coverage"])
        if r["note"].strip() or coverage == 0:
            status = "not testable"
        else:
            status = "validated" if result.passed else "rejected"
        out.append((r, result, status))
    return out


def promotion_section(rows: list[dict[str, str]]) -> str:
    verdicts = factor_verdicts(rows)
    base = window(rows[0], "oos")
    table = md_table(
        [
            "factor",
            "coverage",
            "OOS CAGR",
            "Δ CAGR pts",
            "Δ max DD pts",
            "turnover x base",
            "CAGR",
            "DD",
            "turnover",
            "status",
            "registry",
        ],
        [
            [
                f"`{r['factor']}`",
                coverage_text(num(r["factor_coverage"])),
                pct(num(r["oos_cagr_net"])),
                pts(c.d_cagr_pts),
                pts(c.d_dd_pts),
                f"{c.turnover_ratio:.2f}",
                mark(c.cagr_ok),
                mark(c.dd_ok),
                mark(c.turnover_ok),
                f"**{status}**",
                REGISTRY_VALUE[status],
            ]
            for r, c, status in verdicts
        ],
        align="lr" + "r" * 4 + "lll" + "ll",
    )
    counts = {s: sum(1 for *_, st in verdicts if st == s) for s in REGISTRY_VALUE}
    notes = [
        f"- `{r['factor']}` — not testable: {r['note'].strip()}. The rule's own result on this row "
        f"was {'pass' if c.passed else 'fail'}; it is not used."
        for r, c, status in verdicts
        if status == "not testable"
    ]
    control = (
        f"Control: `base` — OOS CAGR {pct(base.cagr)}, max DD {pct(base.max_dd)}, turnover "
        f"{base.turnover:.2f}. Thresholds: Δ CAGR ≥ 0; Δ max DD ≥ -{MAX_DD_WORSENING_PTS:g} pts; "
        f"turnover ≤ {1 + MAX_TURNOVER_INCREASE:.2f} x base "
        f"({base.turnover * (1 + MAX_TURNOVER_INCREASE):.2f})."
    )
    promoted = ", ".join("`" + r["factor"] + "`" for r, _, s in verdicts if s == "validated")
    summary = (
        f"Outcome: {counts['validated']} validated ({promoted or 'none'}), "
        f"{counts['rejected']} rejected, {counts['not testable']} not testable."
    )
    return "\n\n".join([control, table, summary, "\n".join(notes)])


def study_section(rows: list[dict[str, str]]) -> str:
    by_model = {r["model"]: r for r in rows}
    table_rows = []
    for r in rows:
        control_name = STUDY_CONTROL.get(r["study"])
        if control_name is None or r["model"] == control_name:
            continue
        c = check(window(r, "oos"), window(by_model[control_name], "oos"))
        table_rows.append(
            [
                f"`{r['model']}`",
                f"`{control_name}`",
                pts(c.d_cagr_pts),
                pts(c.d_dd_pts),
                f"{c.turnover_ratio:.2f}",
                mark(c.cagr_ok),
                mark(c.dd_ok),
                mark(c.turnover_ok),
                "**pass**" if c.passed else "**fail**",
            ]
        )
    return md_table(
        [
            "study model",
            "control",
            "Δ CAGR pts",
            "Δ max DD pts",
            "turnover x control",
            "CAGR",
            "DD",
            "turnover",
            "rule",
        ],
        table_rows,
        align="ll" + "rrr" + "llll",
    )


def grid_section(rows: list[dict[str, str]]) -> tuple[str, tuple[int, int]]:
    cells = [r for r in rows if r["study"] == "grid"]
    key = {(int(r["entry_rank"]), int(r["retention_rank"])): r for r in cells}
    incumbent = key[INCUMBENT_CELL]
    metric_cols = [c for c in incumbent if c.startswith(("full_", "is_", "oos_", "return_"))]
    table_rows = []
    stable: list[tuple[float, float, int, tuple[int, int]]] = []
    for (entry, retention), r in sorted(key.items()):
        same_as = next(
            (
                f"≡ e{e}/r{t}"
                for (e, t), other in sorted(key.items())
                if (e, t) != (entry, retention)
                and e < entry
                and t == retention
                and all(other[c] == r[c] for c in metric_cols)
            ),
            "",
        )
        is_c = check(window(r, "is"), window(incumbent, "is"))
        oos_c = check(window(r, "oos"), window(incumbent, "oos"))
        is_stable = is_c.passed and oos_c.passed
        if (entry, retention) == INCUMBENT_CELL:
            verdict = "incumbent"
        elif same_as:
            # A duplicate of a narrower-entry cell is that cell, not a second candidate.
            verdict = "duplicate"
        else:
            verdict = "stable" if is_stable else "not stable"
            if is_stable:
                order = (-num(r["oos_cagr_net"]), num(r["oos_annual_turnover"]), retention)
                stable.append((*order, (entry, retention)))
        table_rows.append(
            [
                f"{entry}/{retention}",
                pct(num(r["is_cagr_net"])),
                pct(num(r["oos_cagr_net"])),
                pct(num(r["oos_max_drawdown"])),
                f"{num(r['oos_annual_turnover']):.2f}",
                "pass" if is_c.passed else "fail",
                "pass" if oos_c.passed else "fail",
                verdict,
                same_as,
            ]
        )
    stable.sort()
    best = stable[0][3] if stable else INCUMBENT_CELL
    table = md_table(
        [
            "entry/retention",
            "IS CAGR",
            "OOS CAGR",
            "OOS max DD",
            "OOS turnover",
            "rule vs 20/40, IS",
            "rule vs 20/40, OOS",
            "verdict",
            "identical to",
        ],
        table_rows,
        align="l" + "rrrr" + "llll",
    )
    ranked = ", ".join(f"{e}/{t}" for *_, (e, t) in stable) or "none"
    choice = (
        f"Stable cells, best first: {ranked}. **Best OOS-stable cell: {best[0]}/{best[1]}.**\n\n"
        f"Selection default (`SelectionConstraints`, C5): `entry_rank={best[0]}, "
        f"retention_rank={best[1]}`."
    )
    return f"{table}\n\n{choice}", best


def status_lines(rows: list[dict[str, str]]) -> dict[str, str]:
    return {r["factor"]: status for r, _, status in factor_verdicts(rows)}


def preset_section(rows: list[dict[str, str]]) -> str:
    """PLAN C7: a preset is ``ready`` only when every factor it ranks by was validated.

    Any term rejected or not testable makes it ``research``. A preset none of whose factors was in
    the ablation keeps its declared status (``desk_quality``: parity with ``baskfy_core.score``).
    """
    statuses = status_lines(rows)
    table_rows = []
    for spec in PRESET_SPECS.values():
        verdicts = {f: statuses.get(f, "not in C8") for f in spec.factors}
        if any(v in ("rejected", "not testable") for v in verdicts.values()):
            status = "research"
        elif all(v == "validated" for v in verdicts.values()):
            status = "ready"
        else:
            status = spec.status
        grouped: dict[str, list[str]] = {}
        for factor, verdict in verdicts.items():
            grouped.setdefault(verdict, []).append(f"`{factor}`")
        evidence = "; ".join(f"{verdict}: {', '.join(keys)}" for verdict, keys in grouped.items())
        joiner = " → " if spec.mode == "sequential" else ", "
        terms = joiner.join(f"`{f}`" for f in spec.factors)
        table_rows.append([f"`{spec.key}`", spec.mode, terms, evidence, status])
    return md_table(["preset", "mode", "terms", "C8 evidence", "status"], table_rows, align="lllll")


def replace_block(text: str, name: str, body: str) -> str:
    pattern = re.compile(
        rf"(<!-- generated:{name}:begin[^>]*-->).*?(<!-- generated:{name}:end -->)", re.S
    )
    if not pattern.search(text):
        raise SystemExit(f"{DOC}: markers for {name!r} not found")
    return pattern.sub(lambda m: m.group(1) + "\n" + body + "\n" + m.group(2), text)


def main() -> None:
    with ABLATION.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or rows[0]["model"] != "base":
        raise SystemExit(f"{ABLATION}: the first row must be the base model")
    grid, best = grid_section(rows)
    text = DOC.read_text(encoding="utf-8")
    for name, body in (
        ("metrics", metrics_section(rows)),
        ("promotion", promotion_section(rows)),
        ("studies", study_section(rows)),
        ("grid", grid),
        ("presets", preset_section(rows)),
    ):
        text = replace_block(text, name, body)
    DOC.write_text(text, encoding="utf-8")
    counts = {s: list(status_lines(rows).values()).count(s) for s in REGISTRY_VALUE}
    print(f"wrote {DOC}: {counts}, best grid cell {best[0]}/{best[1]}")


if __name__ == "__main__":
    main()
