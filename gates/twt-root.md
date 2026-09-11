# TWT — the TW run: TWT-1, the three-weeks-tight position sleeve

**Brief, 11 Sep 2026 (Maulik).** Build TWT-1 — the strategy researched in
`research/tight-close/STRATEGY.md` — into Baskfy as a third sleeve beside the weekly momentum
book and the swing book. Signals from the nightly chain, plans from the desk, every order through
`/analyze → /execute confirm → OrderGateway → GTT`, a **ratcheting trailing GTT**, and a runbook
for the first live morning. **No paper phase**: the sleeve goes live at ₹25 lakh the day Maulik
flips the flag himself. The run builds everything with the flag off, sets no capital, places no
order.

**The law of this run** is `CLAUDE.md` unchanged: the two laws, the seven non-negotiables, the
nine house rules, the safety rails, the autonomy charter. Track C (`docs/twt/02`) is forbidden.

Per-module gates: `gates/twt-0.md` … `gates/twt-10.md`. A module is done when its own file is
fully checked **with evidence**, its tests are green, and its commit exists.

---

**⚠ ELEVEN of the module rows below were repaired on 12 Sep 2026, not four, and the repair is the
point of them.** The first pass caught R6, R7, R9 and R10 — and then wrote the paragraph below as
though those were the only four, while **R0, R1, R2, R3, R4, R5 and R8 carried the identical broken
EXPECT and were all marked green.** Diagnosing a bug and leaving seven live instances of it in the
same file is the fault this ledger keeps recording about itself. All eleven now call
`tools/gates/rerun.py`, and every one of them was executed rather than described.

Running them turned up **nineteen** failing checks underneath — five in twt-0, two in twt-1, five
in twt-2, four in its harness, one each in twt-3, twt-4 and twt-8. Eighteen needed the check
repaired and carry a dated note saying why; the nineteenth (twt-8 G9) needed nothing but the
repo's lint fixed. The classes were: a
`make lint` grep whose window had drifted (twt-1 G12, twt-3 G10), a float filter that flagged the
study's Sharpe ratio as money (twt-1 G9, twt-2 G3), two multi-line `python -c` CHECKs that died on
a syntax error before testing anything (twt-2 G6), four EXPECTs spanning lines with `.*`, three
missing `m` flags, a seam gate still demanding `NOT READY` after TW2 closed the seam, and a delete
guard whose glob had grown onto TW6's file. Each repair is recorded on its own gate. **Two were
real gaps rather than stale checks:** `06`'s TW6a had no `**Goal:**` line, and the repo's `make
lint` was red — one TypeScript error and one ESLint error in the UI tree's uncommitted work, which
had been failing every lint gate in the repository.

**The original paragraph, which remains true of R6, R7, R9 and R10:**
Each read `CHECK: node …/gate-check.mjs gates/twt-N.md | tail -3` with `EXPECT: /0 unchecked/`,
and **that string is not in the checker's vocabulary** — it prints `ALL MET (11 met)` or
`UNMET: 3`, never "0 unchecked". The rows could not have passed as written. Worse, passing a lone
file to that checker makes it scan *every* `gates/*.md` in the tree, and since this file invokes
the checker, it recurses without bound — five nested levels within two minutes, on 12 Sep 2026,
before it was killed.

They also asked the wrong question. The checker only re-runs gates it already believes unmet, so
against a finished file it runs nothing and reports that the **file** is complete — which is a fact
about the ledger, not about the module still being green. A parent row re-verifying a child should
re-execute the child's checks, which is what `tools/gates/rerun.py` does and what these rows now
call. `docs/twt/STATUS.md` records the same class of fault thirty-one times over; this is the
thirty-second through thirty-fifth.

---

- [x] R0: TW0 — the docs pack. `docs/twt/` exists in the shape of `docs/swing/` / `docs/vbt/`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-0.md --skip G0.10 --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-0\.md: 9\/9 checks re-run, 0 failed, skipped G0\.10/
  EVIDENCE: gates/twt-0.md: 9/9 checks re-run, 0 failed, skipped G0.10 (12 Sep 2026). G0.10 is the scoped one — the commit also carries `research/tight-close/`, which was untracked and which the golden fixtures derive from; its ABANDON line is the honest exit. Three of the nine were re-anchored first: the pack legitimately grew a file, migration 0041 is legitimately no longer free, and `06` gained TW6a — which turned out to be the only module heading in the file with no Goal line, now written.

- [x] R1: TW1 — the pure core, `baskfy_core.twt`, law 1 asserted.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-1.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-1\.md: 12\/12 checks re-run, 0 failed/
  EVIDENCE: gates/twt-1.md: 12/12 checks re-run, 0 failed (12 Sep 2026). Two repairs first: G9 was reading the study's Sharpe and Calmar as money, and G12 was grepping for a "Success" line that had drifted out of `tail -6` — it now decides on `make lint`'s exit code, which is 0 only when every stage passed.

