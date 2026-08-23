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

## SC2.1 — Make catalog state a deterministic stored-snapshot view · ⚠ UNREVIEWED

**Context.** The catalog needs shareable filters and age-sensitive return labels, but SC2 has no
popularity model and a stale EOD snapshot must not silently change its meaning as wall-clock time
passes. Stored FEE baskets also exist before the subscription gate is allowed to open.

**Choice.** The API accepts repeated `category` plus volatility, maximum minimum amount,
rebalance frequency, constituent type, include-new, access, and sort fields. Category values are
trimmed and de-duplicated into one canonical URL state. “New” means the reference product's exact
12 calendar months measured against the global latest stored metric date; headline windows use
calendar anniversaries against each metric's date. Only versions effective at that snapshot are
visible. Until SC9, `popular` deliberately shares the stable launched-descending/id ordering with
`newest`. While subscriptions are disabled, every published card projects as FREE and an explicit
FEE-only query returns no rows.

**Rejected alternatives.** `CURRENT_DATE` makes an unchanged snapshot produce different results
tomorrow; 365/30-day approximations are wrong at leap and month-end boundaries; a random or
returns-based stand-in would mislabel itself as popularity; exposing stored FEE state would leak a
dark Track-B surface.

**Reversal path.** SC9 replaces only the `popular` ordering with a persisted computable ranking.
An explicit catalog-snapshot date can replace the global latest-metric boundary without changing
the URL contract. SC10 may expose FEE access only behind its default-off entitlement flag and
tests.

## SC2.2 — Keep performance exact, point-in-time, and explicit about unavailable history · ⚠ UNREVIEWED

**Context.** Card numbers combine unlike units and incomplete market histories: return/CAGR
fields are percentages, volatility is a fractional annual rate, and version changes must not leak
future constituents backwards. Calendar cutoffs frequently land on non-trading days.

**Choice.** Pure core functions accept and return `Decimal`, use raw/LTP prices only for
affordability, adjusted closes for performance, sample volatility (`ddof=1`) annualized by
`sqrt(252)`, and the version effective on each return date. Trailing windows use the first trading
observation on or after the calendar cutoff and become unavailable when it is more than seven
calendar days late. At 12+ published baskets, equal-sized volatility terciles break exact ties by
stable basket id; below 12, the pack's fixed thresholds apply. Storage rounding is explicit
half-up at the documented scale.

**Rejected alternatives.** Binary floats or implicit database rounding lose money/weight digits;
renormalizing around missing prices hides survivorship gaps; using the latest version over all
history introduces look-ahead; population volatility changes the documented estimate; assigning
every tied estimate one label can leave terciles unusably unbalanced.

**Reversal path.** Thresholds, the seven-day completeness tolerance, and tie policy are isolated
constants/pure functions. Change them only with reviewed methodology, replacement golden tests,
and a full recomputation of `cb_metrics`; persisted source prices and immutable versions remain
unchanged.

## SC2.3 — Project one bounded, auditable MomentumScan genesis · ⚠ UNREVIEWED

**Context.** `MomentumScan` produces a ranked engine artifact, while a curated basket needs an
immutable product identity and exact four-decimal weights. The projection boundary must not
silently re-rank a fixture or rewrite history when the same seed is run twice.

**Choice.** The caller supplies an already selected NSE-cash top-N scan (maximum 50 names) with a
positive data version. SC2 resolves active NSE-EQ instruments set-wise, records a schema-valid
private Screen/ScreenRun owned by the configured sole user (never a public/example screen), and
pins the scan id and data version in immutable genesis notes. It creates
`baskfy-momentum-scan` once under `baskfy-engine` and allocates
10,000 four-decimal units equally using largest remainder with alphabetical tie-breaking. An
identical rerun changes no row or timestamp; an identity/content conflict fails before an
immutable version is touched.

**Rejected alternatives.** Re-running ranking inside persistence duplicates strategy truth;
factor-proportional weights invent a product rule not in the pack; unconstrained frames create an
unbounded seed path; upserting genesis destroys the audit trail.

**Reversal path.** SC3's publisher creates a later version from a later scan identity. A reviewed
weighting or constituent-cap change becomes a new version/policy, never a genesis rewrite. A
future native MomentumScan-run table may replace the private Screen adapter after its provenance
fields are migrated and cross-checked.

## SC2.4 — Materialize catalog metrics atomically after publish with hard resource bounds · ⚠ UNREVIEWED

**Context.** A fixed Beat time can race a legitimately late nightly publish, and loading every
ever-held instrument's full history grows quadratically under version churn. A failed basket must
not leave an apparently complete partial catalog date.

**Choice.** Successful nightly publish causally enqueues `baskfy.compute.curated_metrics` on the
compute queue for the exact date; the 20:20 weekday Beat entry is an idempotent recovery trigger.
The job verifies an NSE trading day and published pipeline run, interval-prunes adjusted/raw price
reads, shares the set-based price store across baskets, validates every basket before the first
metrics mutation, and treats identical `(basket,date)` content—including `computed_at`—as a true
no-op. It refuses more than 500 published baskets, 10,000 merged price intervals, or 250,000 price
rows; the measured 200,000-row worker-shaped probe completed in 0.608 seconds at about 152 MiB
process RSS.

**Rejected alternatives.** Beat-only sequencing can permanently miss a late date; per-basket
queries create N+1 load; a one-million Python-row allowance approaches unsafe workstation memory;
writing each basket as it completes makes a coverage failure look partly green; live quotes in an
EOD job would make reruns nondeterministic.

**Reversal path.** SC12 may raise a bound only after its load proof, or replace the Python store
with a columnar/streaming implementation and equivalent exactness tests. Queue timing may move,
but causal post-publish dispatch and idempotent recovery must remain.

## SC2.5 — Derive watchlist movement from stored NAV without a tenant input · ⚠ UNREVIEWED

**Context.** Watch rows need a stable baseline, daily change, and movement since watch, while the
run forbids request-selectable ownership and must not label a multi-session gap as one day's move.

**Choice.** Every route resolves the configured authenticated sole operator internally. PUT stores
the latest chain-linked base-100 NAV at four decimals and is conflict-idempotent; DELETE followed
by PUT uses a wall-clock database timestamp and fresh baseline. Movement is
`nav_today/nav_at_watch - 1`. Daily change uses the preceding stored metric only when the NSE
calendar proves the snapshots are consecutive sessions; otherwise it is null. Reads are set-based
and bounded, with explicit total/truncation state rather than pretending a capped list is complete.

**Rejected alternatives.** A body/query `user_id` is dormant multi-tenancy; recomputing the old
baseline from mutable inputs breaks watch-date meaning; treating the second-latest row as “daily”
across a data gap is misleading; `now()` inside a long PostgreSQL transaction can fail to give a
rewatch a fresh event time.

**Reversal path.** A future D3-approved tenancy model replaces the sole-user dependency across all
routes at once. Higher-frequency NAV can add a separately named intraday field; it must not mutate
the stored EOD baseline or redefine the existing daily value.
