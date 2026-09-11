# TW3 — the `tw_` schema, its migration, and the settings ceilings

**Plan:** `docs/twt/06-module-plan.md` § TW3. **Spec:** `docs/twt/03-data-model.md` is the schema,
column for column; `docs/twt/04-business-rules.md` supplies the seed's defaults; `02` §4 supplies
the ceilings. **House rule 7:** ingestion and seeding are idempotent — re-running any job produces
identical rows.

**Ships:** `services/api/alembic/versions/0041_twt.py` (revising `0040_vbt_scan_run`, WITH a
downgrade), `packages/core/src/baskfy_core/models/twt.py`, the seed, `.env.example` entries, and
the tests.

**The shape is VBT-1's**, which shipped: read `0037_vbt.py` and `0040_vbt_scan_run.py` and the
`models/` module beside them, and copy their idiom.

- [x] G1: `0041_twt` exists, revises `0040_vbt_scan_run`, and carries a real downgrade.
  CHECK: cd decile-blueprint && grep -nE "^(revision|down_revision)" services/api/alembic/versions/0041_twt.py && grep -c "def downgrade" services/api/alembic/versions/0041_twt.py
  EXPECT: /down_revision.*0040_vbt_scan_run/
  EVIDENCE: `revision: str = "0041_twt"` (line 73), `down_revision: str | None = "0040_vbt_scan_run"`
  (line 74), `grep -c "def downgrade"` = 1. `alembic heads` said `0040_vbt_scan_run (head)` before
  the file existed and `0041_twt (head)` after, so the number `03` predicted was still free.

- [x] G2: **The migration round-trips.** `upgrade` → `downgrade` → `upgrade` leaves the same
      schema. A downgrade that does not actually undo is worse than none, because it is the path
      somebody takes at 3am.
  CHECK: cd decile-blueprint && make migrate 2>&1 | tail -2 && make downgrade 2>&1 | tail -2 && make migrate 2>&1 | tail -2
  EXPECT: /Running upgrade|Target database is not up to date|head/
  EVIDENCE: `make migrate` -> `make downgrade` (to base) -> `make migrate`, then
  `select version_num from alembic_version` = `0041_twt` and 13 `tw_` tables. The round trip was
  also compared **column by column** rather than counted: `information_schema.columns`,
  `pg_constraint` and `pg_indexes` dumped before the downgrade and after the re-upgrade diffed
  empty — **193 columns, 103 constraints, 28 indexes identical**. The single-step
  `alembic downgrade -1` leaves 0 `tw_` tables, which is what proves the downgrade undoes rather
  than merely runs. It also caught the real trap: `use_alter=True` inside `op.create_table` makes
  SQLAlchemy silently OMIT the `tw_position.order_id` -> `tw_order` foreign key, so the migration
  adds it with an explicit `op.create_foreign_key` and the downgrade drops it first.

- [x] G3: Every table, column, type and constraint of `03` exists as written — including
      `tw_position.stop_price` never falling, `tw_plan_skip.reason` in its enum, and
      `tw_order.side` in `{BUY, SELL}`.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_schema.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `123 passed in 2.44s` (`packages/core/tests/test_twt_schema.py`; the db half runs, it does not skip). Beyond the columns, the constraints are proved by *triggering* them: a stop below `initial_stop`, `close_reason='EMA_EXIT'`, a `next_trigger` with no `next_trigger_for`, `side='SHORT'`, an unknown `tw_plan_skip.reason`, an order for a signal nobody stored, a second order for the same signal, a thin session with an OPEN gate, a measured session with no percentage, and a plan that expired before it was built - each raises `IntegrityError` naming its own constraint. Deleting the `app_user` takes the whole sleeve with it, composite foreign key included.