- [x] R2: TW2 — the goldens: the research's 164 trades and its metrics reproduced or explained.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-2.md --timeout 900 2>&1 | tail -1; cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-2-harness.md --timeout 900 2>&1 | tail -1
  EXPECT: /twt-2\.md: 15\/15 checks re-run, 0 failed[\s\S]*twt-2-harness\.md: 15\/15 checks re-run, 0 failed/
  EVIDENCE: gates/twt-2.md: 15/15 and gates/twt-2-harness.md: 15/15, 0 failed (12 Sep 2026). Five repairs in the module file and four in the harness, including a CHECK that had never executed (a multi-line `python -c`) and a seam gate still demanding `NOT READY` after TW2 closed the seam. The facts underneath were green throughout: 164 of 164 golden trades, `passes True outside tolerance 0`.

- [x] R3: TW3 — the `tw_` schema and its migration.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-3.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-3\.md: 10\/10 checks re-run, 0 failed/
  EVIDENCE: gates/twt-3.md: 10/10 checks re-run, 0 failed (12 Sep 2026). Two repairs. G10 carried the same drifted `make lint` grep as twt-1 G12 and got the same exit-code repair. **G2 was destructive**: it round-tripped the migration against `BASKFY_DATABASE_URL`'s default, which is the developer's own database, and `make downgrade` is `downgrade base` — so it emptied that database on every run, including this one, before it was caught. It now round-trips a throwaway `baskfy_migrate_check` and asserts `version=0041_twt tw_tables=13`. DECISIONS-TW TW11.4.

- [x] R4: TW4 — the nightly job, the published-session clock, tomorrow's trailing trigger.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-4.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-4\.md: 10\/10 checks re-run, 0 failed/
  EVIDENCE: gates/twt-4.md: 10/10 checks re-run, 0 failed (12 Sep 2026). G2 repaired: it guards the composite FK into `tw_signal_daily`, but its glob had grown onto TW6's `twt_evening.py`, whose `store_plan` deletes superseded PROPOSED **plans**. It now names the three tables it protects and pins that one plan delete, so neither a new signal delete nor a second plan delete can pass.

- [x] R5: TW5 — the sleeve's own cash and book, the half-size counter.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-5.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-5\.md: 8\/8 checks re-run, 0 failed/
  EVIDENCE: gates/twt-5.md: 8/8 checks re-run, 0 failed (12 Sep 2026), 33 db-marked tests run against a live Postgres rather than skipped. The only module of the eleven whose checks all ran and passed unmodified.

- [x] R6: TW6 — the desk plan, `/twt/execute`, the GTT ratchet, the 15:15 sweep.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-6.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-6\.md: 11\/11 checks re-run, 0 failed/
  EVIDENCE: gates/twt-6.md: 11/11 checks re-run, 0 failed (12 Sep 2026). G10 inside it is the desk suite in full — 1984 passed, 17 skipped, 71.83s.

- [x] R7: TW7 — the fill-day rule and the naked-line assertion.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-7.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-7\.md: 6\/6 checks re-run, 0 failed/
  EVIDENCE: gates/twt-7.md: 6/6 checks re-run, 0 failed (12 Sep 2026). G3 is the fill-day rule at 30 tests, G4/G5 the naked-line assertion at 6 each.

- [x] R8: TW8 — the desk page and the read-only web page.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-8.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-8\.md: 9\/9 checks re-run, 0 failed/
  EVIDENCE: gates/twt-8.md: 9/9 checks re-run, 0 failed (12 Sep 2026). It was 8/9 until the repo's lint was fixed: G9 runs the web suite **and** `pnpm run lint`, and the UI tree's uncommitted TS2322 was failing it. Nothing in TW8 was wrong.

- [x] R9: TW9 — the backtest on the page, from the plant's bars, drift flagged.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-9.md --timeout 900 2>&1 | tail -1
  EXPECT: /gates\/twt-9\.md: 8\/8 checks re-run, 0 failed/
  EVIDENCE: gates/twt-9.md: 8/8 checks re-run, 0 failed (12 Sep 2026). G4/G5 are the web page: 2 passed, 49 skipped.

- [x] R10: TW10 — safety properties green, and `docs/twt/FIRST-LIVE-MORNING.md` written.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-10.md --skip G8 --timeout 900 2>&1 | tail -1; cd /Users/maulikdave/Documents/projects/baskfy && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test python3 tools/gates/rerun.py gates/twt-10-runbook.md --timeout 300 2>&1 | tail -1
  EXPECT: /twt-10\.md: 7\/7 checks re-run, 0 failed[\s\S]*twt-10-runbook\.md: 8\/8 checks re-run, 0 failed/
  EVIDENCE: twt-10.md: 7/7 checks re-run, 0 failed, skipped G8 (G8 asserts this file, so R10 cannot be its evidence); twt-10-runbook.md: 8/8 checks re-run, 0 failed. G6 is the drill and it printed "0 orders reached a broker."

