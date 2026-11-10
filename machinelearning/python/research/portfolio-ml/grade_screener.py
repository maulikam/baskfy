"""PM1.13: grade our as-filed NSE rows against the Screener export, with Tapetide as the third voice.

usage: python grade_screener.py <data/screener> <nse_fund.csv> <tapetide archive dir>"""

from __future__ import annotations

import collections
import glob
import json
import os
import sys

from grade_tapetide import CRORE, load_nse, load_tapetide, num, rate, within
from portfolioml.screener import DIRS, quarters

METRICS = {"revenue": ("revenue", CRORE), "net_profit": ("pat", CRORE), "eps": ("eps_diluted", 1.0)}
RESTATE_METRICS = ("revenue", "net_profit")  # filed EPS steps on a bonus or split, so it cannot show a restatement
FLOOR = 0.005
MIN_WORST_CRORE = 10.0
BANK_PREFIXES = ("BANKING_", "INTEGRATED_FILING_BANKING_")


def load_screener(root: str) -> dict[tuple[str, str, str], dict[str, float]]:
    """(symbol, period_end, basis) → figures."""
    out = {}
    for basis, folder in DIRS.items():
        for path in glob.glob(f"{root}/{folder}/*.json"):
            symbol = os.path.basename(path)[:-5]
            for period, row in quarters(json.load(open(path, encoding="utf8"))).items():
                out[(symbol, period, basis)] = row
    return out


def is_bank(row: dict) -> bool:
    return row["source_url"].rsplit("/", 1)[-1].startswith(BANK_PREFIXES)


def agrees(ours: float, theirs: float, metric: str) -> bool:
    """Screener prints larger figures in whole crores, so those carry half a crore of slack."""
    return within(ours, theirs, 0.5 if metric != "eps" and theirs.is_integer() else FLOOR)


def classify(ours: float, theirs: float, third: float | None, metric: str) -> str:
    """A difference: both others against us means our parser; Screener alone is a restatement."""
    if third is None:
        return "restated_no_third"
    if agrees(third, theirs, metric) and not agrees(ours, third, metric):
        return "parser_suspect"
    return "restated_confirmed"


def main(root: str, fund: str, tapetide: str) -> int:
    scr, nse, tt = load_screener(root), load_nse(fund), load_tapetide(tapetide)
    table = collections.defaultdict(list)
    quarter_bad: dict = collections.defaultdict(lambda: [0, 0, 0])
    kinds: collections.Counter = collections.Counter()
    worst, suspects = [], []
    swapped = compared = 0
    nse_symbols = {k[0] for k in nse}
    only_screener = sum(1 for k in scr if k[0] in nse_symbols and "2018-03-31" <= k[1] and k[2] not in nse.get(k[:2], {}))
    for (symbol, period), bases in nse.items():
        for basis, rows in bases.items():
            theirs = scr.get((symbol, period, basis))
            if theirs is None:
                continue
            first, year, bank = rows[0], period[:4], is_bank(rows[0])
            group = "bank" if bank else "non-bank"
            third = tt.get((symbol, period)) if tt.get((symbol, period), {}).get("basis") == basis else None
            restated = False
            compared += 1
            for metric, (col, unit) in METRICS.items():
                value = num(first[col])
                if value is None or metric not in theirs:
                    continue
                ours = value / unit
                good = agrees(ours, theirs[metric], metric)
                table[(year, group, metric)].append(good)
                if good or metric not in RESTATE_METRICS:
                    continue
                restated = True
                t3 = third.get(metric) if third else None
                kind = classify(ours, theirs[metric], t3, metric)
                kinds[(kind, metric)] += 1
                other = "consolidated" if basis == "standalone" else "standalone"
                swap = metric in scr.get((symbol, period, other), {}) and agrees(ours, scr[(symbol, period, other)][metric], metric)
                swapped += swap
                row = {"symbol": symbol, "period": period, "basis": basis, "metric": metric, "nse": ours, "screener": theirs[metric], "tapetide": t3, "kind": kind, "other_basis_matches": swap, "bank": bank}
                if kind == "parser_suspect":
                    suspects.append(row)
                if max(abs(ours), abs(theirs[metric])) >= MIN_WORST_CRORE:
                    worst.append((abs(theirs[metric] - ours) / max(abs(ours), 1e-9), row))
            stats = quarter_bad[year]
            stats[0] += 1
            stats[1] += restated
            stats[2] += bank

    print(f"== compared {compared} (name, quarter, basis) on {len({k[0] for k in nse})} crawled names")
    print(f"   Screener quarters with no NSE row on our basis, 2018 on, crawled names: {only_screener}")
    print("== within 1 % by year")
    print(f"   {'year':5}{'group':10}" + "".join(f"{m:>22}" for m in METRICS))
    for year in sorted({k[0] for k in table}):
        for group in ("non-bank", "bank"):
            cells = [table.get((year, group, m), []) for m in METRICS]
            if any(cells):
                print(f"   {year:5}{group:10}" + "".join(f"{len(c):>10} {rate(c):>10.1%} " for c in cells))
    print("== quarters beyond 1 % on revenue or PAT (the step 3 rate)")
    for year, (n, bad, _) in sorted(quarter_bad.items()):
        print(f"   {year}: {bad}/{n} = {bad / n:.1%}")
    window = [quarter_bad[y] for y in ("2018", "2019", "2020") if y in quarter_bad]
    n, bad = sum(w[0] for w in window), sum(w[1] for w in window)
    print(f"   2018-2020: {bad}/{n} = {(bad / n if n else float('nan')):.1%}")
    print("== differences by kind (revenue and PAT)")
    for key, count in sorted(kinds.items()):
        print(f"   {key[0]:20}{key[1]:12}{count}")
    print(f"   our figure matches Screener's other basis: {swapped}")
    print(f"== parser suspects: Screener and Tapetide agree, we differ: {len(suspects)}")
    for r in sorted(suspects, key=lambda r: (r["symbol"], r["period"]))[:40]:
        print(f"   {r['symbol']:12}{r['period']} {r['basis'][:4]} {r['metric']:11} nse {r['nse']:12.2f} scr {r['screener']:12.2f} tt {r['tapetide']:12.2f}{'  bank' if r['bank'] else ''}")
    print(f"== worst 30, revenue or PAT, either side at least {MIN_WORST_CRORE:.0f} cr")
    for gap, r in sorted(worst, key=lambda x: -x[0])[:30]:
        t3 = "-" if r["tapetide"] is None else f"{r['tapetide']:.2f}"
        print(f"   {r['symbol']:12}{r['period']} {r['basis'][:4]} {r['metric']:11} nse {r['nse']:12.2f} scr {r['screener']:12.2f} tt {t3:>10} gap {gap:7.1%} {r['kind']}{' other-basis' if r['other_basis_matches'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
