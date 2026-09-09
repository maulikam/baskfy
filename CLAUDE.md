# Baskfy — root working agreement

This folder is the umbrella for **Baskfy**: the merge of `decile-blueprint` (momentum
screener/data plant, never run on real data) and `kite-momentum-rebalancer` (live momentum
execution desk) into one product. The full record is in `docs/01–08`; the execution script for
agents is `MERGE-PROMPTS.md`. **Read order for any agent session: this file → `docs/README.md`
→ `MERGE-PROMPTS.md` (and the docs it cites as you reach them).**

**Absorbed at M3.** This file now governs: it carries the desk's seven non-negotiables and the
screener's nine house rules, and a contributor who reads only this file cannot break either
product. The two sub-agreements remain authoritative for **their own trees' internals** — where
each module lives, which file to read before touching scoring, the open-items lists that are the
honest record of what has never run. Nothing here overrides their non-negotiables; it inherits
them, and where the root and a sub-file disagree on a rule, the root is wrong and should be fixed.

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

D3 regulatory posture: **written 23 Aug 2026 as B** in `docs/DECISIONS-MERGE.md` §D3
(⚠ UNREVIEWED). RA/empanelment filings remain counsel C1–C3. **Paid** multi-tenant launch
still waits on C3; P4.1/P4.3 engineering started 24 Aug 2026. D7 pricing amounts, D10
market-data display licensing remain human-track. Track B flags stay false. The web app
never gains an execute route. `MERGE-PROMPTS.md` still documents the Phase-3 stop as
history; D3 is no longer the engineering blocker.

## The two laws (from docs/04 §2 — enforced by tests, both trees)

1. **`packages/core` touches nothing.** DataFrames in, DataFrames out. No database, no network,
   no disk, no clock. I/O lives in `services/` or `packages/providers`.
