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
