#!/usr/bin/env python3
"""Re-run every CHECK in a gate file and verify each against its EXPECT.

Why this exists rather than `gate-check.mjs`:

1. **The checker only re-runs gates it believes are unmet.** Once a file is fully checked with
   evidence it runs nothing and prints `ALL MET`, which says the *file* is complete, not that the
   module is still green. A parent row that wants to re-verify a child needs the checks executed
   again -- that is the verification hierarchy the unlazy discipline asks for, the parent
   re-running what the leaf claimed.

2. **`gate-check.mjs` drops a lone file argument.** Its filter is
   `args.filter((a, i) => !a.startsWith("--") && i !== tIdx + 1)`, and with no `--timeout` present
   `tIdx` is -1, so the predicate excludes index 0 -- the only argument there is. The file list
   falls back to `GATES.md` plus every `gates/*.md` in the tree. In this repo that is hundreds of
   gates, and because `gates/twt-root.md` itself invokes the checker, it recurses until the
   machine gives out. Passing `--status <file>` or `--timeout N <file>` dodges it; passing just
   the file does not.

Usage:
    python3 tools/gates/rerun.py gates/twt-6.md [--timeout 900]

Output ends with one summary line, which is what a parent gate's EXPECT matches:

    gates/twt-6.md: 11/11 checks re-run, 0 failed

Exit status is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

GATE_RE = re.compile(r"^- \[( |x|X)\] (.*)$")
ATTR_RE = re.compile(r"^\s+(CHECK|EXPECT|EVIDENCE):\s?(.*)$")
# Indented, because that is how this repo writes them: `gates/twt-0.md` line 79 sits
# inside its gate block. `gate-check.mjs` anchors at column 0 and so misses those, which
# is why a scoped gate can read as merely unchecked there.
ABANDON_RE = re.compile(r"^\s*ABANDON:\s*(\S+)\s*(.*)$")


@dataclass
class Gate:
    gate_id: str
    title: str
    checked: bool
    check: str | None = None
    expect: str | None = None
    evidence: str | None = None


@dataclass
class ParsedFile:
    gates: list[Gate] = field(default_factory=list)
    abandoned: set[str] = field(default_factory=set)


def parse(text: str) -> ParsedFile:
    """Parse a gate file the same way gate-check.mjs does, so the two agree on what a gate is."""
    out = ParsedFile()
    current: Gate | None = None
    for index, line in enumerate(text.splitlines()):
        gate_match = GATE_RE.match(line)
        if gate_match:
            body = gate_match.group(2).strip()
            id_match = re.match(r"^(\S+?):", body)
            current = Gate(
                gate_id=id_match.group(1) if id_match else f"line{index + 1}",
                title=re.sub(r"^\S+?:\s*", "", body),
                checked=gate_match.group(1).lower() == "x",
            )
            out.gates.append(current)
            continue
        attr_match = ATTR_RE.match(line) if current else None
        if attr_match:
            setattr(current, attr_match.group(1).lower(), attr_match.group(2).strip())
            continue
        abandon_match = ABANDON_RE.match(line)
        if abandon_match:
            out.abandoned.add(abandon_match.group(1).rstrip(":"))
        if (line.startswith("#") or line.startswith("- ")) and not gate_match:
            current = None
    return out


def expect_matches(expect: str, output: str) -> bool:
    """`/regex/flags` is a regular expression; anything else is a substring, as in the checker."""
    rx = re.match(r"^/(.+)/([a-z]*)$", expect)
    if rx:
        # `i` and `m` mean what they mean in JavaScript, because gate-check.mjs compiles the same
        # literal with `new RegExp(body, flags)` and the two must not disagree about a gate.
        flags = 0
        if "i" in rx.group(2):
            flags |= re.IGNORECASE
        if "m" in rx.group(2):
            flags |= re.MULTILINE
        try:
            return re.search(rx.group(1), output, flags) is not None
        except re.error:
            return False
    return expect in output


def tail(output: str, limit: int = 160) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return (" | ".join(lines[-2:]) or "(no output)")[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate_file", type=Path)
    parser.add_argument("--timeout", type=int, default=900, help="per-check timeout in seconds")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="directory to run checks from")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="GATE_ID",
        help=(
            "do not run this gate, and say so in the summary. Its use is circularity: a gate "
            "that asserts the ledger is complete cannot be run from inside a ledger row that is "
            "itself still pending, because the row it is waiting on is the one running it."
        ),
    )
    args = parser.parse_args()

    text = args.gate_file.read_text(encoding="utf-8")
    parsed = parse(text)

    skipped_ids = [
        gate.gate_id for gate in parsed.gates
        if gate.check and gate.gate_id in args.skip
    ]
    runnable = [
        gate for gate in parsed.gates
        if gate.check and gate.gate_id not in parsed.abandoned and gate.gate_id not in args.skip
    ]
    unmatched = sorted(set(args.skip) - set(skipped_ids))
    if unmatched:
        # Not fatal -- an unmatched skip runs more than asked, never less -- but silence here
        # would let `--skip g8` read as `--skip G8` and fail somewhere confusing instead.
        print(
            f"{args.gate_file}: warning, --skip matched no gate: {', '.join(unmatched)}",
            file=sys.stderr,
        )

    if not runnable:
        print(f"{args.gate_file}: no runnable CHECK lines", file=sys.stderr)
        return 2

    failed: list[str] = []
    for gate in runnable:
        try:
            result = subprocess.run(
                gate.check,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
                cwd=args.cwd,
            )
            output = f"{result.stdout}\n{result.stderr}"
            # With an EXPECT the match decides: a check may exit non-zero by design (a grep that
            # finds nothing, a pytest run piped through `tail`). Without one, the exit code does.
            ok = expect_matches(gate.expect, output) if gate.expect else result.returncode == 0
        except subprocess.TimeoutExpired:
            output, ok = f"timed out after {args.timeout}s", False

        status = "PASS" if ok else "FAIL"
        print(f"  {status} {gate.gate_id}: {tail(output)}")
        if not ok:
            failed.append(gate.gate_id)

    unrunnable = len(parsed.gates) - len(runnable) - len(skipped_ids)
    notes = []
    if unrunnable:
        notes.append(f"{unrunnable} without a CHECK or abandoned")
    if skipped_ids:
        notes.append(f"skipped {', '.join(skipped_ids)}")
    note = f", {'; '.join(notes)}" if notes else ""
    print(
        f"{args.gate_file}: {len(runnable) - len(failed)}/{len(runnable)} checks re-run, "
        f"{len(failed)} failed{note}"
        + (f" ({', '.join(failed)})" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
