#!/usr/bin/env python3
"""`gates/twt-scan-now.md` G7 and G8: TW12's code has no path to an order, and never funds.

Two checks, one command, because they answer the same question from opposite ends:

**MONEY-FREE (G7).** None of the modules TW12 added names a broker verb, the execution package,
or a gateway builder. `packages/core/tests/test_twt_safety_properties.py` proves the behavioural
version — it drives `POST /twt/scan` against a real `OrderGateway` with `DRY_RUN` false and asserts
the gateway was never called *at all* — and this is the cheap static half that re-runs in a second.

**CAPITAL-AND-FLAG (G8).** Nothing TW12 added writes `tw_config.sleeve_capital_inr` or touches
`BASKFY_TWT_EXECUTION_ENABLED`. `PLAN-SCAN-SYNC.md` names that rail twice and `docs/twt/02` §3 says
the run "never sets ... in any circumstance". This is the assertion, not the assurance.

WHY IT STRIPS PROSE FIRST, AND WHY THAT IS NOT A LOOPHOLE
---------------------------------------------------------
These modules **describe** what they must not do — `twt_scan.py`'s docstring says in so many words
that it places, arms and cancels nothing "whichever way `BASKFY_TWT_EXECUTION_ENABLED` is set". A
check that read prose would fail on the prohibition itself, which is the trap
`tools/deploy/verify-safety.sh` hit for VBT-1 and recorded in a comment: *a prohibition must not
trip the check*. So docstrings and comments go first and what remains is scanned for code.

That would be a loophole if nobody checked the stripper, so `_self_test` plants a file whose
prohibition is prose and whose offence is code, and asserts exactly one line survives. It runs on
every invocation, before the real scan: a stripper that ate everything would fail there rather than
report a clean tree.

WHAT IS SCANNED, AND WHY IT IS TWO DIFFERENT THINGS
----------------------------------------------------
The four modules TW12 **added** are scanned whole. `app/twt_desk.py` is not: it is TW6's file and
it legitimately names `sleeve_capital_inr` six times — `/twt/halt` zeroes the capital, which is the
sleeve's stop button and the opposite of funding it. Scanning it whole would report six offences
that are not this leaf's and are not offences. So for the files TW12 **edited**, only the added
lines are scanned, read from `git diff` — and then only those that survive stripping the *whole*
file, because a `+` line arrives without the closing quotes that would tell the stripper it is
prose. The first run of this script found that the hard way and the comment in `_scan` records it.

Usage:  uv run --project decile-blueprint python tools/gates/twt_scan_money_free.py
Exit:   0 if both checks are clean; 1 otherwise, naming every offender.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]

#: Written by TW12. Scanned whole.
ADDED_MODULES: Final[tuple[str, ...]] = (
    "decile-blueprint/services/worker/src/baskfy_worker/tasks/twt_scan.py",
    "decile-blueprint/services/api/src/baskfy_api/twt_scan.py",
    "decile-blueprint/services/api/src/baskfy_api/routers/twt.py",
    "decile-blueprint/services/api/alembic/versions/0042_twt_scan_run.py",
)

#: Touched by TW12. Only the lines it added are scanned.
EDITED_MODULES: Final[tuple[str, ...]] = (
    "kite-momentum-rebalancer/app/twt_desk.py",
    "decile-blueprint/packages/core/src/baskfy_core/models/twt.py",
    "decile-blueprint/services/worker/src/baskfy_worker/tasks/celery_tasks.py",
    "decile-blueprint/services/worker/src/baskfy_worker/celery_app.py",
    "decile-blueprint/services/api/src/baskfy_api/queue.py",
    "decile-blueprint/services/api/src/baskfy_api/settings.py",
    "decile-blueprint/services/api/src/baskfy_api/app.py",
)

#: Every way this tree spells "reach a broker". `kc.` catches the desk's Kite handle; the two
#: gateway builders catch the indirection a route would use instead of naming a verb.
BROKER: Final[tuple[str, ...]] = (
    "place_order",
    "place_gtt",
    "place_gtt_stop",
    "OrderGateway",
    "baskfy_execution",
    "build_twt_gateway",
    "twt_gateway",
    "execute_line",
    "rearm_gtt",
    "sweep_naked",
    "KiteConnect",
    "kiteconnect",
)

#: The two things `PLAN-SCAN-SYNC.md` rule 2 forbids by name.
CAPITAL_AND_FLAG: Final = re.compile(r"sleeve_capital_inr|TWT_EXECUTION_ENABLED")

_DOCSTRING: Final = re.compile(r"(\"\"\"|''')(?:.|\n)*?\1")
_COMMENT: Final = re.compile(r"(#|//).*$", re.MULTILINE)


def strip_prose(source: str) -> str:
    """Triple-quoted strings, then `#` and `//` comments. The same crude stripper
    `test_twt_safety_properties.py` uses, and for the same reason: everything it removes is prose,
    and the thing being looked for is code. A cleverer stripper that is occasionally wrong would be
    worse."""
    return _COMMENT.sub("", _DOCSTRING.sub("", source))


def _added_lines(paths: tuple[str, ...]) -> list[tuple[str, str]]:
    """`(file, line)` for every line `git diff` says this working tree adds to `paths`.

    Untracked files produce nothing here and are covered by `ADDED_MODULES` instead.
    """
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    current = "<unknown>"
    lines: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            lines.append((current, line[1:]))
    return lines


def _scan(label: str, needles: tuple[str, ...] | re.Pattern[str]) -> list[str]:
    offenders: list[str] = []
    units: list[tuple[str, str]] = []
    for name in ADDED_MODULES:
        path = ROOT / name
        if not path.exists():
            offenders.append(f"{name}: MISSING — {label} scanned nothing here")
            continue
        units.extend((name, line) for line in strip_prose(path.read_text("utf-8")).splitlines())
    # An added line arrives out of context, so `strip_prose` cannot see that it sits inside a
    # docstring — the closing `"""` is not in the diff. (That is not hypothetical: the first run
    # of this script reported `twt.scan`'s own prohibition, "it places, arms and cancels nothing,
    # whichever way BASKFY_TWT_EXECUTION_ENABLED is set", as an offence.) So the file is stripped
    # *whole* and an added line counts only if it survives that — prose in, prose out.
    for name in EDITED_MODULES:
        path = ROOT / name
        code = (
            set()
            if not path.exists()
            else {line.strip() for line in strip_prose(path.read_text("utf-8")).splitlines()}
        )
        for file_name, raw in _added_lines((name,)):
            if raw.strip() and raw.strip() in code:
                units.append((f"{file_name} (added)", raw))
    for name, line in units:
        hit = (
            needles.search(line)
            if isinstance(needles, re.Pattern)
            else next((n for n in needles if n in line), None)
        )
        if hit:
            offenders.append(f"{name}: {line.strip()[:110]}")
    return offenders


def _self_test() -> None:
    """Non-vacuity. A planted file whose prohibition is prose and whose offence is code."""
    planted = (
        '"""A docstring promising this module never calls place_order."""\n'
        "# A comment repeating that it never calls place_order.\n"
        "AUTO = kc.place_order(symbol='X')\n"
    )
    remaining = [line for line in strip_prose(planted).splitlines() if "place_order" in line]
    if len(remaining) != 1 or "kc.place_order" not in remaining[0]:
        raise SystemExit(f"SELF-TEST FAILED: the prose stripper is wrong: {remaining!r}")
    capital = (
        '"""The run never sets sleeve_capital_inr."""\n'
        "row.sleeve_capital_inr = Decimal('2500000')\n"
    )
    survived = [line for line in strip_prose(capital).splitlines() if CAPITAL_AND_FLAG.search(line)]
    if len(survived) != 1:
        raise SystemExit(f"SELF-TEST FAILED: the capital scan is vacuous: {survived!r}")


def main() -> int:
    _self_test()
    failed = False
    for label, needles in (
        ("MONEY-FREE", BROKER),
        ("CAPITAL-AND-FLAG", CAPITAL_AND_FLAG),
    ):
        offenders = _scan(label, needles)
        scanned = len(ADDED_MODULES) + len(EDITED_MODULES)
        print(
            f"{label}: {'ok' if not offenders else 'FAILED'} ({scanned} modules scanned, "
            f"{len(offenders)} offenders)"
        )
        for one in offenders:
            print(f"  {one}")
        failed = failed or bool(offenders)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