- [x] R11: **No live order, no flag flip, no capital set.** The run never enables execution and
      never puts money in the sleeve.
      ⚠️ **The original CHECK asserted one of the three claims.** It grepped for an assignment of
      the flag to true and nothing else, so a live order in the journal or a seeded capital would
      both have passed it. The row now measures all three: `flag_true` counts assignments of
      `BASKFY_TWT_EXECUTION_ENABLED` to true anywhere in either tree, `live_orders` counts the
      lines of the TWT order journal that are **not** a dry run, and `capital_seed` requires the
      seeder to write `sleeve_capital_inr` as an explicit `Decimal("0")` rather than leaving it to
      a column default. The EXPECT also gained the `m` flag, without which its anchors cannot
      match: a checker tests its regex against `stdout + "\n" + stderr`.
      **`journals=1` is there so the order count cannot pass vacuously.** The journal lives under
      `data/`, which the root `CLAUDE.md` keeps untracked, so a fresh clone has none and
      `grep -c` over a missing file answers `0` — which would read as "no live orders" when it
      means "no evidence either way". This row therefore requires the journal to exist, and G6's
      drill is what writes it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'flag_true=%s journals=%s live_orders=%s capital_seed=%s\n' "$(grep -rn 'BASKFY_TWT_EXECUTION_ENABLED' --include=*.py --include=*.example --include=*.ts decile-blueprint kite-momentum-rebalancer 2>/dev/null | grep -icE '=\s*true|default.?=.?True')" "$(ls decile-blueprint/data/outputs/twt_orders_journal.jsonl 2>/dev/null | grep -c .)" "$(grep -hv dry_run decile-blueprint/data/outputs/twt_orders_journal.jsonl 2>/dev/null | grep -c .)" "$(grep -c 'sleeve_capital_inr=Decimal("0")' decile-blueprint/services/api/src/baskfy_api/seed.py)"
  EXPECT: /^flag_true=0 journals=1 live_orders=0 capital_seed=1$/m
  EVIDENCE: flag_true=0 journals=1 live_orders=0 capital_seed=1 (12 Sep 2026). Checked for vacuity: the flag appears 13 times across both trees and not one occurrence assigns it true.

- [x] R12: **Both trees still green.** The decile suite and the desk suite pass at the end of the
      run, and neither the weekly book, R1–R4, the swing book nor VBT-1 changed behaviour.
      ⚠️ **The original CHECK ran `packages/core` in one tree and matched `/passed/`.** It read
      only a fifth of the decile suite, never touched the desk at all, and `/passed/` matches the
      summary of a run that also reports failures — `3 failed, 900 passed` contains the word. The
      row now runs **both** suites in full and decides on their **exit codes**, which are 0 only
      when nothing failed. `test_load.py` is deselected for the same reason `SW-FINAL-REPORT.md`
      deselects it: it is a load measurement, not a behaviour assertion.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && L1=$(mktemp) && L2=$(mktemp) && (cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest packages/core packages/providers packages/execution services/api services/worker -p no:cacheprovider --deselect services/api/tests/test_load.py > "$L1" 2>&1); d=$?; (cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -q -p no:cacheprovider > "$L2" 2>&1); k=$?; echo "decile exit=$d desk exit=$k"; tail -1 "$L1"; tail -1 "$L2"; rm -f "$L1" "$L2"
  EXPECT: /decile exit=0 desk exit=0/
  EVIDENCE: decile exit=0 desk exit=0 (12 Sep 2026) — 7651 passed, 6 skipped, 3 deselected in 1225.61s; 1984 passed, 17 skipped, 12 subtests passed in 72.80s. Not re-run in the final ledger sweep of 12 Sep, and the reason is scope: everything changed after it — two TSX test files and three markdown files — lies outside the five Python packages this row runs. The web half is covered by R8, which runs the web suite and was re-run after the fix. Run **after** TW11 added `set_twt_sleeve`, the `--capital` widening and the two Beat entries, so this is the tree as it stands and not as it stood when the row was written.

- [x] R13: `TW-FINAL-REPORT.md` at the repo root, in the style of `SW-FINAL-REPORT.md`.
      ⚠️ **The EXPECT was anchored without the `m` flag** and so could not match: the checker
      tests it against `stdout + "\n" + stderr`, and `$` without `m` binds only before the final
      newline. Same fault as `gates/twt-10.md` G5.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && test -f TW-FINAL-REPORT.md && wc -l < TW-FINAL-REPORT.md | tr -d ' '
  EXPECT: /^[1-9][0-9]{2,}$/m
  EVIDENCE: 257 — TW-FINAL-REPORT.md exists at the repo root and is 257 lines.
