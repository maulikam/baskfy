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

- [ ] G1: `0041_twt` exists, revises `0040_vbt_scan_run`, and carries a real downgrade.
  CHECK: cd decile-blueprint && grep -nE "^(revision|down_revision)" services/api/alembic/versions/0041_twt.py && grep -c "def downgrade" services/api/alembic/versions/0041_twt.py
  EXPECT: /down_revision.*0040_vbt_scan_run/
  EVIDENCE: pending

- [ ] G2: **The migration round-trips.** `upgrade` → `downgrade` → `upgrade` leaves the same
      schema. A downgrade that does not actually undo is worse than none, because it is the path
      somebody takes at 3am.
  CHECK: cd decile-blueprint && make migrate 2>&1 | tail -2 && make downgrade 2>&1 | tail -2 && make migrate 2>&1 | tail -2
  EXPECT: /Running upgrade|Target database is not up to date|head/
  EVIDENCE: pending

- [ ] G3: Every table, column, type and constraint of `03` exists as written — including
      `tw_position.stop_price` never falling, `tw_plan_skip.reason` in its enum, and
      `tw_order.side` in `{BUY, SELL}`.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_schema.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: **The seed ships `sleeve_capital_inr = 0`.** Zero is the value that means "nothing
      plans", and it is the safety rail: the sleeve cannot size a line until Maulik enters the
      capital himself. An agent never sets it.
  CHECK: cd decile-blueprint && grep -rn "sleeve_capital_inr" services/api/src/baskfy_api/seed*.py services/api/src/baskfy_api/seed/ 2>/dev/null | head -5
  EXPECT: /sleeve_capital_inr/
  EVIDENCE: pending

- [ ] G5: The seed is **idempotent** (house rule 7) — running it twice changes no rows.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q -k "twt and (seed or idempot)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: A settings write above a ceiling is refused **with the ceiling named**, and — the case
      that is easy to get backwards — a `trail_pct` **below** `BASKFY_TWT_TRAIL_PCT_MIN` is
      refused too. Decision TW0.5: the measured cliff is in the tightening direction, so this one
      ceiling is a floor.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q -k "twt and (ceiling or bound or refus)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G7: The ceilings are in `.env.example` as system-only env, `BASKFY_TWT_TRAIL_PCT_MIN` among
      them, and **no flag is set true and no capital is set** anywhere this module writes.
  CHECK: cd decile-blueprint && grep -nE "BASKFY_TWT" .env.example | head -10 && grep -rniE "BASKFY_TWT_EXECUTION_ENABLED\s*=\s*true|sleeve_capital_inr\s*=\s*[1-9]" .env.example services/api/alembic/versions/0041_twt.py | wc -l
  EXPECT: /BASKFY_TWT_TRAIL_PCT_MIN/
  EVIDENCE: pending

- [ ] G8: `test_schema_matches_docs.py` is extended to `03`, so the schema and the document
      cannot drift apart silently.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_schema_matches_docs.py -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G9: The whole core suite is green — TW3 broke nothing.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -q 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G10: `make lint` clean — ruff, ruff format, mypy --strict.
  CHECK: cd decile-blueprint && make lint 2>&1 | tail -6
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

<!--
A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit.
Never weaken a test to make this file pass.
-->
