# DECISIONS-SC — judgement calls of the smallcase run

Same convention as `docs/DECISIONS-MERGE.md`: numbered by module, each entry records the
context, the choice taken, the rejected alternatives and why, and how to reverse it.
Decisions made without Maulik's review are tagged **⚠ UNREVIEWED** until he clears them.

Two decisions were pre-taken in the pack itself, so the run doesn't stall on them — they
are recorded here for review like any other:

## PACK.1 — Volatility thresholds under single-tenant reality · ⚠ UNREVIEWED

`04-business-rules.md` §3: smallcase buckets by comparing across its whole catalog; with a
handful of published baskets terciles are meaningless. Fixed annualized thresholds
(LOW < 15% ≤ MED < 25% ≤ HIGH) apply until 12 baskets are PUBLISHED, then terciles take
over. Rejected: terciles from day one (degenerate), copying smallcase's unknown internal
cutoffs (unknowable). Reversal: constants in one config location; recompute job re-buckets
everything on change.

## PACK.2 — Web app generates plans but never executes · (not reversible this run)

`02-scope-and-gating.md` Track C: this is the charter's D3 gate plus desk non-negotiable
#1 restated for the new surfaces, not a fresh judgement — recorded here so no later module
"discovers" flexibility in it. Reversal path exists only through a written D3 answer in
`docs/DECISIONS-MERGE.md`, after which the execution-in-web design becomes its own planned
phase (it is NOT part of SC0–SC12).

---

(Module entries follow, newest at the bottom.)

## SC0 — baseline: M41 sits beside the SC run, not inside it ⚠ UNREVIEWED

**Context.** SC0 found uncommitted M41 broker-catalog work and the `docs/smallcase/` pack on
the same dirty tree. The broker grid is product furniture the SC UI will eventually link;
live OAuth is already D3-gated.

**Taken.** Treat M41 as a **merge-track** deliverable (commit `M41: green — …`) and SC0 as
**docs-only baseline** (commit `SC0: green — …`). SC modules do not absorb M41's gate or
`/brokers` page into `cb_*` schema.

**Rejected.** Squashing M41 into SC0 (muddies the two ledgers). Deleting M41 to get a clean
tree (throws away the connect UI the product ask wanted).

**Reversal.** Revert the M41 commit; SC0 STATUS baselines still hold.

## SC1 — extend M22 `/baskets`, do not duplicate · ⚠ UNREVIEWED

**Context.** M22/M30 already serve `/baskets` from `basket_snapshot` — a nightly JSON cache of the
live MomentumScan build. SC1 introduces `cb_basket` as the curated **product** layer (managers,
versions, constituents, collections, investments). The two must coexist until SC5 merges the read
surfaces.

**Taken.** **Extend, don't duplicate.** `basket_snapshot` and `GET /api/v1/baskets` stay as the desk
operator's live scan basket. `cb_basket` with `source=SCAN` is the catalog projection cut from those
runs (deterministic: same scan in → same version out). `source=MANUAL` covers operator- and
user-created private baskets. No second MomentumScan page or duplicate instrument table in SC1–SC2.

**Rejected.** Renaming or dropping `basket_snapshot` in SC1 (breaks the desk before SC5 is ready).
Mirroring every nightly snapshot into `cb_basket_version` immediately (SC3's rebalance engine owns
the cut). A separate `cb_instrument` table (house rule: join the screener's `instrument` master).

**Reversal.** Drop `cb_*` migration 0014 and models; `/baskets` continues unchanged.

## SC1 — weight sum enforced in domain, not Postgres · ⚠ UNREVIEWED

**Context.** docs/smallcase/03 asks for `CHECK sum(weight) per version = 1.0000` but Postgres cannot
express a per-parent aggregate check without triggers.

**Taken.** Service-layer assertion via `baskfy_core.curated_baskets.assert_weights_sum_to_one`,
tested in `test_curated_baskets.py` and `test_curated_schema.py`. Constituent weight storage is
`numeric(7,4)`; tolerance `0.00005` after quantize-to-4dp.

**Rejected.** A deferrable trigger in SC1 (more moving parts before the insert path exists in SC3).
Storing unnormalized weights and fixing at read time (violates house rule 8).

**Reversal.** Add a trigger migration; keep the pure assert as a fast-fail before the DB round-trip.

## SC1 — enum casing matches the smallcase spec · ⚠ UNREVIEWED

**Context.** Billing `plan`/`subscription` enums in docs/04 use lowercase (`active`, `month`). The
smallcase pack uses uppercase (`ENGINE`, `FREE`, `PUBLISHED`).

**Taken.** All `cb_*` check constraints use the uppercase vocabulary from docs/smallcase/03 verbatim.
The dormant billing tables are untouched.

**Rejected.** Lowercasing cb enums to match docs/04 (would diverge from the product spec and every
mock in the smallcase pack).

**Reversal.** One migration to rewrite constraints and seed rows; no production data yet.

## SC1 — sole user resolved from env or e2e account · ⚠ UNREVIEWED

**Context.** Track A is single-tenant; every user-scoped row carries `user_id` equal to
`BASKFY_SOLE_USER_ID` (docs/smallcase/02).

**Taken.** `SOLE_USER_ENV = "BASKFY_SOLE_USER_ID"` in core; `baskfy_api.curated_seed.resolve_sole_user_id`
reads the env var when set, otherwise upserts the e2e account via `seed_e2e_account` and returns its id.
SC1 seeds managers only — no `cb_investment` rows until SC4.

**Rejected.** Hard-coding user id `1` (breaks fresh databases where the first user is not id 1).
Inferring "the only user" implicitly in queries (forbidden by docs/smallcase/03 rule 1).

**Reversal.** Delete `curated_seed.resolve_sole_user_id`; callers pass explicit ids in tests only.

## SC2 — explore API prefix and transcendental float boundary ⚠ UNREVIEWED

**Taken.** Catalog lives at `/api/v1/explore` (and watchlist routes on the same router), matching
docs/smallcase/05 route names rather than `/cb/baskets`. CAGR uses `math.log`/`math.exp` at the
fractional-power boundary then immediately re-enters `Decimal` and quantizes to 2 dp; volatility
uses `Decimal.sqrt()`. Rejected: pure-Decimal CAGR via series expansion (complexity, no gain at
2 dp). Reversal: rename routes in one router file; swap CAGR implementation behind the same
signature.
