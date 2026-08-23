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
      - 1.2.1.2 SC2 catalog computation/API ................ expand before dispatch
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
