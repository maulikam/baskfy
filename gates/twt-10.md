# TW10 — the claims become theorems

**Plan:** `docs/twt/06-module-plan.md` § TW10, the safety half. The runbook half is
`gates/twt-10-runbook.md` and is **already green** (`511e31f`).

**Goal:** every Track-B and Track-C claim of `docs/twt/02` is a test rather than a promise. This is
the last module because it asserts properties over the whole sleeve, and it is the one that has to
be true before Maulik flips a flag on ₹25 lakh of real money.

**Four CHECK lines in this file could never have passed as written, and they were repaired
rather than worked around.** G2's grep, G5's diff, G5's own EXPECT (found on re-running the
repair, 12 Sep 2026) and G6's module path are each wrong in a different way; each repair is
recorded on its own gate below with what it now measures. This is
the same class of fault `983a66d` found thirty-one of in this run, and the root `CLAUDE.md` rule
applies: a criterion is a proxy for its Goal, and when the proxy is broken you fix the proxy and
say so.

- [x] G1: **With `BASKFY_TWT_EXECUTION_ENABLED=false`, no TWT code path reaches
      `OrderGateway.place` or `place_gtt_stop` with `DRY_RUN=false`** — a property test over every
      route and every task, not a spot check.
      The clause that does the work is **`DRY_RUN=false`**. The desk's own `test_twt_execute.py`
      pins *both* switches (`_flag_is_false` sets `TWT_EXECUTION_ENABLED` False **and** `DRY_RUN`
      True), so every test there passes for two reasons and cannot say which one held. This file
      sets `DRY_RUN` **False** throughout, leaving the sleeve's own flag as the only thing between
      the code and a broker — which is the state the desk is actually in once it goes live for the
      weekly book.
      The seven desk routes and three Celery tasks are **discovered from source**, not listed, so
      an eighth route fails `TestTheSurfaceIsWhatWeThinkItIs` before it can go untested.
      **Verified by mutation:** flipping the fixture's flag to true turns 7 of the 21 tests red,
      including all three executable line kinds. The first version of the parametrised case set a
      `RAISE_GTT_STOP` equal to the resting trigger, which `04` §7.2 refuses before any gateway
      call — it passed while proving nothing, and the mutation is what caught it. Each kind now
      asserts the gateway tape is non-empty.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: 21 passed in 0.63s — and under mutation (flag true) 7 failed, 14 passed

- [x] G2: **No auto-execute flag exists for this sleeve.** Nothing of the shape
      `BASKFY_TWT_AUTO`-anything is a *setting* anywhere in either tree, and non-negotiable 1's
      named exception is still the swing sleeve's alone.
      ⚠️ **The original CHECK was `grep -rniE "BASKFY_TWT_AUTO" … | wc -l` expecting 0, and it
      could never have returned 0.** It returns **10**, and every one of the ten is correct: the
      tree deliberately carries that string inside *prohibitions* — `app/config.py` and
      `.env.example` both say "THERE IS NO BASKFY_TWT_AUTO_EXECUTE AND THERE WILL NOT BE ONE" —
      and inside `docs/twt`, `gates/` and this file. VBT-1 hit this exact problem and left the
      finding in `tools/deploy/verify-safety.sh` line 63: *a prohibition must not trip the check*.
      The repaired gate runs the same source scan `test_vbt_safety.py` uses: strip docstrings and
      comments, then look for a setting in what remains. It is backed by a planted-offender test
      proving the scan is not vacuous, and by a test that the scan reads a non-empty file set.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_safety_properties.py -k "AutoExecute" 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: 5 passed, 16 deselected in 0.25s

- [x] G3: `exit_lines` **never emits `SELL_AT_OPEN`** over a generated book (`04` §10.2) — there is
      no end-of-day sell rule in TWT-1 and the GTT is the exit.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and sell_at_open" 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: 3 passed, 1 skipped, 7659 deselected in 1.60s

- [x] G4: **The web app has no order route**, and none was added for this sleeve.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent --reporter=basic -t "read-only" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  7 passed | 159 skipped (166) | Tests  34 passed | 2944 skipped (2978)

- [x] G5: **The neighbours are untouched.** The weekly book, R1–R4, the swing book and VBT-1:
      their suites run and the diff over the whole run touches none of their rules.
      ⚠️ **The original CHECK went stale the moment TWT's own desk code was committed.** It
      counted every changed file under `kite-momentum-rebalancer/app`, which is where TWT's *own*
      `twt_desk.py`, `twt_execute.py` and `templates/twt.html` live — so once `8b5ef41` tracked
      them it read **5** instead of 0, for files that are this sleeve's rather than a neighbour's.
      Counting TWT's own additions as neighbour damage is the check being wrong, not the tree.
      The repaired gate asserts something **stricter** than the original: the neighbours' core
      packages have **zero changed files**, and the desk's `app/` has **zero deleted lines** since
      TW0 — TWT added and removed nothing. Confirmed by reading both diffs by hand: every changed
      line in `app/main.py` and `app/config.py` is an addition and every one of them is TWT's.
      ⚠️ **And the repair's own EXPECT was broken, found on re-running it 12 Sep 2026.** It read
      `/^0\/0$/`, and a checker matches its regex against `stdout + "\n" + stderr` — so the string
      under test is `"0/0\n\n"`, in which an unanchored-by-`m` `$` matches only before the *final*
      newline. `/^0\/0$/.test("0/0\n\n")` is `false` in JavaScript and the same in Python. The
      command's answer was right and was written in by hand; the EXPECT beside it had never been
      run. It now carries the `m` flag, which both `gate-check.mjs` and `tools/gates/rerun.py`
      compile the same way. **This is the fourth broken proxy in this one file**, and the pattern
      is always the same: evidence recorded by a person who ran the command, beside an EXPECT
      nobody ran.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "$(git diff --name-only 5056aa1..HEAD -- decile-blueprint/packages/core/src/baskfy_core/swing decile-blueprint/packages/core/src/baskfy_core/vbt decile-blueprint/packages/core/src/baskfy_core/exposure | wc -l | tr -d ' ')/$(git diff --numstat 5056aa1..HEAD -- kite-momentum-rebalancer/app | awk '{d+=$2} END {print d+0}')"
  EXPECT: /^0\/0$/m
  EVIDENCE: 0/0 — no neighbour core file changed, and not one line was deleted from the desk's app/ (re-run 12 Sep 2026)