2. **`packages/execution` is the only path to an order.** Guards → risk → rate limit → journal →
   broker. Nothing calls `kc.place_order` directly. Guards refuse untouchable instruments before
   any network call. (Multi-tenant clause, active as of P4.3: every order carries `user_id` +
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
   the lowest layer. **Caveat carried from the desk's own wording, still true:** GTT stops go
   through `kite_client.place_gtt_stop`, which carries its own guard — the gateway has no GTT
   method yet. M16 owns closing that gap; until it does, "everything goes through the gateway"
   has one documented exception.
7. Filter-rejected stocks are never bought; `EXCLUDED_SYMBOLS` instruments are untouchable.

## When the code and a doc disagree, the DECISION wins — not the doc (9 Sep 2026)

**This cost two days and a wrong live trading gate. Read it before "fixing" a setting that
disagrees with a document.**

The swing market gate reads **NIFTY MidSmallcap 400** (`nifty-mid-small-400`), fallback NIFTY 500.
Maulik set it in SW17 (`67df9b4`, 3 Sep 2026): *"the book trades mid- and small-caps, so the tape
it asks about is nifty-mid-small-400."* The book screens mid- and small-cap breakouts; a
large-cap-weighted index answers a question about a market it does not trade.

`docs/swing/04` §8.2 and STANDING-ANSWERS A12 still said NIFTY 500, because SW17 changed the code
and not the prose. On 9 Sep an agent (this one) read the doc, concluded the code was buggy,
"fixed" it back to NIFTY 500 — **and wrote a test pinning it "against the document" so it could
not drift again.** That is a decision reverted, and then locked, by an agent who never asked why
the two disagreed.

**The rule.** A doc describes intent at the time it was written. A setting someone deliberately
changed, with a commit message saying why, is a *later* fact. When they conflict:

1. `git log -S "<the value>" -- <the file>` — find when and why it changed. A commit message
   naming Maulik and giving a reason is a decision, not a bug.
2. If it is a decision: **fix the doc**, and say in it that the doc was the stale half.
3. If there is no such commit, it may genuinely be a bug — then say so and change it.
4. **Never pin a value to a document.** Pin it to the decision, and cite the commit.

It changed the verdict, not just a number: on 3 Sep the MidSmall 400 read GREEN (10-day 21,592.34
over 20-day 21,581.78) where NIFTY 500 read RED, and the gate decides whether the book may enter
at all. With auto-execute armed, a wrong gate is a wrong trade or a missed one.

## Which date the product shows, and why it is not today (settled 9 Sep 2026)

**Read this before "fixing" the data date. It has been asked three times in one week and twice
led to a change that had to be reverted.**

The product serves **two different clocks** and they are both correct:

| Surface | Clock | Why |
|---|---|---|
| Baskets, factors, market health, the screener, the freshness pill's `as_of` | The **last completed trading session** | A daily bar is a *closed* day. House rules 5 and 7: point-in-time, idempotent. There is no "today" bar until today ends |
| Portfolio marks and holdings, the swing book's setups and triggers | **Live, from Kite quotes** | These are prices, not bars, and a quote is available whenever the market is |

So on a weekday at 13:15 the pill correctly reads **yesterday**, while the portfolio and the
swing book are showing this minute. That is not staleness; it is the only honest reading of a
half-finished day. **Do not make the published series claim today while today is still running.**

### What was tried and reverted, so nobody tries it again

* **M88 (8 Sep 2026)** moved the publish cutoff to 15:45 because Kite returned that day's bars
  for RELIANCE, TCS and INFY five minutes after the close. Three of the most liquid symbols in
  India are not evidence about a 4,855-instrument universe: Kite's `historical_data` covers
  ~3,000 of them and the rest arrive only in NSE's bhavcopy. The early run wrote a **partial**
  day, the quality gate refused it three times, and the change was reverted the next morning.
  `baskfy_worker.catch_up.SESSION_DATA_READY_IST` carries the numbers.

### What IS legitimate to improve

* Making a *label* say what it means — the freshness pill appends "· market open" during a
  session and its tooltip names which surfaces are live (9 Sep 2026).
* Getting the published day out **sooner after the close**. M90 did this properly: the bhavcopy
  leads, so the chain takes ~14 minutes instead of 73. Earlier is good; *earlier than the day's
  end* is not.
* Widening what counts as live where a quote genuinely answers the question — M91 lets a login
  at any hour after 09:15 scan today, because after the close a quote *is* the day's final
  price. Before 09:15 it is yesterday's, and stamping that as today would be a lie.

## The screener's house rules (they govern the data plant, and the root lacked them)

Verbatim from `decile-blueprint/CLAUDE.md`, with module paths updated for M2's rename. A
contributor who reads only this file must not be able to break the pipeline either, which is
M3's acceptance criterion.

1. Read `decile-blueprint/docs/02-tech-stack-adr.md` before proposing any dependency. Nothing
   outside the locked stack without saying why first.
2. Tests assert the **spec**, never current behaviour. If a test would only lock in what the code
   happens to do today, it is not worth writing.
3. No `# type: ignore`, no `any`, no silently swallowed exceptions.
   `packages/core/tests/test_no_escape_hatches.py` enforces this by scanning the source.
4. Every module ends with: tests passing, `make lint` clean, and a `docs/` update if behaviour
   diverged from the spec.
5. **No look-ahead, ever.** Anything referencing a past date uses point-in-time index membership
   and point-in-time factor rows. Asserted by tests, not by discipline.
   ⚠️ **This rule is currently violated and the violation is known:** `apply_adjustments` applies
   corporate actions with a *future* ex-date (`decile-blueprint/docs/DECISIONS.md` §21.9). It is
   M10's to fix. Do not add a second one.
6. **Adjusted by default.** `close` is adjusted; `close_raw` is the exchange print. Factors read
   `close`; display uses `close_raw` where the user expects a real price.
   ⚠️ Also open: adjusting `open`/`high`/`low` is destructive because no raw counterpart is
   stored (§21.10).
7. **Idempotent ingestion and seeding.** Re-running any day's job produces identical rows.
8. **Round at write time.** Storage precision is the contract, so the API, the UI and the CSV
   export can never disagree.
9. Money and prices are `numeric`, never `float`. Disclaimers are components, not footers.

## The namespace rule (M2)

`decile` is this product's **domain vocabulary** as well as its former brand — a decile is a
statistical bucket. `tools/check-namespace.sh` is the check: no namespace token survives in code,
while `decile_1`…`decile_6` (the D1–D6 values of `apply_filters_on`, a public API contract),
`DECILE_RANK_KEY` and `decile_bucket` are kept deliberately. Never widen the pattern to silence a
hit; add vocabulary to the script's `ALLOWED` list with a reason, and record it in
`docs/DECISIONS-MERGE.md`. (`DECISIONS-MERGE.md` M2.1.)

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
- **Never weaken a test to make a module pass.** If an acceptance criterion looks wrong, settle
  it under the Autonomy charter below: decide by the precedence order, record it in
  `docs/DECISIONS-MERGE.md`, continue.
- Network calls go through the existing rate-limited providers only (Kite ~3 req/s historical;
  NSE with its cookie/header discipline). No scraping around them.

## Autonomy charter (Maulik's standing instruction, 22 Aug 2026)

Run long. Questions to Maulik are the exception, not the rhythm. When a stop-and-ask would have
happened, **decide, record, continue**:

1. Choose the option you would have recommended to him.
2. Write it into `docs/DECISIONS-MERGE.md` as that module's entry, tagged **`⚠ UNREVIEWED`** —
   the context, the choice taken, the rejected alternatives and why, and how to reverse it. He
   reviews the file asynchronously; prefer the choice that stays cheap to reverse.
3. Note it on the status page and keep going — module after module, without pausing to
   summarize between them. If the session's context runs long, write enough state into
   `docs/00-merge-status.md` for a fresh session to resume losslessly, then continue.

**Precedence when rules conflict** (highest wins):

1. The safety rails above — no discretion.
2. The two laws, the seven non-negotiables, the nine house rules.
3. The module's stated **Goal**.
4. Design intent in `docs/03`–`06`.
5. The literal wording of an acceptance criterion — **lowest**: a criterion is a proxy for its
   Goal, and when it over-reaches (M2 proved it), scope the criterion, record why, continue.

**Ties break toward:** the reversible option · preserving public contracts and saved data · the
stricter security boundary when nothing in force changes (M4 proved it) · names that keep their
meaning · what the two codebases' own culture would do.

**The only things that still stop work — and none stops the whole run while independent modules
remain:**

- Something only Maulik's hands can supply (credentials, a login/2FA, money, anything outside
  this repo) → append it to **`NEEDS-MAULIK.md`** at the root (what is needed, why, what it
  blocks, what was done meanwhile) and keep working on everything not dependent on it.
- Destroying or risking unrebuildable data with no verified backup path — never autonomous.
- Placing a live order — never, full stop.
- Paid multi-tenant launch / Track B flag flips / web execute — C3, D7 amounts, and
  non-negotiable #1. P4.1 schema and P4.3 gateway isolation are in; P4.2 two-token OAuth,
  P4.10 RLS, and P4.11 load tests are not.
- A red-gate parity delta that survives exhausted investigation → finish every module that does
  not depend on the failed numbers, then end the run with a full written report.

## Conventions

- **One commit per module**, message `M<N>: green — <one line of what changed>`, matching the
  repos' existing habit of commits written in complete sentences.
- `docs/00-merge-status.md` is the live status page — updated at the end of every module, loud
  about what is NOT done (both repos' culture of honest open-items lists continues here).
- Judgement calls under ambiguity go in `docs/DECISIONS-MERGE.md`, numbered by module — the same
  convention as decile's `docs/DECISIONS.md`.
- Deployment target is AWS Mumbai per `docs/08-aws-architecture.md`; the Phase-A box comes
  *after* the merge modules and is operator-led.
