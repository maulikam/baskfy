# Baskfy — root working agreement

This folder is the umbrella for **Baskfy**: the merge of `decile-blueprint` (momentum
screener/data plant, never run on real data) and `kite-momentum-rebalancer` (live momentum
execution desk) into one product. The full record is in `docs/01–08`; the execution script for
agents is `MERGE-PROMPTS.md`. **Read order for any agent session: this file → `docs/README.md`
→ `MERGE-PROMPTS.md` (and the docs it cites as you reach them).**

Until absorbed (module M3), the two sub-agreements remain authoritative for their own trees:
`decile-blueprint/CLAUDE.md` and `kite-momentum-rebalancer/CLAUDE.md`. Nothing in this file
overrides their non-negotiables — it inherits them.

## Decisions taken (agents may proceed on these)

| | Decision |
|---|---|
| D1 | Name **Baskfy**. Domain **baskfy.com** (owned by Maulik, 21 Aug 2026); `.in`/`.co.in` to be added defensively. Namespaces: `baskfy_core`, `baskfy_api`, `baskfy_worker`, `baskfy_providers`, `baskfy_execution`; TS `@baskfy/*`; env prefix `BASKFY_`; database `baskfy`. Decile's product vocabulary (D1 bucket, decile drift, Market Pulse, Replay, hold band) is kept. `desk.modelbasket.in` stays the operator console |
| D2 | One monorepo, histories preserved via `git subtree add` — 91 + 22 commits of reasoning are an asset |
| D4 | Strangle/options lab: **frozen, not deleted** — `frozen/strangle/`, out of every gate; the live box's collectors are ops, not repo |
| D5 | Backfill from **2011-01-01** (resumable; ~an evening at Kite's 3 req/s — see docs/07 §4b) |
| D8 | The desk's SQLite **migrates** with row-count + checksum assertions; the file is archived forever |
| D9 | The public API **stays shut** (source constant + flag), reinforced by docs/07 §4d |

## Human-track decisions (NOT for agents — never build against a guess)

D3 regulatory posture (posture B intended: RA registration + the Kite-Publisher/empanelment
question — with counsel and Zerodha), D7 pricing amounts, D10 market-data display licensing.
**No Phase-4+ (multi-tenant) work happens in this repo until D3 has a written answer in
`docs/DECISIONS-MERGE.md`.** `MERGE-PROMPTS.md` deliberately stops at Phase 3.

## The two laws (from docs/04 §2 — enforced by tests, both trees)

1. **`packages/core` touches nothing.** DataFrames in, DataFrames out. No database, no network,
   no disk, no clock. I/O lives in `services/` or `packages/providers`.
2. **`packages/execution` is the only path to an order.** Guards → risk → rate limit → journal →
   broker. Nothing calls `kc.place_order` directly. Guards refuse untouchable instruments before
   any network call. (Multi-tenant clause, dormant until P4: every order carries `user_id` +
   `broker_account_id`, and the gateway refuses a mismatch.)

## The desk's seven non-negotiables (survive verbatim, forever)

1. **Never auto-execute.** Orders fire only from `POST /execute` with `confirm=true` and the
   `plan_id` issued by `/analyze`; plans expire in 30 minutes. `DRY_RUN=true` must simulate end
   to end.
2. Holdings quantity = `quantity` + `t1_quantity` + `collateral_quantity`.
3. Pledged shares sell directly (Zerodha instant-sale); plan flags them as info only.
4. Every buy gets a GTT stop the same session, vol-scaled 8–12% via `stop_from_vol()`.
5. Product gates: CNC-only; MIS needs `INTRADAY_ENABLED`, NFO/BFO needs `OPTIONS_ENABLED`;
   both default off, enforced inside the gateway.
6. All order flow goes through the gateway (guards → risk → rate-limits → journal);
   `client_id = plan_id:symbol` so a re-posted plan cannot double-send; SGB*/G-sec blocked at
   the lowest layer.
7. Filter-rejected stocks are never bought; `EXCLUDED_SYMBOLS` instruments are untouchable.

## Safety rails for agent work in this repo

- **`DRY_RUN=true` is the default in every environment an agent creates.** An agent never places
  a live order, and never runs execution tests against live credentials.
- **`kite-momentum-rebalancer/data/portfolio.db` is unrebuildable evidence** (trades, fills,
  decisions — the strategy's track record). Before any step that can touch it: a verified backup
  (`python -m scripts.backup` must say `ok`) plus a dated copy outside the repo. Migrations run
  against a copy first; the original SQLite file is archived forever, never deleted.
- **Never print, log, or commit secrets.** `.env` files and `data/` stay untracked; if a command
  would echo a token, don't run it that way.
- **The desk must be able to rebalance on any Friday.** No module ends with the desk's tree
  broken; its test suite is green before an agent stops for the day.
- **`kite-momentum-rebalancer/data/uploads/*.csv` is the regression corpus.** Read-only. It is
  the answer key the whole merge is graded against (docs/README, "the one thing").
- **`frozen/strangle/` (after M6) is untouched** — no refactors, no deletions, no lint fixes.
- **Never weaken a test to make a module pass.** If an acceptance criterion looks wrong, stop,
  write the case in `docs/DECISIONS-MERGE.md`, and ask.
- Network calls go through the existing rate-limited providers only (Kite ~3 req/s historical;
  NSE with its cookie/header discipline). No scraping around them.

## Conventions

- **One commit per module**, message `M<N>: green — <one line of what changed>`, matching the
  repos' existing habit of commits written in complete sentences.
- `docs/00-merge-status.md` is the live status page — updated at the end of every module, loud
  about what is NOT done (both repos' culture of honest open-items lists continues here).
- Judgement calls under ambiguity go in `docs/DECISIONS-MERGE.md`, numbered by module — the same
  convention as decile's `docs/DECISIONS.md`.
- Deployment target is AWS Mumbai per `docs/08-aws-architecture.md`; the Phase-A box comes
  *after* the merge modules and is operator-led.
