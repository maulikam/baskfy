#!/usr/bin/env python3
"""Report which gate files are incomplete, so one gate can assert a whole run's ledger.

A file is **complete** when every box is checked, every checked box carries evidence that is not
the word ``pending``, and the only unchecked boxes are ones an ``ABANDON:`` line accounts for --
the honest exit the unlazy discipline names.

**Why a gate needs to exclude itself.** The gate that asserts "every gate file in this run is
complete" is written in one of those files, and its own evidence stays ``pending`` until it
passes. Scanning itself, it counts its own pending line and fails -- for a reason that has nothing
to do with the thing it tests. ``gates/twt-10.md`` G8 already carries one repair of this shape
(its CHECK line contained the string it grepped for); this is the same fault one layer down, and
the answer is to name the exclusion rather than hide it. ``--exclude FILE:GATE_ID`` does that, and
the summary line prints what was excluded so a reader sees exactly what is not being asserted.

Usage:
    python3 tools/gates/ledger.py gates/twt-*.md --exclude gates/twt-10.md:G8

The last line is what a gate's EXPECT matches:

    14 files scanned, 0 incomplete (excluded as self-referential: gates/twt-10.md G8)

Exit status is 0 only when nothing is incomplete.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GATE_RE = re.compile(r"^- \[( |x|X)\] (.*)$")
ATTR_RE = re.compile(r"^\s+(CHECK|EXPECT|EVIDENCE):\s?(.*)$")
# Indented, because that is how this repo writes them: `gates/twt-0.md` line 79 sits
# inside its gate block. `gate-check.mjs` anchors at column 0 and so misses those, which
# is why a scoped gate can read as merely unchecked there.
ABANDON_RE = re.compile(r"^\s*ABANDON:\s*(\S+)\s*(.*)$")


def scan(path: Path, excluded_ids: set[str]) -> tuple[int, list[str]]:
    """Return (gates counted, reasons this file is incomplete)."""
    current_id: str | None = None
    current_checked = False
    seen: dict[str, tuple[bool, str | None]] = {}
    order: list[str] = []
    abandoned: set[str] = set()

    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        gate_match = GATE_RE.match(line)
        if gate_match:
            body = gate_match.group(2).strip()
            id_match = re.match(r"^(\S+?):", body)
            current_id = id_match.group(1) if id_match else f"line{index + 1}"
            current_checked = gate_match.group(1).lower() == "x"
            seen[current_id] = (current_checked, None)
            order.append(current_id)
            continue
        attr_match = ATTR_RE.match(line) if current_id else None
        if attr_match and attr_match.group(1).upper() == "EVIDENCE":
            seen[current_id] = (current_checked, attr_match.group(2).strip())
            continue
        abandon_match = ABANDON_RE.match(line)
        if abandon_match:
            abandoned.add(abandon_match.group(1).rstrip(":"))
        if (line.startswith("#") or line.startswith("- ")) and not gate_match:
            current_id = None

    reasons: list[str] = []
    counted = 0
    for gate_id in order:
        if gate_id in excluded_ids or gate_id in abandoned:
            continue
        counted += 1
        checked, evidence = seen[gate_id]
        if not checked:
            reasons.append(f"{gate_id} unchecked")
        elif not evidence or evidence.lower() == "pending":
            reasons.append(f"{gate_id} evidence pending")
    return counted, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate_files", nargs="+", type=Path)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="FILE:GATE_ID",
        help="a self-referential gate to leave out of the scan; printed in the summary",
    )
    args = parser.parse_args()

    excluded: dict[str, set[str]] = {}
    for spec in args.exclude:
        if ":" not in spec:
            print(f"ledger: --exclude wants FILE:GATE_ID, got {spec!r}", file=sys.stderr)
            return 2
        file_part, gate_part = spec.rsplit(":", 1)
        excluded.setdefault(str(Path(file_part)), set()).add(gate_part)

    incomplete: list[str] = []
    total_gates = 0
    empty: list[str] = []
    for path in args.gate_files:
        counted, reasons = scan(path, excluded.get(str(path), set()))
        total_gates += counted
        if counted == 0:
            empty.append(str(path))
        if reasons:
            incomplete.append(f"{path}: {'; '.join(reasons)}")

    # A file with no gates in it would otherwise report "0 incomplete" and exit 0 -- a glob that
    # matched the wrong thing reading as a clean ledger. Refuse instead of passing vacuously.
    if empty:
        print(f"ledger: no gates found in {', '.join(empty)}", file=sys.stderr)
        return 2

    for line in incomplete:
        print(f"  INCOMPLETE {line}")

    note = ""
    if args.exclude:
        pretty = ", ".join(spec.replace(":", " ") for spec in args.exclude)
        note = f" (excluded as self-referential: {pretty})"
    print(
        f"{len(args.gate_files)} files scanned, {total_gates} gates, "
        f"{len(incomplete)} incomplete{note}"
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
