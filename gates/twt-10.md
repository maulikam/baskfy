# TW10 — the claims become theorems

**Plan:** `docs/twt/06-module-plan.md` § TW10, the safety half. The runbook half is
`gates/twt-10-runbook.md` and is **already green** (`511e31f`).

**Goal:** every Track-B and Track-C claim of `docs/twt/02` is a test rather than a promise. This is
the last module because it asserts properties over the whole sleeve, and it is the one that has to
be true before Maulik flips a flag on ₹25 lakh of real money.

- [ ] G1: **With `BASKFY_TWT_EXECUTION_ENABLED=false`, no TWT code path reaches
      `OrderGateway.place` or `place_gtt_stop` with `DRY_RUN=false`** — a property test over every
      route and every task, not a spot check.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G2: **No auto-execute flag exists for this sleeve.** A grep over the whole tree finds no
      `BASKFY_TWT_AUTO`-anything, and non-negotiable 1's named exception is still the swing
      sleeve's alone.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rniE "BASKFY_TWT_AUTO" --exclude-dir=.git --exclude-dir=node_modules . | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: pending

- [ ] G3: `exit_lines` **never emits `SELL_AT_OPEN`** over a generated book (`04` §10.2) — there is
      no end-of-day sell rule in TWT-1 and the GTT is the exit.
  CHECK: cd decile-blueprint && uv run pytest -q -k "twt and sell_at_open" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: **The web app has no order route**, and none was added for this sleeve.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent --reporter=basic -t "read-only" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G5: **The neighbours are untouched.** The weekly book, R1–R4, the swing book and VBT-1:
      their suites run and the diff over the whole run touches none of their rules.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff --name-only 5056aa1..HEAD -- kite-momentum-rebalancer/app decile-blueprint/packages/core/src/baskfy_core/swing decile-blueprint/packages/core/src/baskfy_core/vbt decile-blueprint/packages/core/src/baskfy_core/exposure | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: pending

- [ ] G6: `tools/twt/drill.py` runs the full DRY_RUN drill — evening plan, morning plan, confirm
      every line, a fill, a GTT, a ratchet, the sweep — and reports **0 orders reached a broker**.
      This is the sentence the runbook tells Maulik to look for the night before he goes live.
  CHECK: cd decile-blueprint && uv run python -m tools.twt.drill 2>&1 | tail -4
  EXPECT: /0 orders reached a broker/
  EVIDENCE: pending

- [ ] G7: Both trees' suites pass and lint is clean in both.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

- [ ] G8: Every gate file in this run is fully checked with evidence, and `gates/twt-root.md`
      records each module's result.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && for f in gates/twt-*.md; do printf "%s %s/%s pending:%s\n" $f $(grep -c '^\- \[x\]' $f) $(grep -cE '^\- \[[ x]\]' $f) $(grep -c 'EVIDENCE: pending' $f); done
  EXPECT: /pending:0/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
