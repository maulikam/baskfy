"""PM1.9: grade Tapetide's archived pages against our NSE rows, per (name, period).

usage: python grade_tapetide.py <tapetide archive dir> <nse_fund.csv> <nse_shp.csv> <sessions.txt> <dead.csv> <banks.txt>"""

from __future__ import annotations

import bisect
import calendar
import collections
import csv
import datetime as dt
import glob
import json
import statistics as st
import sys

TOL, VALUE_PASS, DATE_PASS, DEAD_PASS = 0.01, 0.95, 0.95, 0.80
MIN_DEAD_QUARTERS = 8
CRORE = 1e7
METRICS = {"revenue": ("revenue", CRORE), "net_profit": ("pat", CRORE), "eps": ("eps_diluted", 1.0)}


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text[:10])


def within(ours: float, theirs: float, floor: float = 0.0) -> bool:
    """Relative error ≤ TOL; `floor` is the absolute slack for figures quoted to two decimals."""
    gap = abs(theirs - ours)
    return gap <= floor or (ours != 0 and gap / abs(ours) <= TOL)


def num(text: str | None) -> float | None:
    return float(text) if text not in (None, "") else None


def load_tapetide(root: str) -> dict:
    rows = {}
    for path in glob.glob(f"{root}/fundamentals/*/*/p*.json"):
        symbol = path.split("/")[-3]
        for r in json.load(open(path, encoding="utf8"))["data"]:
            if r["period_type"] == "quarter" and r["value"] is not None:
                rows.setdefault((symbol, r["period_end"]), {"basis": r["basis"], "from": r["available_from"], "est": r["available_from_estimated"]})[r["metric"]] = float(r["value"])
    return rows


def load_nse(path: str) -> dict:
    """(symbol, period_end) → {basis: rows oldest filing first}."""
    out: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in csv.DictReader(open(path, encoding="utf8")):
        out[(r["symbol"], r["period_end"])][r["period_type"]].append(r)
    for bases in out.values():
        for rows in bases.values():
            rows.sort(key=lambda r: r["filed_on"])
    return out


def session_gap(sessions: list[dt.date], a: dt.date, b: dt.date) -> int:
    return bisect.bisect_right(sessions, a) - bisect.bisect_right(sessions, b)


def label_to_end(label: str) -> str:
    month, year = label.split()
    m = list(calendar.month_abbr).index(month)
    return dt.date(int(year), m, calendar.monthrange(int(year), m)[1]).isoformat()


def rate(hits: list[bool]) -> float:
    return sum(hits) / len(hits) if hits else float("nan")


def grade_values(tt: dict, nse: dict, only: set[str] | None = None) -> tuple[dict, list]:
    res = collections.defaultdict(list)
    errors = collections.defaultdict(list)
    basis = collections.Counter()
    for key, t in tt.items():
        if key not in nse or (only is not None and key[0] not in only):
            continue
        bases = nse[key]
        if t["basis"] not in bases:
            basis["tapetide basis absent from NSE rows"] += 1
            continue
        basis[f"same basis: {t['basis']}"] += 1
        first = bases[t["basis"]][0]
        for metric, (col, unit) in METRICS.items():
            ours = num(first[col])
            if ours is None or metric not in t:
                continue
            good = within(ours / unit, t[metric], 0.005 if metric == "eps" else 0.0)
            res[metric].append(good)
            if ours:
                errors[metric].append(abs(t[metric] - ours / unit) / abs(ours / unit))
    return {m: (len(h), rate(h), st.median(errors[m]) if errors[m] else None) for m, h in res.items()}, [basis]


