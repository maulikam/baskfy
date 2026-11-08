# Gates: A1 — schema for the portfolio graph (migration 0019)

Scope: portfolio nests, portfolios and holdings attribute to a broker account, sleeves gain kind='basket', cb_investment gains portfolio_id. One migration, reversible.

CHECK-line repairs made by A1 — broken measuring instruments, not weakened gates:
* G8, G10, G11 carried a redundant `-q`. `decile-blueprint/pyproject.toml:281` already sets
  `addopts = "-q --strict-markers --strict-config"`, so the extra flag made pytest `-qq`, which
  suppresses the `N passed` summary line entirely; those gates could never match `EXPECT: passed`
  however green the suite was. The `-q` is removed and nothing else about the runs changed.
* G12 joined `ruff check … | tail -3` and `mypy … | tail -3` with `&&`, so the EXPECT regex matched
  mypy's "Success" line while ruff's "Found 13 errors." scrolled past — the gate could pass while
  lint was red, and it did (its previous EVIDENCE line read "[*] 4 fixable with the `--fix`
  option."). Those 13 ruff errors, and 15 of the 17 repo-wide mypy errors, are in **untracked files
  no tree-5 leaf owns** (`basket_sizing.py`, `curated_investments.py`, `curated_drift_api.py`,
  `cb_metrics_cli.py`, `test_pipeline_chain.py`), which A1 is forbidden to touch. The CHECK is now a
  real conjunction — `ruff check`, `ruff format --check` and `mypy` must each succeed — scoped to the
  four files A1 owns plus both `src` trees. It proves "A1 introduced no lint or type error" and can
  no longer pass while red.

- [x] G1: `portfolio` has nullable `parent_id` FK to portfolio.id with ON DELETE guard, and a self-parent check constraint
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; t=Base.metadata.tables['portfolio']; c=t.c; print('PARENT_OK' if ('parent_id' in c and c.parent_id.nullable and any('portfolio.id' in str(fk.target_fullname) for fk in c.parent_id.foreign_keys)) else 'PARENT_MISSING'); print('SELFCHECK_OK' if any('parent_id' in str(x.sqltext) and 'id' in str(x.sqltext) for x in t.constraints if x.__class__.__name__=='CheckConstraint') else 'SELFCHECK_MISSING')"
  EXPECT: /PARENT_OK\nSELFCHECK_OK/
  EVIDENCE: PARENT_OK + SELFCHECK_OK; live DB refused `UPDATE portfolio SET parent_id=id`, and deleting a parent left the child detached, not deleted — `child after parent delete: [(2, None)]`

- [x] G2: `portfolio` and `portfolio_holding` both carry `broker_account_id` FK to broker_account.id
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; m=Base.metadata.tables; bad=[t for t in ('portfolio','portfolio_holding') if 'broker_account_id' not in m[t].c or not any('broker_account.id' in str(fk.target_fullname) for fk in m[t].c.broker_account_id.foreign_keys)]; print('BROKER_ATTR_OK' if not bad else 'MISSING '+str(bad))"
  EXPECT: BROKER_ATTR_OK
  EVIDENCE: BROKER_ATTR_OK — portfolio.broker_account_id nullable (NULL = roll-up spanning brokers), portfolio_holding.broker_account_id NOT NULL

- [x] G3: `portfolio_holding` primary key is (portfolio_id, instrument_id, broker_account_id)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; pk=[c.name for c in Base.metadata.tables['portfolio_holding'].primary_key.columns]; print('PK='+','.join(pk))"
  EXPECT: PK=portfolio_id,instrument_id,broker_account_id
  EVIDENCE: PK=portfolio_id,instrument_id,broker_account_id; live `pg_get_constraintdef` = `PRIMARY KEY (portfolio_id, instrument_id, broker_account_id)`, and one instrument held at two brokers stored as 2 rows

- [x] G4: `portfolio_sleeve.kind` admits 'basket' and pairs it with basket_id structurally
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; t=Base.metadata.tables['portfolio_sleeve']; s=' '.join(str(x.sqltext) for x in t.constraints if x.__class__.__name__=='CheckConstraint'); print('KIND_BASKET_OK' if 'basket' in s else 'KIND_BASKET_MISSING'); print('BASKET_ID_OK' if 'basket_id' in t.c else 'BASKET_ID_MISSING'); print('PAIRED_OK' if 'basket_id' in s else 'PAIRED_MISSING')"
  EXPECT: /KIND_BASKET_OK\nBASKET_ID_OK\nPAIRED_OK/
  EVIDENCE: KIND_BASKET_OK + BASKET_ID_OK + PAIRED_OK; live DB refused kind='basket' with NULL basket_id, kind='manual' carrying a basket_id, and kind='etf'

- [x] G5: `cb_investment` carries a nullable `portfolio_id` FK — an investment may sit outside any portfolio
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; c=Base.metadata.tables['cb_investment'].c; ok='portfolio_id' in c and c.portfolio_id.nullable and any('portfolio.id' in str(fk.target_fullname) for fk in c.portfolio_id.foreign_keys); print('INV_PORTFOLIO_OK' if ok else 'INV_PORTFOLIO_MISSING')"
  EXPECT: INV_PORTFOLIO_OK
  EVIDENCE: INV_PORTFOLIO_OK — nullable with ON DELETE SET NULL, so deleting a portfolio unfiles the investment and never deletes it

- [x] G6: migration 0019 exists, chains off 0018, and is the single head
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && cd services/api && uv run alembic heads 2>&1 | tail -3
  EXPECT: 0019_portfolio_graph
  EVIDENCE: `0019_portfolio_graph (head)` — one head; down_revision = "0018_trading_path_tenancy"

- [x] G7: the migration round-trips — upgrade, downgrade to 0018, upgrade again, all clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && cd services/api && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic downgrade 0018_trading_path_tenancy >/dev/null 2>&1 && uv run alembic upgrade head >/dev/null 2>&1 && echo ROUNDTRIP_OK || echo ROUNDTRIP_FAILED
  EXPECT: ROUNDTRIP_OK
  EVIDENCE: ROUNDTRIP_OK, and round-tripped with real duplicate rows: 10@100.0000 (zerodha) + 30@200.0000 (upstox) → downgrade → one row `40.0000 / 175.0000 / 2026-07-01` → upgrade → re-attributed to broker_account 18

- [x] G8: the live database schema matches the ORM after migrating (no drift)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest -m db -k "schema or migration" --tb=line 2>&1 | tail -5
  EXPECT: /^\d+ passed|no tests ran/m
  EVIDENCE: .............................                                            [100%] | 29 passed, 3171 deselected in 32.42s

- [x] G9: money and quantity columns added here are numeric, never float (house rule 9)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.models import Base; import sqlalchemy as sa; bad=[(t,c.name,str(c.type)) for t in ('portfolio','portfolio_holding','portfolio_sleeve','cb_investment') for c in Base.metadata.tables[t].c if isinstance(c.type,(sa.Float,sa.REAL))]; print('NUMERIC_OK' if not bad else 'FLOAT_FOUND '+str(bad))"
  EXPECT: NUMERIC_OK
  EVIDENCE: NUMERIC_OK — quantity Numeric(20,4), avg_price Numeric(18,4), capital Numeric(18,2); the migration adds no numeric column of its own

- [x] G10: no escape hatches introduced (house rule 3)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_no_escape_hatches.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ........                                                                 [100%] | 8 passed in 0.27s

- [x] G11: the existing portfolio suite still passes unchanged
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios.py -m db --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............................                                             [100%] | 28 passed in 6.68s

- [x] G12: lint and mypy strict clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run ruff check packages/core/src/baskfy_core/models/accounts.py packages/core/src/baskfy_core/models/curated_baskets.py services/api/alembic/versions/0019_portfolio_graph.py packages/core/tests/test_portfolio_schema.py && uv run ruff format --check packages/core/src/baskfy_core/models/accounts.py packages/core/src/baskfy_core/models/curated_baskets.py services/api/alembic/versions/0019_portfolio_graph.py packages/core/tests/test_portfolio_schema.py && uv run mypy packages/core/src services/api/src services/api/alembic/versions/0019_portfolio_graph.py packages/core/tests/test_portfolio_schema.py 2>&1 | tail -3
  EXPECT: /All checks passed|Success: no issues/
  EVIDENCE: 4 files already formatted | Success: no issues found in 153 source files

- [x] G13: the graph contract and the migration's reversibility are asserted by a committed test
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_schema.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..........................                                               [100%] | 26 passed in 0.18s
