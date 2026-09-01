# Gates: leaf C2-holdings-sync

Scope: Phase 1 broker holdings sync — the first step of the whole chain

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The sync exists and its tests pass against the fixture provider.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_holdings_sync.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......................                                                   [100%] | 22 passed in 3.93s

- [x] G2: Criterion 4 through the sync: a sell attributes or raises an item, never silently.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_holdings_sync.py -p no:randomly -k "sell or reconcil or attribut" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......                                                                   [100%] | 6 passed, 16 deselected in 1.86s

- [x] G3: New holdings land UNALLOCATED (§6.6's first-run experience), never guessed into a portfolio.
  CHECK: cd decile-blueprint && export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_tree3_test" && uv run pytest services/worker/tests/test_holdings_sync.py -p no:randomly -k "unalloc or new or inflow" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 20 deselected in 1.31s

- [x] G4: The provider suite still passes — the port change broke no existing adapter.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .....................................................                    [100%] | 269 passed in 8.35s

- [x] G5: No live broker fetch is claimed that did not happen, and no credential was faked.
  EVIDENCE: **LIVE BROKER FETCH: NO** — stated plainly by the leaf and consistent with what the
    environment allows. No Kite call was made and no credential was read, written or invented.
    The whole chain is proven against `FixtureHoldingsProvider` with the suite's socket block
    armed; `KiteProvider.broker_holdings` is exercised only through an injected fake client.
    The two hands-only prerequisites — a daily Kite token (login + 2FA) and knowing which
    `broker_account.id` it belongs to — are appended to `NEEDS-MAULIK.md` §16. Neither blocks
    further engineering; both block a *live* run, and this run did not perform one.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