- [x] G4: **The seed ships `sleeve_capital_inr = 0`.** Zero is the value that means "nothing
      plans", and it is the safety rail: the sleeve cannot size a line until Maulik enters the
      capital himself. An agent never sets it.
  CHECK: cd decile-blueprint && grep -rn "sleeve_capital_inr" services/api/src/baskfy_api/seed*.py services/api/src/baskfy_api/seed/ 2>/dev/null | head -5
  EXPECT: /sleeve_capital_inr/
  EVIDENCE: `services/api/src/baskfy_api/seed.py:722` "One ``tw_config`` row for the sole tenant, with
  ``sleeve_capital_inr = 0`` (TW3)" and `:751` `insert(TwConfig).values(user_id=user_id,
  sleeve_capital_inr=Decimal("0"), updated_by="seed")` - written **explicitly** rather than left to
  the column default, because the one number this function must never get wrong should be visible
  in it. Asserted on a real database: a freshly seeded row reads `0.00`, 10 slots, 12.50, 20.00,
  20.00, 10 first-live entries.

- [x] G5: The seed is **idempotent** (house rule 7) — running it twice changes no rows.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "twt and (seed or idempot)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `4 passed, 4014 deselected in 2.55s` (3 were TW3's when first run; the selection
  also catches a TW1 test that landed in the same tree). The idempotence test is not "the row still exists":
  it seeds, then sets capital 2500000.00 / stop 22.00 / trail 25.00, then seeds **again**, and
  asserts all three survived and exactly one row exists. A seeder written with `ON CONFLICT DO
  UPDATE` would pass a naive check and silently reset a person's sleeve on the next deploy - and
  on this sleeve resetting the trail would rearm every stop in the book at a different level.

- [x] G6: A settings write above a ceiling is refused **with the ceiling named**, and — the case
      that is easy to get backwards — a `trail_pct` **below** `BASKFY_TWT_TRAIL_PCT_MIN` is
      refused too. Decision TW0.5: the measured cliff is in the tightening direction, so this one
      ceiling is a floor.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "twt and (ceiling or bound or refus)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `88 passed, 3930 deselected in 6.34s` (the count drifts upward as the TW1 sibling's
  tests land in the same tree; 53 of them are `test_twt_ceilings.py`). Above a ceiling: 422 `setting-above-ceiling`
  carrying `field`, `requested`, `ceiling` and `env_var`, the ceiling in the detail string.
  **Below the floor: 422 `setting-below-floor`** carrying `floor` and
  `env_var=BASKFY_TWT_TRAIL_PCT_MIN`. Three tests rather than one, because one would have passed
  on the mirror image: `trail_pct=15.00` refuses, `18.00` is allowed, and **20/25/30/40 are all
  allowed** - a ceiling in this slot would refuse a setting that is merely conservative while
  accepting the 15 % that halves the CAGR. On the database, a two-field patch that crosses either
  bound writes neither field and leaves `tw_config_audit` empty.

- [x] G7: The ceilings are in `.env.example` as system-only env, `BASKFY_TWT_TRAIL_PCT_MIN` among
      them, and **no flag is set true and no capital is set** anywhere this module writes.
  CHECK: cd decile-blueprint && grep -nE "BASKFY_TWT" .env.example | head -10 && grep -rniE "BASKFY_TWT_EXECUTION_ENABLED\s*=\s*true|sleeve_capital_inr\s*=\s*[1-9]" .env.example services/api/alembic/versions/0041_twt.py | wc -l
  EXPECT: /BASKFY_TWT_TRAIL_PCT_MIN/
  EVIDENCE: Six `BASKFY_TWT_` lines in `decile-blueprint/.env.example` (137-153) and the same six in
  the monorepo root's (227-243): `EXECUTION_ENABLED=false`, `NIGHTLY_ENABLED=true`,
  `MAX_OPEN_POSITIONS_MAX=15`, `MAX_POSITION_PCT_MAX=15.00`, `STOP_PCT_MAX=25.00`,
  **`TRAIL_PCT_MIN=18.00`** - every one marked `# system-only`. The second grep
  (`EXECUTION_ENABLED=true` or a non-zero `sleeve_capital_inr`) returns **0**. A test widens that
  scan to every committed `.env*` under the monorepo, and the comment block says
  "AND THIS ONE IS A FLOOR, NOT A CEILING - note the _MIN" in both files.

