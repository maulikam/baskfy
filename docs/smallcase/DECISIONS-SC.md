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
