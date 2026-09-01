# Plan: Tree 4 — four product-integrity items

Mode: **solo, tree-4 structure.** The skill's tree-4 default is to fan out into subagents. Not
here, for two reasons: this session is barred from spawning agents unless asked, and this repo
already has four-plus concurrent sessions — two write collisions in one day, one of which cost a
whole leaf. Adding parallel writers to a tree that edits shared route files would make it worse,
not faster. Each leaf still gets its own gates file and its own full pass.

Root gates: `gates/tree4-root.md`. Leaf gates: `gates/tree4-{a,b,c,d}-*.md`.

---

## The brief, measured before planning

Three of the four items were checked against the tree before any work started. **Two do not
reproduce as stated**, and saying so is part of the job — inventing 31 items of work to match a
number would be the exact failure this discipline exists to prevent.

| # | Brief says | Measured | Verdict |
|---|---|---|---|
| A | `/investments/[id]/*` and `/me/investments/[id]/*` are both live | 9 route files; both trees are real pages | **Confirmed** |
| B | "Only 9 components have an empty state" | 59 files handle an empty collection; 47 use an explicit `length === 0` branch | **Not reproducible** — the gap is narrower and different |
| C | `/` shows "The sample screen could not be loaded" | Reproduced: `curl /` returns 200 with that string in the HTML | **Confirmed** |
| D | "31 placeholder markers (TODO / not-implemented) in src" | `apps/web/src`: 9 `not-implemented`, **0** TODO/FIXME. Repo-wide outside tests: **1** match total | **Not reproducible** |

**On D specifically.** The 9 `not-implemented` hits are not placeholder UI. They are a value in a
discriminated union — `{ status: "not-implemented" }` returned when a search endpoint answers 404
— and both modules carry a comment saying it is "kept rather than deleted" and "is not
decoration". The single repo-wide `NotImplementedError` is a protocol guard in
`baskfy_worker.engine` whose message directs the caller to `run` instead. Both are finished code.
The count of 31 most likely came from a pattern that also matched `placeholder=` input attributes
(33 hits) and the word "stub" in prose comments (47 hits).

## Decision taken on C, which the brief explicitly left to me

Measured: `SAMPLE_COLUMNS` is `close_raw, marketcap_cr, ret_12m, sharpe_12m, vol_12m`. The free
set is `close_raw, ret_12m, vol_12m`. **The two gated columns are `marketcap_cr` and
`sharpe_12m`.**

**Decision: drop the gated columns. Do not move the paywall.** Three reasons, in order of weight:

1. Moving a paywall is a commercial decision (D7-adjacent, and `NEEDS-MAULIK` §18 says so). It is
   not reversible by a code change alone and it is not mine.
2. `marketcap_cr` is NULL for every instrument today — `fundamental_daily` is empty
   (`NEEDS-MAULIK` §15). The column the paywall is protecting renders as em dashes. Dropping it
   costs the reader nothing that exists.
3. The landing page is the product's **only** public acquisition surface left, now that
   `/instruments` sits behind the auth gate. A centrepiece reading "could not be loaded" is worse
   than a narrower table.

Reversal is one line: put the two names back in `SAMPLE_COLUMNS` once the entitlement changes.

## The tree

```
Tree 4 — product integrity
├── A  one investments journey        gates/tree4-a-routes.md
├── B  empty states that are missing  gates/tree4-b-empty.md
├── C  the public landing page        gates/tree4-c-landing.md
└── D  placeholder markers            gates/tree4-d-markers.md
```

## Contract — shared surfaces, decided before touching anything

- **A owns** `src/app/(app)/investments/**` and the redirect registry entry for it.
- **B owns** only components it adds an empty state to, and may not edit a route file A owns.
- **C owns** `src/lib/marketing/sample-screen.ts` and nothing else.
- **D owns** no source file; it produces a measurement and, if warranted, filed items.
- No leaf edits `src/lib/nav.ts` except A, and only to keep the canonical entry.
- Canonical route is **`/me/investments/*`**: Tree 6 established `/market`, `/baskets`, `/build`,
  `/me` as the four canonical sections with legacy redirects, so `/investments/*` is the legacy
  spelling by that decision, not by a new one taken here.

## Status log

- 2026-08-25 — Plan written after measuring all four items. B and D do not reproduce as briefed;
  recorded above rather than silently rescoped.
