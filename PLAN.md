# Plan: Baskfy smallcase-shaped product layer

Depth: tree 11 (user-requested; sparse at natural joints)   Mode: orchestrated
Budget note: SC0–SC12 is a multi-module product build. Unlazy v2 ties effort to per-leaf gates,
not artificial `2^N` expansion; every leaf remains a coherent ten-minute-or-larger deliverable.

## Contract

Fixed before fan-out:

- Interfaces: PostgreSQL tables are the `cb_` contract in `docs/smallcase/03-data-model.md`;
  money, prices, quantities, returns, volatility, and weights are `Decimal`/`numeric`, never
  floats. `CuratedBasketRepository.create_version(basket_id, draft)` is the only SC1 version
  write seam and consumes pure `VersionDraft`/`ConstituentDraft` values.
- Data ownership: market instruments/prices stay in `instrument`/`ohlcv_daily`; execution truth
  stays in the desk journal; broker holdings stay in desk snapshots. No SC leaf duplicates them.
  During parallel work no two leaves own the same file.
- Identity: `BASKFY_SOLE_USER_ID` resolves an `app_user.public_id`; order-shaped rows also carry
  the configured sole broker-account id. No request-selectable tenant identity exists this run.
- Versioning: basket versions and constituents are append-only; corrections create a new
  version. Exact weights sum to `Decimal("1.0000")` before any write.
- Error convention: domain validation raises typed `ValueError` subclasses; repository errors do
  not swallow database exceptions. API problem mapping starts only when the API surface lands.
- Security and scope: `DRY_RUN=true`; no live order, web order route, third-party broker OAuth,
  payment collection, public signup, or public basket publication. Track-B flags default off.
- Performance/memory: queries are set-based and indexed on foreign keys/list filters; jobs are
  idempotent by unique keys; time-series computation streams or bounds materialization; tests
  include query/count or timing evidence where the module has a performance budget.
- Verification: every leaf performs implementation, expert reread, defect hunt, and free polish;
  the driver reruns leaf checks and owns branch integration, STATUS/DECISIONS, and module commit.

## Tree

Tree 11 is represented sparsely: governance → run → product track → module → layer → component →
contract → implementation → verification → integration → release evidence. Natural leaves are
not split below a coherent deliverable.

- 1 SC run ................................................ `GATES.md`
  - 1.1 Governance / scope / safety (SC0) .................. complete in `2bee310`
  - 1.2 Product implementation
    - 1.2.1 Data foundation
      - 1.2.1.1 SC1 schema/domain/seeding .................. `gates/node-sc1.md`
        - schema + migration ............................... `gates/leaf-sc1-schema.md`
        - pure domain + repository ......................... `gates/leaf-sc1-domain-repository.md`
        - settings + sole-user/manager seeding ............. `gates/leaf-sc1-seeding.md`
      - 1.2.1.2 SC2 catalog computation/API ................ `gates/node-sc2.md`
        - exact catalog math + golden/property tests ........ `gates/leaf-sc2-math.md`
        - authenticated catalog/watchlist API ............... `gates/leaf-sc2-api.md`
        - idempotent metrics job + SCAN projection .......... `gates/leaf-sc2-job-seed.md`
      - 1.2.1.3 SC3 versions/rebalance/plans ............... expand before dispatch
      - 1.2.1.4 SC4 accounting ............................. expand before dispatch
    - 1.2.2 Product surfaces
      - SC5 discovery/detail ............................... expand before dispatch
      - SC6 investor surfaces .............................. expand before dispatch
      - SC7 SIP reminders .................................. expand before dispatch
      - SC8 create/customize ............................... expand before dispatch
      - SC9 engagement ..................................... expand before dispatch
    - 1.2.3 Safety and release
      - SC10 gating/dark machinery ......................... expand before dispatch
      - SC11 hardening/safety proof ........................ expand before dispatch
      - SC12 verification/final report ..................... expand before dispatch

## Status log

Append-only. One line per event; never rewrite earlier entries.

- 2026-08-23 SC0 verified and committed as `2bee310` on `codex/sc-run`.
- 2026-08-23 tree 11 orchestrated contract fixed after explicit `$unlazy` invocation.
- 2026-08-23 SC1 schema, domain/repository, and seeding leaves dispatched with disjoint ownership.
- 2026-08-23 SC1 leaves assigned separate disposable databases; parent integration retains `baskfy_sc_test`.
- 2026-08-23 SC1 schema, domain/repository, and seeding leaf ledgers independently reached 6/6 gates.
- 2026-08-23 parent integration corrected three missed formatting defects; 46 SC1 tests, migrate/seed/migrate, full lint/type checks, and 1,328-test desk regression passed.
- 2026-08-23 shared-host contention pushed the unchanged factor benchmark above 30 s at ten Polars threads; its unchanged budget passes with three bounded worker threads and no test weakening.
- 2026-08-23 SC1 parent gates are complete: legacy reference-seed checks pass, every application test has a green normal or contention-bounded profile, STATUS/DECISIONS are lossless, and the module is ready to commit.
- 2026-08-23 SC1 committed green as `3f9b5af`; SC2 expanded into exact math, catalog/API, and job/seed leaves with disjoint ownership.
- 2026-08-23 SC2's four leaves reached 24/24 gates after an independent audit found and closed
  calendar-boundary, watchlist-daily-change, post-publish sequencing, and historical-churn memory
  defects; no test, budget, or safety rule was weakened.
- 2026-08-23 parent integration passed 85 SC2 tests, 110 generated-client tests, full repository
  lint/type checks, migrate/seed idempotence, and the unchanged 1,328-test desk suite under
  `DRY_RUN=true`; SC2 is ready for its single green commit.

## SC2 fixed contract

- Pure math lives in `baskfy_core.curated_metrics`: Decimal inputs/results, no I/O/clock; exact
  `ceil(max(price/weight))`, whole-share floors, point-in-time version intervals, base-100
  chain-linking, window/CAGR metrics, annualized volatility, fixed buckets below 12 published
  baskets and deterministic terciles at/above 12.
- The authenticated API contract is `GET /api/v1/catalog`, `GET /api/v1/catalog/{slug}`,
  `GET /api/v1/managers/{slug}`, `GET /api/v1/collections/{slug}`, plus authenticated sole-user
  `GET /api/v1/watchlist`, `PUT /api/v1/watchlist/{basket_slug}`, and
  `DELETE /api/v1/watchlist/{basket_slug}`. Requests never supply a user id.
- Catalog query state is explicit and round-trippable: repeated `category`, plus `volatility`,
  `max_min_amount`, `rebalance_frequency`, `type`, `include_new`, `access`, and
  `sort=popular|returns|min_amount|newest`. Until SC9 supplies rankings, `popular` is a
  deterministic launched-date/id fallback and must say so in SC2's decision log.
- Catalog reads use latest metric/version rows in bounded set-based queries; no N+1 query path.
  Watchlist baselines use the latest stored chain-linked NAV and only the configured sole
  operator can write.
- `baskfy.compute.curated_metrics` runs after nightly publish on trading days, is idempotent per
  `(basket_id, as_of_date)`, performs no network or execution action, and persists rounded exact
  numerics. The first projection is `baskfy-momentum-scan`, source `SCAN`, strategy key
  `MomentumScan`, owned by `baskfy-engine`, with equal four-decimal weights using deterministic
  largest-remainder allocation. Projection tests consume a fixture scan; they do not infer live
  orders.
