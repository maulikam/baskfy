# TWT-11 — the funding surface and the sleeve's two missing clock entries

**Raised 12 Sep 2026, under `/unlazy` on `REMAINING.md` §2, with Maulik's answer in the session.**
The TW run closed code-complete and handed two things back rather than building them at the end of
an unrelated module (`NEEDS-MAULIK.md` T3, "**Neither was built here**"). This file is that choice
being taken, now that he has seen it.

**Two things, and deliberately not a third.**

1. **The funding surface.** `NEEDS-MAULIK.md` T3 says the sleeve's capital is "your keystroke" and
   then records that **there is nowhere to type it**: `seed.py` line 948 refuses `--capital` for
   any command but `swing`, there is no `/api/v1/twt/config` route, and there is no `me/twt` page.
   The only thing that works today is a raw `UPDATE` on `tw_config`, which bypasses
   `record_system_change` — so the change has no author and no trail, on a sleeve whose whole
   design is that a person decided each number. T3 names the fix itself: *"`set_twt_sleeve` beside
   `set_swing_sleeve` in `seed.py` plus a `--capital` flag, which is the smallest version and
   audits correctly."* That is what is built here.
2. **The clock.** `baskfy.twt.evening` and `baskfy.twt.morning` are registered tasks with **no Beat
   entry**, while `baskfy.twt.detect` has had one at 21:00 since TW4 and VBT-1 has both. Nothing
   builds the plan the runbook tells a person to read.

**And the third, which is refused on purpose: the 15:15 sweep stays a person's command.** The sweep
re-arms GTT stops, so it is order flow, and a Beat entry for it would be the desk placing orders on
a timer. Non-negotiable #1 allows exactly one named auto-execute exception and it is the *swing*
sleeve's. An agent may not add a second (`CLAUDE.md`, seven non-negotiables, #1). `POST /twt/sweep`
and `tools/twt/sweep.py` remain what the runbook says they are.

**This file sets no money and flips no flag.** F5 is here to prove it: after every change below, a
fresh seed still writes ₹0, and `gates/twt-root.md` R11 still reads `flag_true=0 live_orders=0`.

---

- [x] F1: `set_twt_sleeve` exists in `seed.py` beside `set_swing_sleeve` and writes through
      `twt_settings.apply_patch`, so every field that moves leaves a `tw_config_audit` row with an
      author. It never creates the `tw_config` row — `seed_twt_config` owns creation, exactly as
      `set_swing_sleeve` leaves creation to `seed_swing_config`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && python3 -c "import re,pathlib; s=pathlib.Path('decile-blueprint/services/api/src/baskfy_api/seed.py').read_text(); b=s[s.index('async def set_twt_sleeve'):]; b=b[:b.index('\ndef ') if '\ndef ' in b else len(b)]; print('apply_patch' in b and 'TwtConfigPatch' in b and 'TwtCeilings' in b and 'insert(TwConfig)' not in b)"
  EXPECT: /^True$/m
  EVIDENCE: set_twt_sleeve is present, builds a TwtConfigPatch, checks TwtCeilings.from_settings and writes through apply_patch; it contains no insert(TwConfig), so it cannot create the row. Probe printed True.

- [x] F2: `seed twt --capital N` is accepted **and the number reaches the seeder**, `--capital` on
      a command that owns no sleeve is still refused, and `--risk` stays a swing idea because
      `tw_config` has no risk-per-trade column. The old `parser.error` said "`swing` command only"
      and was the whole reason T3 had nowhere to go.
      The first test replaces `_run` rather than pointing at an unreachable database: a check that
      only read the exit code would pass equally well if the capital were parsed and then dropped,
      so what is asserted is the value arriving at the seeder.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_seed_twt_sleeve.py -k "TheCommandLine or TheZeroSurvives" -p no:cacheprovider 2>&1 | tail -2
  EXPECT: /4 passed/
  EVIDENCE: 4 passed — the three command-line tests and the zero-survives source check. The first replaces _run and asserts the parser hands the seeder {"command": "twt", "capital": Decimal("2500000"), "risk": None}.

- [x] F3: A test proves the capital write audits: setting it writes one `tw_config_audit` row whose
      `new_value` is the capital and whose `changed_by` is not empty, and re-running with the same
      number writes no second row (idempotent the way the settings form is).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests -k twt_sleeve -p no:cacheprovider 2>&1 | tail -2
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 7 passed, 1872 deselected in 3.32s. The audit assertion is the load-bearing one: one tw_config_audit row reading ("sleeve_capital_inr", "2500000.00", "seed"), and a second identical call adds no row.

- [x] F4: A negative capital is refused and writes nothing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests -k twt_sleeve_rejects -p no:cacheprovider 2>&1 | tail -2
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 1 passed, 1878 deselected in 3.22s. A negative capital raises before anything is written; the capital and the audit count are both unchanged afterwards.

- [x] F5: **The safety rail still holds.** A plain `seed twt` with no `--capital` still writes
      `sleeve_capital_inr` as an explicit `Decimal("0")`, and no assignment of
      `BASKFY_TWT_EXECUTION_ENABLED` to true exists in either tree. This is `gates/twt-root.md`
      R11's check, re-run against the changed seeder.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'flag_true=%s capital_seed=%s\n' "$(grep -rn 'BASKFY_TWT_EXECUTION_ENABLED' --include=*.py --include=*.example --include=*.ts decile-blueprint kite-momentum-rebalancer 2>/dev/null | grep -icE '=\s*true|default.?=.?True')" "$(grep -c 'sleeve_capital_inr=Decimal("0")' decile-blueprint/services/api/src/baskfy_api/seed.py)"
  EXPECT: /^flag_true=0 capital_seed=1$/m
  EVIDENCE: flag_true=0 capital_seed=1 against the changed seeder. The ₹0 is still written explicitly and the funding call sits behind `if capital is not None`.