def main(root: str, fund: str, shp: str, sessions_path: str, dead_path: str, banks_path: str) -> int:
    tt, nse = load_tapetide(root), load_nse(fund)
    sessions = [day(x) for x in open(sessions_path, encoding="utf8").read().split()]
    banks = set(open(banks_path, encoding="utf8").read().split())
    dead = {r["symbol"]: r for r in csv.DictReader(open(dead_path, encoding="utf8"))}
    crawled = {k[0] for k in nse}

    print("== values, first NSE filing, same basis, relative error ≤ 1 %")
    values, notes = grade_values(tt, nse)
    for m, (n, r, med) in values.items():
        print(f"  {m:11} matched {n:5}  within 1 %: {r:6.1%}  median error {med:.4%}")
    print("  basis:", dict(notes[0]))
    print("  banks only:", {m: f"{n} {r:.1%}" for m, (n, r, _) in grade_values(tt, nse, banks)[0].items()})

    print("== available_from against our first filed_on, sessions")
    gaps, by_est, early = [], collections.defaultdict(list), collections.Counter()
    for key, t in tt.items():
        rows = nse.get(key, {}).get(t["basis"])
        if not rows:
            continue
        g = session_gap(sessions, day(t["from"]), day(rows[0]["filed_on"]))
        gaps.append(g)
        by_est[bool(t["est"])].append(g)
        if g < 0:
            early[bool(t["est"])] += 1
    date_ok = rate([abs(g) <= 1 for g in gaps])
    print(f"  matched {len(gaps)}  within 1 session: {date_ok:.1%}")
    for est, g in sorted(by_est.items()):
        q = st.quantiles(g, n=20) if len(g) > 1 else [0] * 19
        print(f"  estimated={est}: n={len(g)} within 1: {rate([abs(x) <= 1 for x in g]):.1%}  median {st.median(g)}  p5 {q[0]} p95 {q[18]}  earlier than the filing: {early[est] / len(g):.1%}")

    print("== quarters, 2018-03 to 2026-09, crawled names")
    pulled = {k[0] for k in tt}
    ours_keys = {k for k in nse if k[0] in pulled and "2018-03-31" <= k[1] <= "2026-09-30"}
    theirs_keys = {k for k in tt if k[0] in crawled and "2018-03-31" <= k[1] <= "2026-09-30"}
    print(f"  both {len(ours_keys & theirs_keys)}  only NSE {len(ours_keys - theirs_keys)}  only Tapetide {len(theirs_keys - ours_keys)}")

    print("== promoter % and pledge %")
    shares: dict = collections.defaultdict(list)
    for r in csv.DictReader(open(shp, encoding="utf8")):
        shares[(r["symbol"], r["period_end"])].append(r)
    promoter, pledge = [], []
    for path in glob.glob(f"{root}/shareholding/*/*/p0.json"):
        symbol = path.split("/")[-3]
        table = json.load(open(path, encoding="utf8"))["data"]["data"][0]["data"].get("Promoters", {})
        for label, v in table.items():
            rows = shares.get((symbol, label_to_end(label)))
            if rows and v is not None and num(sorted(rows, key=lambda r: r["filed_on"])[0]["promoter_pct"]) is not None:
                promoter.append(within(float(sorted(rows, key=lambda r: r["filed_on"])[0]["promoter_pct"]), float(v), 0.01))
    for path in glob.glob(f"{root}/pledge/*/*/p0.json"):
        symbol = path.split("/")[-3]
        for q in json.load(open(path, encoding="utf8"))["data"].get("quarterly_trend") or []:
            rows = shares.get((symbol, q["quarter"]))
            v = q.get("pct_promoter_holding_pledged")
            if rows and v is not None:
                ours = num(sorted(rows, key=lambda r: r["filed_on"])[0]["promoter_pledged_pct"])
                if ours is not None:
                    pledge.append(within(ours, float(v), 0.01))
    print(f"  promoter matched {len(promoter)} within 1 %: {rate(promoter):.1%}")
    print(f"  pledge   matched {len(pledge)} within 1 %: {rate(pledge):.1%}")

    print(f"== dead names ({len(dead)}): quarters of revenue, last period against our last bar")
    quarters = collections.Counter(k[0] for k, t in tt.items() if "revenue" in t)
    last = {}
    for k in tt:
        last[k[0]] = max(last.get(k[0], ""), k[1])
    present, recycled = 0, []
    for symbol, r in sorted(dead.items()):
        n = quarters[symbol]
        late = bool(last.get(symbol)) and day(last[symbol]) > day(r["last_bar"]) + dt.timedelta(days=200)
        if late:
            recycled.append(symbol)
        present += n >= MIN_DEAD_QUARTERS
        print(f"  {symbol:12} {r['status']:9} last bar {r['last_bar']}  quarters {n:3}  last period {last.get(symbol, '-')}{'  AFTER DEATH' if late else ''}")
    dead_ok = present / len(dead)
    print(f"  with ≥ {MIN_DEAD_QUARTERS} quarters: {present}/{len(dead)} = {dead_ok:.0%}; figures after the last bar: {len(recycled)}")

    print("== verdict")
    checks = {f"{m} ≥ 95 %": v[1] >= VALUE_PASS for m, v in values.items()}
    checks |= {"promoter ≥ 95 %": rate(promoter) >= VALUE_PASS, "pledge ≥ 95 %": rate(pledge) >= VALUE_PASS,
               "available_from within 1 session ≥ 95 %": date_ok >= DATE_PASS, "dead names ≥ 80 %": dead_ok >= DEAD_PASS}
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("OVERALL", "PASS" if all(checks.values()) else "FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:7]))
