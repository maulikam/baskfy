"""PM0.7: the reference files under crates/providers/src/reference/, from NSE's "List of Companies
Delisted" (.xlsx) and the archived yearly corporate actions.

usage: python build_delisted_csv.py <archived.xlsx> <nse_delisted.csv> <corporate-actions-year dir> <nse_schemes.csv>"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HOLDER_PAID = re.compile(r"amalgam|merge|scheme|arrangement", re.I)


def first_sheet(path: str) -> list[dict[str, str]]:
    z = zipfile.ZipFile(path)
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        strings = ["".join(t.text or "" for t in si.iter(f"{NS}t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{NS}si")]
    rows = []
    for r in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).find(f"{NS}sheetData"):
        row = {}
        for c in r:
            v = c.find(f"{NS}v")
            if v is not None and v.text is not None:
                row[re.match(r"[A-Z]+", c.get("r")).group()] = strings[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(row)
    return rows


def records(rows: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    """symbol, status, reason. Column A is the symbol, F the type of delisting (read 9 Nov 2026)."""
    head = rows[0]
    if head.get("A", "").strip() != "Symbol" or "Type of Delisting" not in head.get("F", ""):
        raise ValueError(f"unexpected header {head}")
    out = {}
    for r in rows[1:]:
        symbol, reason = r.get("A", "").strip(), " ".join(r.get("F", "").split())
        if symbol:
            out[symbol] = ("merged" if HOLDER_PAID.search(reason) else "delisted", reason)
    return [(s, *out[s]) for s in sorted(out)]


def schemes(directory: str) -> list[tuple[str, str, str]]:
    """issuer (first 7 of the ISIN), ex-date, subject of every scheme, merger or amalgamation NSE lists."""
    out = set()
    for f in sorted(Path(directory).rglob("*.json")):
        body = json.loads(f.read_text(encoding="utf8"))
        for row in body if isinstance(body, list) else body.get("data", []):
            isin, subject = str(row.get("isin") or ""), " ".join(str(row.get("subject") or "").split())
            if not isin.startswith("INE") or not HOLDER_PAID.search(subject):
                continue
            try:
                ex = dt.datetime.strptime(str(row.get("exDate")), "%d-%b-%Y").date()
            except ValueError:
                continue
            out.add((isin[:7], ex.isoformat(), subject.replace(",", ";")))
    return sorted(out)


def write(target: str, header: list[str], rows: list[tuple[str, str, str]]) -> None:
    with open(target, "w", newline="", encoding="utf8") as f:
        w = csv.writer(f, lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
        w.writerow(header)
        w.writerows(rows)


def main(source: str, target: str, actions: str, scheme_target: str) -> None:
    got = records(first_sheet(source))
    write(target, ["symbol", "status", "reason"], [(s, st, why.replace(",", ";")) for s, st, why in got])
    events = schemes(actions)
    write(scheme_target, ["issuer", "ex_date", "subject"], events)
    print(f"{len(got)} delisted names, {sum(1 for _, st, _ in got if st == 'merged')} merged; {len(events)} scheme events")


if __name__ == "__main__":
    main(*sys.argv[1:5])