- [x] F6: `twt-evening` and `twt-morning` are Beat entries pointing at the registered tasks, and
      **no Beat entry runs the sweep**. The negative half is the point of the row.
      ⚠️ **The first version of this CHECK could not run at all.** It was a multi-line `python -c`
      with embedded double quotes; the runner hands a CHECK to `sh -c` as one line, so it died on
      *"unexpected EOF while looking for matching quote"* — a failure that says nothing about the
      schedule. **And it had been marked green on the strength of running it by hand in a different
      shell**, which is the exact fault this whole session is about: evidence recorded for a check
      that could not execute where checks execute. Rewritten as a one-liner. F7 asserts the same
      facts from the loaded Celery app, which is the stronger reading; this row reads the source
      file, so a schedule present in one and absent in the other cannot pass both.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && F=decile-blueprint/services/worker/src/baskfy_worker/celery_app.py; printf 'evening=%s morning=%s sweep=%s tasks=%s\n' "$(grep -c '"twt-evening": {' $F)" "$(grep -c '"twt-morning": {' $F)" "$(grep -cE '^    "twt-[a-z-]*sweep[a-z-]*": \{' $F)" "$(grep '"task":' $F | grep -oE 'baskfy\.twt\.[a-z_]+' | sort -u | tr '\n' ',')"
  EXPECT: /^evening=1 morning=1 sweep=0 tasks=baskfy\.twt\.detect,baskfy\.twt\.evening,baskfy\.twt\.morning,$/m
  EVIDENCE: evening=1 morning=1 sweep=0 tasks=baskfy.twt.detect,baskfy.twt.evening,baskfy.twt.morning, — re-run through tools/gates/rerun.py, not by hand.

- [x] F7: A test asserts every TWT Beat entry names a task the worker actually registers, so a
      renamed task cannot leave a dead entry firing into nothing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests -k twt_beat -p no:cacheprovider 2>&1 | tail -2
  EXPECT: /[1-9][0-9]* passed/
  EVIDENCE: 4 passed, 1090 deselected in 0.64s, including test_the_sweep_is_not_on_a_timer.

- [x] F8: The decile tree lints and type-checks clean with the new module and its two test
      files in it. **The suites are not asserted here**: `gates/twt-root.md` R12 runs both
      trees in full and decides on their exit codes, and it is re-run after this work rather
      than duplicated into a second forty-minute command.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check . 2>&1 | tail -1 && uv run mypy 2>&1 | tail -1
  EXPECT: /Success: no issues found/
  EVIDENCE: ruff "All checks passed!" and mypy "Success: no issues found in 647 source files" (645 before this work, plus the two new test modules).


- [x] F9: **No CHECK in the TWT pack destroys a database a person owns.** A ledger is re-run by
      definition, so a check that writes must write somewhere disposable — and a check that takes
      its database URL from a default is pointing at whatever the environment says, which on a
      deploy box is not a test database.
      This row exists because `gates/twt-3.md` G2 did exactly that from TW3 until 12 Sep 2026: it
      ran `make migrate && make downgrade && make migrate` with no URL set, `BASKFY_DATABASE_URL`
      defaults to the developer's own `localhost:5433/baskfy`, and `make downgrade` is
      `alembic downgrade **base**`. Every run emptied that database. It is repaired to round-trip a
      throwaway; this check is what stops the next one. DECISIONS-TW TW11.4.
      Checked for vacuity: the pattern still matches the repaired line (which names its scratch
      database) and still returns 1 against the original text of G2.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -h "^  CHECK:" gates/twt-*.md | grep -E "make downgrade|alembic downgrade" | grep -vc "baskfy_migrate_check"
  EXPECT: /^0$/m
  EVIDENCE: 0 (12 Sep 2026). One CHECK in the pack executes a downgrade and it names `baskfy_migrate_check`; the only other line mentioning the word is twt-3 G1, which greps the migration file for the string `def downgrade` and writes nothing.

---

**⚠ Four of these CHECK lines could not have passed as first written, and the repair is recorded
rather than quietly applied.** F2, F3, F4 and F7 each ran pytest with `-q`. The decile tree's
`pyproject.toml` line 302 already sets `addopts = "-q --strict-markers --strict-config"`, so the
second `-q` makes `-qq` — and `-qq` suppresses the summary line entirely. The whole output becomes
one progress line ending `[100%]`, with no `7 passed` anywhere in it, so an `EXPECT` of
`/[1-9][0-9]* passed/` fails against a run in which every test passed.

It is the same fault `gates/twt-root.md` records four times and `gates/twt-10.md` twice: a check
that fails for a reason unrelated to the thing it tests. It was caught here by running the check
and reading its bytes rather than trusting that green tests meant a green gate — `od -c` on the
captured output, because `.......` and `7 passed` look equally like success in a terminal.

The `-q` is removed from all four; the repo's own `addopts` still supplies one.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: F<n> <reason> is the honest exit. -->
