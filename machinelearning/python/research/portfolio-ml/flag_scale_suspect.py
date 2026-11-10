"""PM1.13: mark filings whose scale tag looks wrong. The figures stay as filed; only `scale_suspect` is written.

usage: python flag_scale_suspect.py <data/screener> [--apply]"""

from __future__ import annotations

import collections
import math
import os
import subprocess
import sys

from portfolioml.screener import CRORE

PG = ["docker", "exec", "-i", os.environ.get("BASKFY_PG_CONTAINER", "baskfy-local-postgres-1"), "psql", "-U", "baskfy", "-d", "baskfy", "-v", "ON_ERROR_STOP=1", "-q"]
EXACT, PAT_SLACK, MIN_CRORE, MAX_POWER = 0.01, 0.10, 1.0, 5


def scale_gap(revenue: float | None, pat: float | None, ref_revenue: float | None, ref_pat: float | None) -> int:
    """k when the reference is 10^k times ours (k ≠ 0): a wrong tag scales every figure, so PAT must not contradict it."""
    if not revenue or not ref_revenue or revenue * ref_revenue < 0 or max(abs(revenue), abs(ref_revenue)) < MIN_CRORE:
        return 0
    ratio = ref_revenue / revenue
    k = round(math.log10(ratio))
    if k == 0 or abs(k) > MAX_POWER or abs(ratio / 10.0**k - 1) > EXACT:
        return 0
    if pat and ref_pat and abs(ref_pat / pat / 10.0**k - 1) > PAT_SLACK:
        return 0
    return k


def flagged(group: list[dict], reference: dict | None) -> set[str]:
    """`group` is one (name, quarter, basis), oldest filing first, in crore."""
    def off(r: dict) -> int:
        return scale_gap(r["revenue"], r["pat"], reference.get("revenue"), reference.get("net_profit")) if reference else 0

    out = {r["source_url"] for r in group if off(r)}
    for earlier, later in zip(group, group[1:]):
        if scale_gap(earlier["revenue"], earlier["pat"], later["revenue"], later["pat"]):
            trust_earlier = reference is not None and "revenue" in reference and off(earlier) == 0 and off(later) != 0
            out.add(later["source_url"] if trust_earlier else earlier["source_url"])
    return out


def main(root: str, apply: bool) -> int:
    from grade_screener import load_screener

    scr = load_screener(root)
    out = subprocess.run([*PG, "-At", "-F", "\t", "-c", "select i.symbol, f.period_end, f.period_type, f.filed_on, f.revenue, f.pat, f.source_url from fundamental_pit f join instrument i on i.id = f.instrument_id order by 1,2,3,4"], capture_output=True, text=True, check=True).stdout
    groups: dict = collections.defaultdict(list)
    for symbol, period, basis, filed, revenue, pat, url in (line.split("\t") for line in out.splitlines()):
        groups[(symbol, period, basis)].append({"filed": filed, "revenue": float(revenue) / CRORE if revenue else None, "pat": float(pat) / CRORE if pat else None, "source_url": url})
    marks = {}
    for key, group in groups.items():
        for url in flagged(group, scr.get(key)):
            marks[url] = key
    print(f"scale_suspect: {len(marks)} of {sum(map(len, groups.values()))} filings, {len({k[0] for k in marks.values()})} names")
    for url, key in sorted(marks.items(), key=lambda kv: kv[1]):
        print(f"  {key[0]:12}{key[1]} {key[2][:4]} {url.rsplit('/', 1)[-1]}")
    if not apply:
        print("dry run: nothing written")
        return 0
    urls = ",".join("'" + u.replace("'", "''") + "'" for u in marks)
    subprocess.run([*PG, "-c", f"update fundamental_pit set scale_suspect = (source_url = any(array[{urls}]::text[])) where scale_suspect is distinct from (source_url = any(array[{urls}]::text[]))"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--apply" in sys.argv))
