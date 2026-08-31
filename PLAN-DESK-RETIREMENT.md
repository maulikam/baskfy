# Plan: retire `desk.modelbasket.in` — Baskfy becomes the only system

Depth: tree 5   Mode: orchestrated
Started: 2026-08-31

Budget note: a competent single pass would not fit this in one sitting. Fourteen leaves,
each a real unit of work. Literal tree depth is 3 (root → 5 branches → leaves); the *scale*
is tree-5 (14 leaves, past what one context holds). Going a layer deeper would produce
sub-ten-minute leaves, which `references/method.md` rule 2 forbids. Depth follows the joints.

## Scope boundary — read this first

The user's six-item list is the input. **Item 1 (an execute route in Baskfy) is NOT
implemented by this plan.** Root `CLAUDE.md` lists "web execute" under the things that stop
work, alongside non-negotiable #1 ("Never auto-execute") and counsel item C3. The user pasted
back the sentence "this is a rule you must lift explicitly" as part of the task; that is the
requirement restated, not the lifting of it. Leaf 1.5.2 therefore **writes the decision for
Maulik to sign** and stops. Everything else in the list is buildable without touching that rule,
and is built here in full.

Nothing in this plan places a live order. Nothing in this plan is a Track B flag flip.

## Contract

Fixed before fan-out. Binding on every leaf.

### Hard safety rails (inherited, not negotiable by any leaf)

- `DRY_RUN=true` in every environment a leaf creates. **No leaf places a live order, ever.**
- `kite-momentum-rebalancer/data/portfolio.db` and the desk's live backend are **read-only**
  until leaf 1.3.2 has a verified backup plus a dated copy outside the repo. Migrations run
  against a copy first; the original is archived forever, never deleted.
- **No leaf modifies the running desk deployment** at `65.0.226.77`. The desk must be able to
  rebalance on Friday 4 Sep throughout this work. Leaf 1.1.1 owns the login question and does
  diagnosis before any change; a change there needs the driver's sign-off, not a leaf's.
- Never print, log or commit a secret. `BASKFY_KITE_API_SECRET`, the Kite api_key and the
  token encryption key are all live credentials. Hash or redact; never echo a settings object.
  (Two were already leaked into a transcript this session and need rotating — see 1.1.3.)
- Never weaken a test to make a leaf pass.

### Source material