- [x] G8: `test_schema_matches_docs.py` is extended to `03`, so the schema and the document
      cannot drift apart silently.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_schema_matches_docs.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `381 passed in 0.50s`. `DOCUMENTED_TABLES` gains the thirteen `tw_` tables with their
  primary keys (so `test_no_undocumented_tables` now covers them),
  `test_twt_tables_are_recorded_in_docs` requires each to be named in `docs/twt/03`, and 97
  parametrised cases assert that every column `03` names is modelled **and still named in the
  document**. Four columns the document writes in shorthand (`week_close_0/1/2`,
  `expires_at = built_at + 30 min`) are mapped to the document's own literal rather than the check
  being loosened.

- [x] G9: The whole core suite is green — TW3 broke nothing.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `3929 passed, 4 skipped in 242.93s`. Baseline before TW3 was `3815 passed, 3 skipped`; the difference is TW3's 176 tests plus the TW1 sibling's, landing in the same tree.

- [x] G10: `make lint` clean — ruff, ruff format, mypy --strict.
  CHECK: cd decile-blueprint && make lint 2>&1 | tail -6
  EXPECT: /Success|All checks passed/
  EVIDENCE: Ruff over the whole tree: **one** error, and it is not TW3's -
  `services/worker/src/baskfy_worker/tasks/vbt_rescan.py:81` is 101 characters, committed at
  `29ce944` (VB13.4) and unmodified in the working tree, so `make lint` was already red before
  this module began and stops at its first step. TW3 left it alone per its own brief (name the
  file, do not fix it) and ran the steps individually: `ruff check . --exclude
  services/worker/.../vbt_rescan.py` -> **All checks passed!**; `ruff format --check .` ->
  **725 files already formatted**; `uv run mypy` -> **Success: no issues found in 621 source
  files**. `pnpm -r run lint` also fails, also pre-existing and also not TW3's (no TypeScript was
  touched): `apps/web/src/components/portfolio/__tests__/no-internals.test.tsx:68` fails `tsc`,
  committed at `dd9cc73` (PC-INT). Every file TW3 wrote or edited is ruff-clean, format-clean and
  mypy-strict-clean.

<!--
A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit.
Never weaken a test to make this file pass.
-->

---

**10 of 10, 11 Sep 2026.** Two caveats stated in the open rather than buried in G10: `make lint`
as a single command does **not** exit 0 in this tree, and did not before TW3 either - one ruff
line in `vbt_rescan.py` (VB13.4, `29ce944`) and one `tsc` error in `no-internals.test.tsx`
(PC-INT, `dd9cc73`). Both are one-line fixes in files this module never opened, and both block
`make lint` for every module after this one.

**Three decisions recorded in `docs/twt/DECISIONS-TW.md`, all tagged UNREVIEWED.** TW3.1 the
vocabulary tuples are transcribed from `03`/`04` and pinned to them rather than imported from
`baskfy_core.twt`, which TW1 was writing in a parallel session. TW3.2 `tw_position.entry_adj_factor`
exists although `03` §5's column table omits it, because `04` §7.3 and TW0.7 read it by name.
TW3.3 a below-floor refusal is its own problem type - **and its place in the enum is load-bearing**,
because declared after the ceiling's it silently rewrote the 422 description on every route in the
published OpenAPI document.

**What TW3 did not do, on purpose.** It set no capital (`sleeve_capital_inr` is 0 and stays 0 -
`NEEDS-MAULIK` T1), flipped no flag (`BASKFY_TWT_EXECUTION_ENABLED=false` everywhere, asserted by
a scan of every committed `.env*`), added no auto-execute setting, wrote no order path, mounted no
router, and touched nothing under `packages/core/src/baskfy_core/twt/`.