- [x] G6: `tools/twt/drill.py` runs the full DRY_RUN drill — evening plan, morning plan, confirm
      every line, a fill, a GTT, a ratchet, the sweep — and reports **0 orders reached a broker**.
      This is the sentence the runbook tells Maulik to look for the night before he goes live.
      ⚠️ **The original CHECK was `cd decile-blueprint && uv run python -m tools.twt.drill`, which
      resolves `tools.twt` against `decile-blueprint/tools/` — a package that exists and has no
      `twt` in it.** It failed `ModuleNotFoundError` regardless of what was written, and it passed
      no `--database-url`, which the drill cannot run without. The drill lives at the repo root
      beside `tools/twt/sweep.py`, `tools/twt/backtest.py` and the sibling sleeves'
      `tools/vbt/drill.py`, and is invoked by path exactly as `make twt-backtest` invokes its
      neighbour.
      **The drill asserts its own steps.** Its first version printed `fill -> None`,
      `ratchet -> BLOCKED` and `sweep -> naked_before=0` and then declared a whole session had run:
      three no-ops under a success sentence. Each step now raises `DrillFailure` if it did not
      fire, and the ratchet is driven the way the nightly drives it — a real `RAISE_GTT_STOP` line
      off a real plan, cancelling the resting trigger and arming a higher one. **Verified by
      mutation:** lowering the new high so no ratchet is possible exits 1.
      ⚠️ **Re-verified 12 Sep 2026, and the obvious mutation is not one.** `NEW_HIGH` is ₹130.00
      against a resting trigger of ₹80.00. Lowering it to **₹100.50 changes nothing** — 20 % under
      that is ₹80.40, still above ₹80.00, so the ratchet fires and the drill passes. It takes
      **₹100.00**, where the raised stop lands exactly on the resting trigger and `04` §7.2 refuses
      it, for the step to die: the drill stops at step 5 with `ratchets=0` and exits 1. Recorded
      because a mutation that does not mutate proves nothing, and this one nearly went in as
      evidence that it did.
  CHECK: cd decile-blueprint && docker exec baskfy-postgres psql -U baskfy -d postgres -q -c "DROP DATABASE IF EXISTS baskfy_drill;" -c "CREATE DATABASE baskfy_drill;" >/dev/null 2>&1; uv run python ../tools/twt/drill.py --database-url postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_drill 2>&1 | tail -4
  EXPECT: /0 orders reached a broker/
  EVIDENCE: exit 0 · "calls: 0  []" · "0 orders reached a broker." · counters signals 1, confirms 2, fills 1, ratchets 1 · trigger 80.00 -> 104.00 · sweep rearmed 1, naked after 0

- [x] G7: Both trees' suites pass and lint is clean in both.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: All checks passed! | Success: no issues found in 645 source files

- [x] G8: Every gate file in this run is fully checked with evidence, and `gates/twt-root.md`
      records each module's result.
      ⚠️ **The original CHECK counted itself.** It grepped for the literal string
      `EVIDENCE: pend`+`ing`, and its own CHECK line contains that string — so this file always
      reported at least one pending gate and G8 could never pass, whatever the other files said.
      A check that fails for a reason unrelated to the thing it tests is not a check; `GATES.md`
      G18 carries the same finding about a different probe. The pattern is now anchored to the
      start and end of a line, which its own CHECK line (starting `CHECK:`) cannot match, and the
      grep argument is split across a shell string concatenation so the phrase does not appear
      here whole. The formula also treats an `ABANDON:` line as the honest exit the discipline
      says it is — `gates/twt-0.md` is 9/10 because one criterion was scoped, and a file whose
      shortfall is exactly its abandonments is complete rather than unfinished.
      ⚠️ **And that repair still could not pass, because a gate cannot be its own evidence.**
      This gate's own `EVIDENCE:` line reads `pending` until it passes, so the scan counted itself
      and failed for a reason unrelated to what it tests — the same fault one layer down from the
      one above. The repaired check is `tools/gates/ledger.py`, which takes
      `--exclude FILE:GATE_ID` and **prints the exclusion in its summary line**, so what is not
      being asserted is visible rather than hidden. It also reads an **indented** `ABANDON:` line,
      which is how this repo writes them (`gates/twt-0.md` line 79) and which the inline formula
      caught only by accident of using `[[:space:]]*`. DECISIONS-TW **TW10.2**.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && python3 tools/gates/ledger.py gates/twt-*.md --exclude gates/twt-10.md:G8 2>&1 | tail -1
  EXPECT: /, 0 incomplete/
  EVIDENCE: 15 files scanned, 150 gates, 0 incomplete (excluded as self-referential: gates/twt-10.md G8) — 12 Sep 2026. Fifteen files, not fourteen: `gates/twt-11-funding-clock.md` is in the glob. This gate could not be filled until `gates/twt-root.md`'s seven pending rows were actually run, which is why it sat at `pending` while the run reported itself complete.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
