# Gates: node 1.1.2 — the schema, so criterion 2 stops being a promise

Scope: `portfolio.kind` and `portfolio.source` (§3, §4.1), and a database constraint that makes
"a holding belongs to exactly one capital portfolio" (§4.2, criterion 2) **impossible to violate
in SQL**, not merely refused by Python.

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/portfolio-spine-1.1.2.md
```

## Why this node is next

Node 1.1.1 made the domain refuse a double allocation. `portfolio_holding`'s primary key still
*permits* the row. So criterion 2 is enforced in one language and violable in the other, and the
database is the one that survives a bug in the other.

## The design, and why not the obvious one

The constraint needs to say: *for a given `(instrument_id, broker_account_id)`, at most one row
whose portfolio is CAPITAL.* The awkward part is that "is capital" lives on `portfolio`, and a
Postgres unique index cannot reach across a table.

The obvious fix is a trigger. Rejected: a trigger is procedural, fires per row, and is silently
skipped by `COPY ... FREEZE` and by a disabled-trigger session — the exact circumstances of a bulk
import, which is how a broker sync will one day write.

Taken instead, the composite-foreign-key pattern:

1. `portfolio` gains `kind`, and a `UNIQUE (id, kind)` — redundant against the primary key, and
   existing only so it can be the target of (2).
2. `portfolio_holding` gains `portfolio_kind`, with a composite FK
   `(portfolio_id, portfolio_kind) -> portfolio(id, kind)` `ON UPDATE CASCADE`. The denormalised
   column therefore *cannot* disagree with its portfolio: the foreign key is the synchroniser.
3. A **partial unique index** on `(instrument_id, broker_account_id) WHERE portfolio_kind =
   'CAPITAL'`. Monitoring rows are simply not in the index, which is exactly §4.1 — lenses
   overlap freely and never enter a total.

The cascade earns its keep in both directions: flipping a portfolio CAPITAL -> MONITORING drops
its holdings out of the index automatically, and flipping MONITORING -> CAPITAL when that would
collide **fails the flip** rather than corrupting the ledger. That is the behaviour §4.1 wants
and it costs no application code.

## Facts measured before writing these gates

- `portfolio_holding` has **0 rows**, so the restructure carries no data-migration risk.
- `portfolio` has 3 rows, `portfolio_sleeve` 8, `broker_account` 2 — so new NOT NULL columns need
  a default and a backfill, not a rewrite.
- Alembic head is `0020_manager_identity`; this node is `0021`.
- No active session on the `baskfy` database at survey time.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.

---

## A — the migration

- [x] G1: `0021` applies cleanly onto `0020_manager_identity`, and `portfolio` carries `kind` and
      `source` with the §3 vocabulary and nothing else — a CHECK constraint, not a convention.
  CHECK: bash tools/portfolio/schema-check.sh columns
  EXPECT: /COLUMNS OK/
  EVIDENCE: an unknown kind is: refused by CHECK | COLUMNS OK

- [x] G2: **Criterion 2 is enforced by the database.** Inserting the same
      `(instrument_id, broker_account_id)` into two CAPITAL portfolios is rejected by Postgres.
      Proven by attempting it, not by reading the DDL.
  CHECK: bash tools/portfolio/schema-check.sh criterion2
  EXPECT: /CRITERION 2 ENFORCED/
  EVIDENCE (adversarial, added after the gate passed — the design's whole claim was that an
    index beats a trigger, so the two ways a trigger loses were tried directly):
      bulk `COPY ... FROM STDIN`      -> ERROR: duplicate key value violates unique constraint
                                         "uq_portfolio_holding_one_capital_portfolio"
      `SET session_replication_role = replica` (which disables triggers **and** FK checks)
                                      -> same refusal, verbatim
      moving a holding A -> B (legit) -> UPDATE 1, one row remains
    A trigger would have been bypassed by both of the first two. This is the evidence for the
    design choice, not merely for the constraint.
  EVIDENCE: same holding into capital portfolio B: REFUSED by unique index | CRITERION 2 ENFORCED

- [x] G3: Monitoring views still overlap freely — the same holding in a capital portfolio *and*
      in any number of monitoring views is accepted, because §4.1 requires exactly that.
  CHECK: bash tools/portfolio/schema-check.sh monitoring
  EXPECT: /MONITORING OVERLAP OK/
  EVIDENCE: first lens: INSERT 0 1   second lens: INSERT 0 1 | MONITORING OVERLAP OK

- [x] G4: The denormalised `portfolio_kind` cannot drift from its portfolio: setting it by hand to
      a value the portfolio does not have is rejected, and flipping a portfolio's kind carries its
      holdings with it. Both directions of the cascade are exercised, including the flip that must
      **fail** because it would collide.
  CHECK: bash tools/portfolio/schema-check.sh cascade
  EXPECT: /CASCADE OK/
  EVIDENCE: flipping back into a collision: REFUSED | CASCADE OK

- [x] G5: `downgrade` returns the schema to exactly `0020` — the columns, the constraints and the
      index are all removed, and re-upgrading works. A migration that cannot be rolled back is a
      migration nobody can deploy on a Friday.
  CHECK: bash tools/portfolio/schema-check.sh roundtrip
  EXPECT: /ROUNDTRIP OK/
  EVIDENCE: after re-upgrade: 3 column(s) back | ROUNDTRIP OK

## B — the ORM agrees with the database

- [x] G5b: A database that **already** breaks criterion 2 is refused with a message naming the
      symbol, the broker account and the portfolios — not a raw `UniqueViolationError` delivered
      halfway through. Proven by building the violation at `0020` and upgrading into it.
      This gate exists because the first round-trip attempt failed exactly this way, on data an
      earlier subcommand had left behind: a real deployment would have hit it too.
  CHECK: bash tools/portfolio/schema-check.sh preflight
  EXPECT: /PREFLIGHT OK/
  EVIDENCE: The migration will not choose a winner: which portfolio keeps a holding decides whose return series it belongs to, and only the account's owner can answer that. Resolve each case by removing the holdi

- [x] G6: The models carry the same truth as the migration, and the repo's own
      schema-matches-docs test still passes.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest packages/core/tests/test_schema_matches_docs.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..................                                                       [100%] | 162 passed in 0.24s

- [x] G7: `PortfolioKind` and `PortfolioSource` have exactly one definition in the codebase — the
      ledger's. The ORM imports them rather than restating the strings, so the database and the
      domain cannot drift apart.
  CHECK: bash tools/portfolio/schema-check.sh single-vocabulary
  EXPECT: /ONE VOCABULARY/
  EVIDENCE: kinds match: True  sources match: True | ONE VOCABULARY

- [x] G8: House rules hold on everything this node adds: no `type: ignore`, no `Any`, `ruff`,
      `ruff format` and `mypy` clean, and the repo's escape-hatch scanner passes.
  CHECK: bash tools/portfolio/house-rules-1.1.2.sh
  EXPECT: /HOUSE RULES OK/
  EVIDENCE: ORM carries kind, source, portfolio_kind and the composite unique key | HOUSE RULES OK

## C — nothing else broke

- [x] G9: The Python suites that touch these tables are green.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest packages/core -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .........................................                                [100%] | 1695 passed, 2 skipped in 66.16s (0:01:06)

- [x] G10: The dev database is left on head with the data it started with — 3 portfolios,
      8 sleeves, 2 broker accounts — and the desk tree and `frozen/` are untouched.
  CHECK: bash tools/portfolio/schema-check.sh dev-intact
  EXPECT: /DEV DB INTACT/
  EVIDENCE: desk + frozen changed files: 0 | DEV DB INTACT

- [x] G11: `PLAN.md`'s status log records this node, and every number in the report is
      re-measured at report time with the ledger pasted N-of-N.
  EVIDENCE: `PLAN.md` records node 1.1.2 as done, the three defects found while proving it, and
    names 1.1.3 as next. Numbers re-measured immediately before reporting:
      dev database head            0021_allocation_ledger
      portfolios backfilled        3, all CAPITAL / HOLDING_GROUP
      the enforcing index          CREATE UNIQUE INDEX uq_portfolio_holding_one_capital_portfolio
                                   ON portfolio_holding (instrument_id, broker_account_id)
                                   WHERE portfolio_kind = 'CAPITAL'
      migration                    209 lines
      schema checks                8 of 8 pass from a freshly migrated database, in order
      ledger tests                 43 passed
      whole core suite             1695 passed, 2 skipped
      dev data                     3 portfolios, 8 sleeves, 2 broker accounts — unchanged
    Still not claimed: criteria 3, 7 and 8 remain Phase 2. This node moved criterion 2 from
    "enforced in Python" to "enforced in Postgres"; it did not add a criterion.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