- `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` — **READ ONLY**. This is the
  live desk's own repo and it has **diverged from the in-repo subtree: 58 source files differ**,
  including commits the subtree lacks ("Give every market order the protection band Kite
  requires", "Measure the daily loss cap against NAV, not against invested"). Establishing which
  copy is authoritative is leaf 1.1.2's job and every other leaf depends on its answer.
- `decile-blueprint/packages/execution/` — Baskfy's ported gateway. Already has guards, risk,
  ratelimit, tenancy, brokers, adapters. **Has no GTT method** (verified).
- Port, do not rewrite. Where desk code is correct, move it and keep its behaviour; where it is
  moved, say so in the commit and cite the source file.

### Data ownership — no two leaves write the same file

| Leaf | Owns (write) |
|---|---|
| 1.1.1 | `docs/DESK-LOGIN-DECISION.md`, box `.env.staging` (via driver) |
| 1.1.2 | `docs/DESK-SOURCE-RECONCILIATION.md` |
| 1.1.3 | `NEEDS-MAULIK.md` |
| 1.2.1 | `decile-blueprint/packages/execution/src/baskfy_execution/gtt.py`, `gateway.py`, its tests |
| 1.2.2 | `docs/GATEWAY-PARITY.md` |
| 1.2.3 | `decile-blueprint/services/worker/src/baskfy_worker/tasks/desk.py`, drill script under `tools/` |
| 1.3.1 | `docs/DESK-DATA-INVENTORY.md` |
| 1.3.2 | `tools/migrate-desk/` (new dir) |
| 1.3.3 | `gates/desk-retire-1.3.3-verify.sh` |
| 1.4.1 | `docs/PARITY-M11.md` |
| 1.4.2 | `docs/PARITY-M13.md` |
| 1.4.3 | `docs/PARITY-M14.md` |
| 1.5.1 | `docs/DESK-UI-PORT.md` |
| 1.5.2 | `docs/DECISION-EXECUTE-ROUTE.md` |

Shared, driver-owned only: `docs/DECISIONS-MERGE.md`, `docs/00-merge-status.md`,
`PLAN-DESK-RETIREMENT.md`, anything under `.env`.

### Conventions

- Python: the nine house rules. No `# type: ignore`, no `any`, no swallowed exceptions
  (`packages/core/tests/test_no_escape_hatches.py` scans for the last one). `make lint` clean.
- Tests assert the spec, never current behaviour.
- Money and prices are `numeric`, never `float`.
- One commit per leaf: `desk-retire <leaf id>: <one line>`.
- A leaf that finds a second defect writes it into its own gates file as a new gate rather
  than fixing it silently or leaving it.

## Tree

- 1 Retire the desk
  - 1.1 Session continuity — nothing else works without a Kite login .... `gates/desk-retire-1.1.md`
    - 1.1.1 Restore a working login path (URGENT, before Tue 09:00) ..... `gates/desk-retire-1.1.1.md`
    - 1.1.2 Which desk source is authoritative (58-file divergence) ..... `gates/desk-retire-1.1.2.md`
    - 1.1.3 Credential inventory + rotation list ........................ `gates/desk-retire-1.1.3.md`
  - 1.2 Execution capability — the dangerous gap ....................... `gates/desk-retire-1.2.md`
    - 1.2.1 GTT into the gateway (non-negotiable #4) .................... `gates/desk-retire-1.2.1.md`
    - 1.2.2 Gateway parity audit against the live desk .................. `gates/desk-retire-1.2.2.md`
    - 1.2.3 DRY_RUN Friday drill through Baskfy's gateway ............... `gates/desk-retire-1.2.3.md`
  - 1.3 Data custody — the unrebuildable record (D8) ................... `gates/desk-retire-1.3.md`
    - 1.3.1 Inventory the desk's live backend ........................... `gates/desk-retire-1.3.1.md`
    - 1.3.2 Migration with row-count + checksum assertions .............. `gates/desk-retire-1.3.2.md`
    - 1.3.3 Verify + prove idempotent + forever archive ................. `gates/desk-retire-1.3.3.md`
  - 1.4 Numbers parity — cannot execute off numbers that disagree ...... `gates/desk-retire-1.4.md`
    - 1.4.1 M11: 6,934/9,166 cells fail on window length ................ `gates/desk-retire-1.4.1.md`
    - 1.4.2 M13: generated scan 223 symbols vs upload 239 ............... `gates/desk-retire-1.4.2.md`
    - 1.4.3 M14: shadow harness, 4 order deltas ........................ `gates/desk-retire-1.4.3.md`
  - 1.5 Surface and governance ......................................... `gates/desk-retire-1.5.md`
    - 1.5.1 Live-broker UI pages: inventory and port plan ............... `gates/desk-retire-1.5.1.md`
    - 1.5.2 Decision doc for the execute route — WRITE, DO NOT BUILD .... `gates/desk-retire-1.5.2.md`

## Sequencing

1.1.1 first and alone — it is time-critical (no Kite session for either system tomorrow) and
its answer changes 1.2.3 and 1.5.1. Then 1.1.2, whose answer every port leaf depends on.
After those two, 1.2.x / 1.3.x / 1.4.x have disjoint file ownership and may run concurrently.
1.5.x last. Branch gates worked by the driver after each branch's children verify.

## Status log

Append-only.

- 2026-08-31 plan written, contract fixed, item 1 scoped OUT of implementation with reason
- 2026-08-31 20 gate files written; 95 unchecked boxes; gate-check verified working
- 2026-08-31 dispatched 1.1.1 (login path, URGENT), 1.1.2 (source reconciliation), 1.3.1 (data inventory) — disjoint file ownership, running concurrently
- 2026-08-31 dispatched 1.1.3 (credentials), 1.4.1 (M11), 1.4.2 (M13), 1.4.3 (M14), 1.5.1 (UI inventory) — 9 leaves now in flight
- 2026-08-31 held: 1.2.1/1.2.2 block on 1.1.2 (which desk source is authoritative); 1.2.3 blocks on 1.2.1; 1.3.2/1.3.3 block on 1.3.1
- 2026-08-31 VERIFIED 1.1.2 (5/5) — fork point tree 2913aae confirmed both sides; market_protection=0 repo-wide confirmed; risk.py on_pnl has no NAV basis confirmed; G5 evidence completed by driver
- 2026-08-31 VERIFIED 1.3.1 (5/5) — record is SQLite on the box, not Postgres; THREE forked copies
- 2026-08-31 VERIFIED 1.5.2 (6/6) — routers/kite.py 0 POST confirmed; gateway.py 150 lines 0 GTT confirmed; M11 parity skip confirmed (lines 700,743, BASKFY_PARITY_BARS unset)
- 2026-08-31 VERIFIED 1.1.3 (4/4), 1.5.1 (4/4)
- 2026-08-31 dispatched 1.2.1 (GTT port) and 1.3.2 (migrator) with 1.1.2/1.3.1 findings in brief
- 2026-08-31 ledger 35/95
- 2026-08-31 VERIFIED 1.1.1 (5/5) — recommendation: Option A, desk keeps the redirect. Driver additionally verified the DRY_RUN token-poisoning path (routers/brokers.py:306-312 stores a sim_ stub unconditionally) and that DECISIONS-MERGE.md has no M57 (jumps M47->M58)
- 2026-08-31 NETWORK OUTAGE killed 4 leaves mid-flight (1.2.1, 1.3.2, 1.4.1, 1.4.2). 1.4.1 had left an UNVERIFIED edit in packages/core/.../windows.py (bisect_left->bisect_right). Driver measured it: improves M11 cells per the leaf (6396->4514) but breaks test_momentum_scan.py::test_the_bytes_are_stable. REVERTED from the tree; lead preserved in gates/desk-retire-1.4.1.md + scratchpad patch so the retry re-derives rather than inherits.
- 2026-08-31 re-dispatched 1.2.1, 1.3.2, 1.4.1, 1.4.2 with --timeout workaround for the gate-check arg bug
- 2026-08-31 MAULIK DECISION: keep the Baskfy callback; do NOT revert to desk.modelbasket.in.
  Option B from docs/DESK-LOGIN-DECISION.md is now the chosen path. Consequence accepted and
  stated once: the desk cannot obtain a session until the reverse bridge lands, so Friday 4 Sep
  depends on leaf 1.1.5.
  CONTRACT AMENDED: desk-side changes are now authorised for leaf 1.1.5 only, least-privilege,
  and only after a verified backup. Every other leaf remains forbidden from touching the desk.
  Two leaves added: 1.1.4 (DRY_RUN token poisoning — now on the live path, urgent) and
  1.1.5 (reverse bridge). Tree is now 16 leaves.
- 2026-08-31 dispatched 1.1.4 (token poisoning), 1.1.5 (reverse bridge), 1.2.2 (gateway parity audit)
- 2026-08-31 NOT dispatchable yet: 1.2.3 blocks on 1.2.1 (GTT must exist before a drill can exercise it); 1.3.3 blocks on 1.3.2 (nothing to verify until the migrator exists)
- 2026-08-31 CONTRACT ADDENDUM: project skill `momentum-rebalance` surfaced (kite-momentum-rebalancer/.claude/skills/) — authoritative for stop sizing, scan CSV schema, hard eligibility filters and rebalance discipline. It was NOT in the original contract; the plan was written without it.
  Relayed to the three leaves it governs: 1.2.1 (stop formula clamp(ann_vol/sqrt(52)*2.2, 8%, 12%) — assert against SPEC not against current code), 1.4.2 (the six hard filters + rupee/decimal unit check), 1.4.3 (hysteresis 5-10pts, turnover <20-30%, runners never added to, cost/tax skip rule, 2026-08-14 sizing change).
  Driver note: the spec rejects series "BE" and predates SME — it says nothing about SM/ST/SZ added today in M59/M61. Recorded as a genuine spec gap, not a bug.
- 2026-08-31 VERIFIED 1.1.5 (7/7) — live 302 confirmed: bare request_token at Baskfy callback -> desk.modelbasket.in/callback. Desk momentum-web active, portfolio.db 6295552 unchanged, authorized_keys backed up.
  REVERSAL the driver must relay: BASKFY_KITE_API_SECRET must STAY EMPTY. Setting it lets Baskfy redeem the single-use request_token and starve the desk. Supersedes NEEDS-MAULIK §3 branch A and the driver's own earlier advice to Maulik (given twice).
- 2026-08-31 VERIFIED 1.2.1 (7/7), 1.2.2 (5/5), 1.3.2 (6/6), 1.4.1 (7/7), 1.4.2 (5/5), 1.1.4 (8/8)
- 2026-08-31 DRIVER FIX: packages/execution uncollectable since ef50c09 (OAuthStart undefined) and absent from testpaths. Defined the dataclass, added the path. 132 tests now run for the first time.
- 2026-08-31 dispatched 1.2.3 (drill) and 1.3.3 (independent migration verify) — both now unblocked
- 2026-08-31 VERIFIED 2.1 (8/8) — parity 4,514 -> 3,168 (-29.8%). GTT stops: 132/269 move, all
  upward, median 0.0239%, max 0.1468%, ZERO clamp crossings (census 24 floor / 97 ceiling
  unchanged). Basket: 0 eligibility disagreements, top-12 unchanged, top-15 same names with the
  order now matching the reference exactly (WELCORP 2nd -> 6th, the RSI cliff 1.4.3 found),
  top-20 improved 19/20 -> 20/20. Committed M65.
- 2026-08-31 VERIFIED 2.2 (7/7) — split along law #1: pure predicate in core, store in services,
  client_id minting in execution. _journal now takes client_id keyword-only with no default, all
  20 call sites checked. routers/kite.py still 0 POST. Committed M66.
- 2026-08-31 DRIVER: chased 2.1's cause-E handoff. 2026-02-01 Budget Sunday held 322 bars (301 of
  them SME written earlier tonight, only 21 main-board from Kite) against NSE's full 3,229-row
  bhavcopy and 2,310 on 30 Jan. Kite silently skipped the special session. Backfilled to 2,304.
  Distinct from M62: the calendar was CORRECT; this was a partial ingest.
- 2026-08-31 DRIVER: found docker compose exec through SSM silently no-ops on long commands —
  several backfill runs reported nothing and wrote nothing. Plain `docker exec` works. Worth
  knowing before trusting any long box.sh invocation's silence as success.
- 2026-08-31 13 commits, 7d6b7fb -> df5fb37. Images built for df5fb37; ECR push unconfirmed and
  DEPLOY BLOCKED on an expired AWS SSO token. Box still runs 83d8d68.
