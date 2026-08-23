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

## SC0.1 — Isolate SC from the concurrently active M41 worktree · ⚠ UNREVIEWED

**Context.** SC0 began on `main` while an uncommitted M41 broker/backtest implementation was
present. Its first baseline run failed five tests. The M41 files were preserved in a named stash,
but another live Claude process continued writing the same M41 task into the source worktree while
the SC database gate was running. That made source and test results nondeterministic, and the
broker-connect surface also overlaps SC's stricter Track-C boundary.

**Choice.** Preserve the first M41 snapshot as `stash@{0}` (object
`dda9c25c0f15431905a6a3c94dcc5806e6c31ef7`), leave the subsequently active M41 worktree alone,
and run SC on a new `codex/sc-run` branch/worktree forked from the last committed green SHA
`aed71ed`. Give SC its own disposable test database, `baskfy_sc_test`. This makes every SC module
and commit reproducible without deleting, absorbing, or racing the other work.

**Rejected alternatives.** Folding M41 into SC0 would violate the one-module/one-purpose history
and risk admitting Track-C broker OAuth machinery. Resetting or repeatedly stashing the source
worktree would destroy or interrupt somebody else's active work. Continuing in place would make a
green gate meaningless because test files changed during collection.

**Reversal path.** After M41 is committed or intentionally abandoned, merge or cherry-pick the
SC commits from `codex/sc-run` into the chosen integration branch. Then remove the worktree with
`git worktree remove /Users/maulikdave/Documents/projects/baskfy-sc-run`; the branch and M41 stash
remain until explicitly deleted. Any genuine non-conflicting M41 fixes can instead be merged into
`codex/sc-run` and the full gates rerun.

## SC1.1 — Keep curated baskets as projections over existing market and execution truth · ⚠ UNREVIEWED

**Context.** The live schema already has the screener's instrument/price history and current
`basket_snapshot`, while the desk owns broker holdings and the only execution journal. SC1 needed
a durable smallcase-shaped catalog without creating a second source of truth for any of those
facts.

**Choice.** `cb_basket` is the curated product identity and its append-only versions are durable
model-portfolio projections. They reference the existing `instrument` and optional `screen_run`
rows. `basket_snapshot` remains the upstream current strategy cache, and `desk_plan_id` remains an
opaque identifier: `cb_order_batch` records product intent but never records execution. No new
instrument, price, broker-holding, or execution-journal table was created.

**Rejected alternatives.** Rebranding `basket_snapshot` as the product catalog loses manager,
version, collection, and point-in-time semantics. Copying screener or desk facts into `cb_` tables
creates reconciliation races and an order-shaped bypass around the execution gateway.

**Reversal path.** A later migration may retire `basket_snapshot` after every consumer reads an
equivalent `cb_basket_version` projection, or replace the opaque desk identifier with a stable
read-only integration contract. The desk must remain execution truth unless a separately approved
D3 decision explicitly changes that law.

## SC1.2 — Resolve one configured public user identity to the existing account row · ⚠ UNREVIEWED

**Context.** The scope requires an explicit user and broker account on order-shaped data while
forbidding request-selectable tenants. `app_user` uses an internal numeric primary key plus a
stable public id, and the operator may not yet have login credentials in a clean database.

**Choice.** `BASKFY_SOLE_USER_ID` means `app_user.public_id`, defaults to
`baskfy-sole-user`, and is resolved internally to the numeric foreign key. The distinct
`BASKFY_SOLE_BROKER_ACCOUNT_ID` defaults to `operator-kite`. SC1 seeds a single no-password user
with a deterministic non-routable `.invalid` email, preserves any later operator-owned profile or
credential values, and seeds only the first-party `baskfy-engine` and `maulik` manager profiles.
Neither identity can be selected from a request.

**Rejected alternatives.** Configuring a database primary key makes clean rebuilds brittle;
using an email as identity risks making a routable address part of an ownership contract; and
accepting user/broker ids from callers would be dormant multi-tenancy in name only.

**Reversal path.** Change the environment-backed public ids and migrate owned rows in one audited
transaction. Multi-user resolution requires a future D3-approved tenant boundary and must replace,
not silently widen, the sole-identity resolver.

## SC1.3 — Enforce unambiguous, append-only basket version intervals · ⚠ UNREVIEWED

**Context.** Two versions effective on the same date make point-in-time returns ambiguous, while
service-only immutability can be bypassed by maintenance scripts or future code. Constituents must
also survive exactly at the database's four-decimal weight precision.

**Choice.** Each basket has unique `(basket_id, version_no)` and `(basket_id, effective_date)`
pairs; the repository locks the owning basket, requires a strictly later effective date, derives
labels and add/remove counts, rejects weights that cannot be stored exactly at four decimals, and
requires an exact `1.0000` total before writing. PostgreSQL triggers reject update or delete of a
version or constituent; corrections are new versions.

**Rejected alternatives.** Last-write-wins same-day revisions destroy historical meaning;
epsilon/float comparisons admit allocations that do not equal their stored values; and relying on
one repository alone leaves immutable facts vulnerable to another write path.

**Reversal path.** A future migration can introduce an explicit intraday sequence or supersession
model, backfill it, and then replace the date uniqueness/trigger rules. Dropping those protections
without a replacement is not a supported reversal.
