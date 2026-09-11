# TW1 — the pure core: the arithmetic of TWT-1, touching nothing

**Plan:** `docs/twt/06-module-plan.md` § TW1. **Rules:** `docs/twt/04-business-rules.md` is the
spec; every threshold is a field of `baskfy_core.twt.config` and a test asserts no threshold is
written as a literal anywhere else. **Law 1** (`/CLAUDE.md`): `packages/core` touches nothing —
no database, no network, no disk, no clock.

**Ships:** `packages/core/src/baskfy_core/twt/` — `config.py`, `calendar.py`, `indicators.py`,
`signals.py`, `breadth.py`, `sizing.py`, `exits.py`, `plan.py`, `sleeve.py`, `__init__.py` — and
its tests under `packages/core/tests/`.

**The shape is VBT-1's**, which shipped: `baskfy_core/vbt/` has the same ten modules. Copy its
shape, its purity test and its config discipline. Do NOT copy its rules.

- [ ] G1: The package exists with every module `06` names, and `baskfy_core.vbt.breadth` is
      CALLED rather than copied (`04` §4.4) — TWT passes its own spelled-out thresholds.
  CHECK: cd decile-blueprint && ls packages/core/src/baskfy_core/twt/ | tr '\n' ' ' && grep -c "from baskfy_core.vbt" packages/core/src/baskfy_core/twt/breadth.py
  EXPECT: /config.py.*signals.py/
  EVIDENCE: pending

- [ ] G2: **Law 1.** The package imports no database, no network, no disk and no clock.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_purity.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G3: The signal is `04` §3 — the five lines with their senses, the three weekly closes with
      the current week's close taken as the latest daily close, the month-3 low, and the entry
      event. **The year-boundary case is named in the AC because it is the one that silently
      sorts wrong:** ISO week 1 of the next year is AFTER week 52, and a `searchsorted` on
      `year × 100 + week` must not place it before.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_signals.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: The exits are `04` §7 — the 20 % hard stop, the trailing stop off the highest high
      clamped to the exchange tick, the ratchet that never lowers a stop, and the fill-day rule.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_exits.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G5: Sizing applies every cap of `04` §6.2 **in order**, and §6.4's half size.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_sizing.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: The breadth gate is **strict** (> 40 %, not ≥), and a zero denominator is SHUT rather
      than open — an empty universe must not read as a healthy market.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_breadth.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G7: **No threshold is a literal outside `config.py`.** This is the gate that keeps `04`
      the spec rather than a description of the code.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_no_literals.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G8: Thin sessions (`04` §2.1) and the 10 % missing-bar tolerance (§2.2) are TESTS, not
      comments — `research/tight-close/STRATEGY.md` §1 is where they come from.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q -k "twt and (thin or tolerance or calendar)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G9: Money and price levels are `Decimal`. No float reaches a price path (house rule 9).
  CHECK: cd decile-blueprint && grep -rnE ": *float|float\(" packages/core/src/baskfy_core/twt/ | grep -viE "ratio|pct|share|weight|tolerance|#" | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: pending

- [ ] G10: `test_no_escape_hatches.py` still green — no `# type: ignore`, no `Any`, no swallowed
      exception (house rule 3).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_no_escape_hatches.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G11: The whole core suite is green — TW1 broke nothing.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G12: `make lint` clean — ruff, ruff format, mypy --strict.
  CHECK: cd decile-blueprint && make lint 2>&1 | tail -6
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

<!--
A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit.
Never weaken a test to make this file pass; settle a wrong criterion by the charter's precedence
order, record it in docs/twt/DECISIONS-TW.md, continue.
-->
