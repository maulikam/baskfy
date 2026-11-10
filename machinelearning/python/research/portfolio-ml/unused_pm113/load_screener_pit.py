"""UNUSED (PM1.13): Maulik ruled that nothing from Screener feeds fundamental_pit; kept for the record, --apply refuses.

PM1.13 step 3: Screener quarters with no NSE row, lagged, into fundamental_pit. Idempotent; dry run without --apply.

usage: python load_screener_pit.py <data/screener> [--apply]     undo: DELETE FROM fundamental_pit WHERE source = 'screener'"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import glob
import io
import json
import os
import subprocess
import sys
from decimal import Decimal

from portfolioml.screener import DIRS, quarters

PG = ["docker", "exec", "-i", os.environ.get("BASKFY_PG_CONTAINER", "baskfy-local-postgres-1"), "psql", "-U", "baskfy", "-d", "baskfy", "-v", "ON_ERROR_STOP=1", "-q"]
COLUMNS = ["instrument_id", "period_end", "filed_on", "period_type", "filed_at", "available_on", "period_start", "is_revision", "revenue", "other_income", "finance_costs", "depreciation", "ebitda", "pbt", "pat", "eps_diluted", "source", "source_url", "pit_quality", "restated"]


def lagged_available_on(period_end: dt.date) -> dt.date:
    """SEBI's deadlines: 45 days after a quarter, 60 after the March year-end."""
    return period_end + dt.timedelta(days=60 if period_end.month == 3 else 45)


def quarter_start(period_end: dt.date) -> dt.date:
    month = period_end.month - 2
    return dt.date(period_end.year, month, 1)


def sql(query: str) -> list[list[str]]:
    out = subprocess.run([*PG, "-At", "-F", "\t", "-c", query], capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines()]


def rupees(crore: float | None) -> Decimal | None:
    return None if crore is None else (Decimal(str(crore)) * 10_000_000).quantize(Decimal("0.01"))


def build(symbol: str, iid: int, basis: str, period: str, q: dict[str, float]) -> list | None:
    if "revenue" not in q and "net_profit" not in q:
        return None
    end = dt.date.fromisoformat(period)
    day = lagged_available_on(end)
    parts = [q.get(k) for k in ("pbt", "interest", "depreciation", "other_income")]
    ebitda = None if any(p is None for p in parts) else parts[0] + parts[1] + parts[2] - parts[3]
    return [iid, end, day, basis, dt.datetime.combine(day, dt.time()), day, quarter_start(end), False, rupees(q.get("revenue")), rupees(q.get("other_income")), rupees(q.get("interest")), rupees(q.get("depreciation")),
            rupees(ebitda), rupees(q.get("pbt")), rupees(q.get("net_profit")), q.get("eps"), "screener", f"screener:{basis}/{symbol}/{period}", "lagged", True]


def main(root: str, apply: bool) -> int:
    ids = {s: int(i) for i, s in sql("select id, symbol from instrument")}
    filed = {(int(i), p) for i, p in sql("select distinct instrument_id, period_end from fundamental_pit where source <> 'screener'")}
    rows, years, names = [], collections.Counter(), set()
    for basis, folder in DIRS.items():
        for path in glob.glob(f"{root}/{folder}/*.json"):
            symbol = os.path.basename(path)[:-5]
            iid = ids.get(symbol)
            if iid is None:
                continue
            for period, q in quarters(json.load(open(path, encoding="utf8"))).items():
                row = None if (iid, period) in filed else build(symbol, iid, basis, period, q)
                if row:
                    rows.append(row)
                    years[period[:4]] += 1
                    names.add(symbol)
    print(f"{len(rows)} lagged rows on {len(names)} names; by year {sorted(years.items())}")
    if not apply:
        print("dry run: nothing written")
        return 0
    print("REFUSED: Screener rows do not go into fundamental_pit (PM1.13)")
    return 2
    body = io.StringIO()
    csv.writer(body).writerows(rows)
    script = (f"begin;\ncreate temp table s (like fundamental_pit including defaults) on commit drop;\n\\copy s ({','.join(COLUMNS)}) from stdin csv\n{body.getvalue()}\\.\n"
              "delete from s using fundamental_pit f where f.instrument_id = s.instrument_id and f.period_end = s.period_end and f.source <> 'screener';\n"
              f"insert into fundamental_pit ({','.join(COLUMNS)}) select {','.join(COLUMNS)} from s on conflict (source_url) do nothing;\ncommit;\n")
    subprocess.run(PG, input=script, text=True, check=True)
    print("loaded:", sql("select count(*), count(distinct instrument_id), min(period_end), max(period_end) from fundamental_pit where source = 'screener'"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--apply" in sys.argv))
