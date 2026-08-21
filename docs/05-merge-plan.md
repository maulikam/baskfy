# 05 — The merge plan

Seven phases. Every task states its **acceptance criterion**, because that is the convention both
codebases already run on (Decile's `PROMPTS.md`; the desk's *"print the top-25 delta table in
your response"*).

**Rules for the whole plan**

1. The desk keeps trading throughout. No phase may leave it unable to rebalance on a Friday.
2. No phase ships without the two laws holding (`04` §2).
3. `DRY_RUN=true` is the default in every new environment. Flipping it is a deliberate act.
4. Anything that touches the desk's unrebuildable ~10 MB takes a verified backup first.
5. The strangle/options subsystem is out of scope. Do not migrate, refactor or delete it.

**Sequencing at a glance**

```
P0 Foundation ──► P1 Prove the numbers ──► P2 Cut the CSV cord ──► P3 One codebase
                        ▲                                                │
                        │ the whole plan gates here                      ▼
                                                            ┌─── SEBI GATE (06 D3) ───┐
                                                            ▼                          
                                     P4 Multi-tenant ──► P5 Product surface ──► P6 Launch
```

---

## Phase 0 — Foundation
*Decisions, naming, one repo, no behaviour change. ~1 week.*

| # | Task | Acceptance |
|---|---|---|
| **P0.1** | Answer the nine decisions in `06-decisions-required.md`. D1, D2 and D3 block everything after them. | Each has a written answer committed to `docs/DECISIONS.md` |
| **P0.2** | Create the umbrella git repo at `baskfy/`, preserving **both histories** via `git subtree add` (not a copy — 91 + 22 commits of reasoning are an asset) | `git log --follow` reaches `app/scoring.py`'s original commit and `module 20: green` |
| **P0.3** | Global namespace rename to the chosen name, as **one commit containing nothing else** | `make lint` + both test suites green; zero occurrences of the old prefix outside `docs/` |
| **P0.4** | Merge the two `CLAUDE.md` files into one working agreement. Both sets of non-negotiables survive verbatim; add the tenancy clause to Law 2 | A new contributor can read one file and not break either product |
| **P0.5** | Merge the two `.env.example` files into one prefixed schema; split **user-editable** from **system-only** knobs (`03` §3f) | A test asserts no risk-ceiling constant is reachable from a user-facing settings route |
| **P0.6** | Unify CI: Decile's `ci.yml` + the desk's pytest, one workflow, both suites, both linters | One green check on a PR touching either tree |
| **P0.7** | Freeze the strangle subsystem: move to `frozen/strangle/`, exclude from lint/type/coverage gates, leave the systemd collectors running on the box | Collectors still write their series; `make lint` does not scan it |
| **P0.8** | Write `docs/00-merge-status.md` — one live page tracking which phase is done, mirroring both repos' habit of loud honesty | Exists, and is the first thing `README.md` links |

## Phase 1 — Prove the numbers
*The gate. Nothing after this matters if this fails. ~2–4 weeks, mostly waiting on data.*

| # | Task | Acceptance |
|---|---|---|
| **P1.1** | Verify every NSE endpoint against a live fetch — URL shapes, headers, cookie priming, column names. Decile wrote these from documented layouts with the suite network-blocked | `make doctor` reports every provider serving real bytes; each URL committed with the date it was verified |
| **P1.2** | Run the first real backfill: `make backfill FROM=2011-01-01 TO=<today>`, resumable, chunked ≤2000 days, ≤3 concurrent | `ohlcv_daily` populated; `ingest_cursor` shows a clean finish; the raw-file archive holds every fetch |
| **P1.3** | Run `reconcile_calendar` over the backfill and re-assert window lengths | Windows resolve **22/64/121/185/247**, not 22/67/127/191/256 |
| **P1.4** | Run corporate-action adjustment across the full history | `adj_factor` populated; CUPID's documented actions reproduce; rights issues correctly report `INSUFFICIENT_DATA` rather than guessing |
| **P1.5** | **Run the decisive parity test.** Set `DECILE_PARITY_BARS` and un-skip `test_reference_parity.py` — all 93 numeric columns of all 271 rows | It runs, and it is **green**. Any column that is not gets a written explanation before proceeding |
| **P1.6** | **Settle the skip-month formula.** `docs/05` §8 candidate A implies 521.31% for CUPID against a published 608.37% — a 16.7% gap the offset mismatch cannot explain | Either candidate B reproduces the published figure against real bars, or the factor is marked UNVERIFIED and excluded from any default screen |
| **P1.7** | **THE MERGE'S REAL ACCEPTANCE TEST.** For each real scan CSV in the desk's `data/uploads/`: generate the same 30 columns from the merged engine for the same date, run **both** through the desk's unmodified `scoring.py`, and diff | The top-25 rank delta table is **empty**. Any name that moves gets explained before Phase 2 opens |
| **P1.8** | Re-run P1.7 across every historical scan you hold, not just the latest | Stability across dates, not one lucky day |
| **P1.9** | Reconcile `market_health_daily` breadth against the desk's `breadth_readings` for the one date both cover | Agreement, or a written reason (universe definitions differ) |
| **P1.10** | Commit the results as `reconciliation/DESK-PARITY.md`, in the style of the existing `REPORT.md` | Committed, with the date, the data version and every unexplained delta |

> **If P1.7 does not go clean, stop.** The merged product would place orders on numbers that
> disagree with the ones the account has been traded on. Fix the engine, or narrow the merge to
> "Decile is a second opinion, not the source."

## Phase 2 — Cut the CSV cord
*Still one user, still one desk, still SQLite. The manual download dies. ~2 weeks.*

| # | Task | Acceptance |
|---|---|---|
| **P2.1** | Stand up the Decile pipeline on the Mumbai box (or a second box) — Postgres+Timescale, Redis, Celery, Beat at 19:30 IST | Ten nightly steps run green for five consecutive sessions; `data_version` publishes |
| **P2.2** | Build `MomentumScan` — the internal contract that returns the desk's 30 columns for `(date, universe)`, reusing the CSV export's exact names and precision | Byte-identical to a CSV the desk would have accepted, asserted by a fixture test |
| **P2.3** | Add a "generate scan" path to `/analyze` alongside upload. **Keep upload working.** | Both paths produce the same plan for the same date, asserted end to end |
| **P2.4** | Record `screen_run_id` (definition + `as_of` + `data_version`) on every plan (`04` §4) | An old plan can be re-run and reproduce its own inputs exactly |
| **P2.5** | Run the desk in **shadow mode** for four weekly rebalances: generate the plan both ways, execute the CSV one, diff and log the other | Four consecutive weeks with an empty order-level diff |
| **P2.6** | Switch the default to generated; upload becomes a fallback behind a flag | A Friday rebalance completes with no human download |
| **P2.7** | Wire the desk's breadth-driven cash bands to `market_health_daily` instead of the CSV-derived reading | `_cash_pct()` reads the pipeline; the 1-row `breadth_readings` table stops being written |
| **P2.8** | Delete nothing yet. `data/uploads/` is now a regression corpus, not an input | Kept, and referenced by the P1.7 test |

> **Phase 2 is the smallest thing that is worth doing on its own.** If the plan stopped here it
> would already have removed a third-party dependency, made every past decision reproducible,
> and turned the desk into a closed loop.

## Phase 3 — One codebase
*Structural. Still single-tenant. Still legal without a registration. ~4–6 weeks.*

| # | Task | Acceptance |
|---|---|---|
| **P3.1** | Move `scoring.py` → `packages/core/score.py` unchanged | Byte-identical outputs on the P1.7 corpus |
| **P3.2** | Make `rebalance.py` pure → `packages/core/basket.py`. Lift the `data/sectors.csv` read out to a caller-supplied mapping | `test_no_io_in_core` passes over it; same plans on the corpus |
| **P3.3** | Move `costs.py`, `core/regime.py`, `core/regime_alloc.py` into `packages/core/` (`exposure/`) | Pure-core test passes; regime replay is byte-identical to the persisted `regime_evaluations` rows |
| **P3.4** | Rename Decile's `regime.py` → `instrument_regime.py` (`03` §3c) | No module imports both names; a test forbids the collision |
| **P3.5** | Create `packages/execution/` from `core/{gateway,guards,risk,ratelimit}.py` | All 7 non-negotiables still enforced, each with a named test |
| **P3.6** | Unify the broker layer: one port, two faces (`MarketData` / `Trading`) | A test asserts a user token cannot reach an ingestion call and the system token cannot reach `place_order` |
| **P3.7** | Move the desk's token to Decile's **encrypted** token storage | `data/.kite_token.json` plaintext path removed |
| **P3.8** | Demote `decile_core/rebalance.py` to `rank_buffer.py`; re-express it as an input to `basket.py`; keep `/portfolios` working via an adapter | Decile's portfolio Playwright specs still pass |
| **P3.9** | **Migrate SQLite → Postgres.** One-way, scripted, with row-count and checksum assertions per table; SQLite file archived forever | Every count matches; NAV series recomputes identically; a restore drill proves the dump opens |
| **P3.10** | Convert `scripts/daily.py` and `autorun.py` to Celery tasks on Beat; retire the systemd timers **only after** five green runs | Idempotency preserved — a re-run still changes nothing |
| **P3.11** | Point the Jinja desk at the merged backend; keep it running | Every one of the 13 pages works against Postgres |
| **P3.12** | Extend observability to the desk paths: OTel spans, Sentry, Prometheus metrics on plan/execute/GTT | The order path is traceable end to end |
| **P3.13** | Write runbook #6: *"a rebalance half-executed"* | Committed, with a `Verified against: NOT YET` line like the other five |

### ⛔ SEBI GATE
**Do not start Phase 4 without a written answer to `06` D3.** Everything above improves a personal
tool and needs no registration. Everything below places orders for other people.

## Phase 4 — Multi-tenant
*The largest phase. ~8–12 weeks.*

| # | Task | Acceptance |
|---|---|---|
| **P4.1** | Add `user_id` (+ `broker_account_id` where relevant) to every desk-derived table; backfill the existing rows to the founder account | No table in the trading path lacks a tenant column; a test enumerates them |
| **P4.2** | Per-user broker connection: Kite OAuth, encrypted token per user, daily expiry surfaced in the UI | Two accounts hold two tokens; neither can read the other's holdings |
| **P4.3** | **Tenant isolation on the order path.** The gateway refuses any order whose `plan.user_id` ≠ the caller's | A test tries the cross-tenant order and gets a refusal, not a 500 |
| **P4.4** | Rate limiting per user **and** globally, still ≤9 OPS per broker account | A load test with N users never exceeds the per-account cap |
| **P4.5** | Risk ceilings per user; kill switch both per user and global | An operator can stop one account or all of them |
| **P4.6** | `BasketDefinition` — the desk's `config.py` strategy knobs become a versioned, user-owned object beside `ScreenDefinition` (`03` §3f) | System knobs provably unreachable from it |
| **P4.7** | Wire entitlements: which tier may build baskets, connect a broker, execute | The entitlement service is the only place that decides |
| **P4.8** | Per-user EOD snapshots and NAV, on the same schedule | N users, N snapshot rows, one job |
| **P4.9** | Per-user GTT stop management and reconciliation | Stops never cross accounts |
| **P4.10** | Row-level security or an equivalent enforced boundary in Postgres | A raw query without a tenant predicate cannot return another tenant's rows |
| **P4.11** | Load-test 50 concurrent users against the Kite caps in a sandbox | The limiter, not Zerodha, is what refuses |

## Phase 5 — Product surface
*Rebuild the desk in Next.js. ~6–8 weeks, parallelisable with P4.*

| # | Task | Acceptance |
|---|---|---|
| **P5.1** | `/baskets` — create from a screen, choose the construction rules, name it | A basket is a saved object with a version history |
| **P5.2** | `/baskets/[id]/plan` — the rebalance plan (the desk's `index.html`, rebuilt) | Every column the Jinja page shows, incl. pledged-share warnings |
| **P5.3** | Execution confirm flow — contract-note review, explicit confirm, 30-minute plan expiry, `DRY_RUN` badge | The four safety properties hold identically to the Jinja path |
| **P5.4** | `/baskets/[id]/stops` and `/reconcile` | Parity with `stops.html` / `reconcile.html` |
| **P5.5** | `/performance` — NAV, PRI/TRI benchmarks, drawdown, attribution | Parity with `performance.html` |
| **P5.6** | `/exposure` — R1–R4 regime overlay, read-only status + backtest | Parity with `regime.html` / `regime_backtest.html` |
| **P5.7** | `/ops` — job runs, daily-run status, failures | Parity with `ops.html` / `ops_job.html` |
| **P5.8** | Broker connection UI: connect, token status, daily re-auth prompt | The one thing no host can automate is at least obvious |
| **P5.9** | **Backtest the full loop** — screen → basket → construction rules → costs → stops, not just the screen | The PIT engine runs `basket.py`, so a backtest and a live plan share one code path |
| **P5.10** | Retire the Jinja desk, page by page, only as each React equivalent goes green | The last template is deleted, not the first |
| **P5.11** | Rewrite the marketing/legal surface for a product that executes, not one that screens | The copy lint still passes; every claim is one the product makes |

## Phase 6 — Launch gate
*Nothing here is code.*

| # | Task | Acceptance |
|---|---|---|
| **P6.1** | Complete whichever SEBI registration D3 resolves to; obtain the algo ID / broker registration if the framework applies | In hand, in writing |
| **P6.2** | Lawyer review of the four legal drafts + the 8 open questions in `DRAFT-NOTICE.md`; appoint a grievance officer and a DPO | Every `[BRACKETED]` placeholder resolved |
| **P6.3** | CA confirmation of the GST rate and SAC code (18% / 998439 are defaults, not advice) | Confirmed before the first real charge |
| **P6.4** | Razorpay webhook proven against a real test-mode delivery | A real signature verified, not a `MockTransport` |
| **P6.5** | The data-redistribution opinion that unlocks the public API constant | Written, or the constant stays `False` |
| **P6.6** | Execute all six runbooks against staging; replace every `Verified against: NOT YET` | Six lines carry a real date and real output |
| **P6.7** | A real backup uploaded to and restored from R2; a real WAL segment archived | The restore drill reads the bucket, not a fresh dump |
| **P6.8** | Run the Playwright journey suite, including the new execution journey | It has been executed, and it is green |
| **P6.9** | Independent security review of the order path and tenant isolation | Findings closed or accepted in writing |

---

## Effort shape

| Phase | Calendar | Risk | Can stop here? |
|---|---|---|---|
| P0 Foundation | ~1 wk | low | — |
| **P1 Prove the numbers** | 2–4 wk | **highest** | no — it is the gate |
| **P2 Cut the CSV cord** | ~2 wk | medium | **yes — genuinely valuable alone** |
| P3 One codebase | 4–6 wk | medium | yes — a great personal system |
| P4 Multi-tenant | 8–12 wk | high | needs the SEBI answer |
| P5 Product surface | 6–8 wk | medium | parallel with P4 |
| P6 Launch gate | 4–8 wk | external | — |

**Roughly 6–10 months to a launchable product; ~6 weeks to the version that makes your own desk
materially better.** The plan is deliberately shaped so the early value lands first and the
expensive, regulated half is a separate decision.
