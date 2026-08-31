# The desk console's UI — what is left, and what it would take to move it

**Leaf 1.5.1.** Document only. No UI is built here, and neither desk tree is modified.

M26 moved the desk's *record* onto the Baskfy web app — `/performance`, `/holdings`,
`/tradebook`, `/regime`, `/reconcile`, read-only, in plain language — and deliberately left the
**live-broker pages** on the console (`docs/DECISIONS-MERGE.md` §M26.2: *"Porting either whole
would mean a user-facing web surface holding a live broker session… that is the D3 question"*).

D3 posture B has since been signed off. The gate is now a data structure, not a memo:

```
decile-blueprint/packages/core/src/baskfy_core/broker_connections.py:82
    signed_off=True,
```

So the D3 half of M26.2's reason has lapsed. The **non-negotiable #1** half has not: nothing in
this document proposes a web surface that can place, confirm, modify or cancel an order, or set
a GTT. Those routes are quarantined in §3 and are blocked on leaf 1.5.2
(`docs/DECISION-EXECUTE-ROUTE.md`).

---

## 1. Every route the desk serves

**Count: 38 URL surfaces — 34 declared routes, 1 static mount, 3 conditional docs routes.**

Measured from `kite-momentum-rebalancer/app/main.py` (1266 lines):

```
$ grep -cE '^@app\.(get|post)\("' kite-momentum-rebalancer/app/main.py
34
```

24 `GET` + 10 `POST`. Plus `app.mount("/static", …)` at `main.py:60`, and `/docs`, `/redoc`,
`/openapi.json`, which exist only when `DESK_DOCS=true` (`app/config.py:38`, default `false`) —
the constructor comment at `main.py:35–39` says why: *"The interactive docs enumerate every
route, including the ones that place orders. Off unless asked for."*

Twelve of these render a Jinja page. `app/templates/` holds 12 templates in the merged repo
(`base.html` plus 11 pages); the live copy holds 13 (it still has `options.html`).

### The two source trees agree on the route table

Both copies were compared route-for-route:

```
$ diff <(routes in baskfy/kite-momentum-rebalancer) <(routes in portfolio/kite-momentum-rebalancer)
ROUTE SETS IDENTICAL   # 34 = 34, no route exists in one and not the other
```

**No route exists in one tree and not the other.** The 58-file divergence is entirely *below*
the route line, and three parts of it change what a route does:

| Divergence | Merged repo (`baskfy/kite-momentum-rebalancer`) | Live copy (`~/Documents/portfolio/…`) | Consequence |
|---|---|---|---|
| `app/templates/options.html` | absent (moved to `frozen/strangle/kite-momentum-rebalancer/app/templates/options.html`) | present | `/options` cannot render in the merged repo even with `OPTIONS_ENABLED=true` |
| `app/analytics/options_view.py` | absent | present | `_options_view()` (`main.py:674`) takes its `ImportError` branch and returns 404 — by design (D4) |
| `app/strategies/options*.py`, `strategies/strangle/*` (16 modules) | absent (frozen) | present | the live box is still running the strangle collectors as ops |
| `app/static/logo.svg`, `favicon.ico`, `apple-touch-icon.png` | present | absent | the live desk serves an unbranded topbar; the merged one is branded |
| `app/telemetry.py`, `token_store.py`, `scan_source.py`, `breadth_source.py`, `analytics/pg.py` | present | absent | merged-only: OTel spans, the token store, the generated-scan path (M13/M14), Postgres |
| `app/templates/base.html`, `index.html`, `settings.html` | differ | differ | nav, desk home and the settings form have all moved on in the merge |

So the live desk at `desk.modelbasket.in` is running an *older* console with a *live* options lab
attached. Anything ported must be ported from the **merged** tree, which is the one that is under
gates.

### The inventory

`Kind` — **page** renders HTML, **json** is a data twin of a page, **action** is a POST that
changes something, **infra** is neither.

| # | Route | Kind | Template / handler | What it is | Classification |
|---|---|---|---|---|---|
| 1 | `GET /` | page | `index.html` · `main.py:178` | Desk home: session state, daily-collection health, stop coverage read off the broker, post-login autorun status — **and** the scan upload → Analyze → review → Execute flow | **ORDER-CAPABLE** (see §3). Read-only panels: partly **needs porting** |
| 2 | `GET /login` | infra | `main.py:228` | Redirect to Kite's login URL for the *desk process* | **dies with the desk** — Baskfy's per-user equivalent is `POST /brokers/{id}/connect` (`routers/brokers.py:333`) on `/brokers` |
| 3 | `GET /callback` | infra | `main.py:233` | Exchanges `request_token`, **then fires the `autorun` collection** because logging in is the moment the blocker clears | **dies with the desk** for the token half (Baskfy has `GET /brokers/callback`, `brokers.py:273`). The autorun side-effect **needs porting** — see §5 |
| 4 | `POST /analyze` | action | `main.py:291` | Scores a scan, pulls live holdings + cash + LTP, runs the funding check, returns the plan and **mints the `plan_id` `/execute` requires** | **ORDER-CAPABLE** (see §3) |
| 5 | `POST /execute` | action | `main.py:515` | Places the batch through the gateway | **ORDER-CAPABLE** (see §3) |
| 6 | `GET /options` | page | `options.html` · `main.py:687` | The strangle lab's status page | **dies with the desk** — D4 froze it; 404 by default and unrenderable in the merged tree |
| 7 | `POST /options/run` | action | `main.py:704` | Starts one of the 5 allowlisted options operations | **dies with the desk** — same reason |
| 8 | `GET /options/data` | json | `main.py:729` | JSON twin of `/options` | **dies with the desk** — same reason |
| 9 | `GET /reconcile` | page | `reconcile.html` · `main.py:738` | Plan vs **live broker**: open orders, resting GTTs, holdings straight from Kite. Also reconciles fills as a side effect of loading | **already ported** (record half) → Baskfy `/reconcile` + `GET /desk/reconcile` (`routers/desk.py:412`). The live-broker comparison **needs porting** |
| 10 | `GET /reconcile/data` | json | `main.py:769` | JSON twin | **already ported** → `GET /desk/reconcile` |
| 11 | `GET /stops` | page | `stops.html` · `main.py:786` | Reviews the GTT stops that *would* be armed; mints the `plan_id` `/stops/arm` consumes | **ORDER-CAPABLE** (see §3) |
| 12 | `POST /stops/arm` | action | `main.py:812` | Cancels wrong triggers and arms new ones at the broker | **ORDER-CAPABLE** (see §3) |
| 13 | `GET /indices` | page | `indices.html` · `main.py:888` | Every NSE index board, read-only | **already ported** → Baskfy `/market/today`, `IndexDashboard` over `GET /indices/dashboard` (`routers/market_data.py:105`) |
| 14 | `GET /indices/data` | json | `main.py:894` | JSON twin | **already ported** → `GET /indices/dashboard` |
| 15 | `GET /indices/constituents` | json | `main.py:900` | Who is in one index, with a live quote **and the desk's own screen score for each name** | **needs porting** — no Baskfy equivalent; a repo-wide grep for `constituents` finds only basket constituents, never index drilldown |
| 16 | `GET /regime` | page | `regime.html` · `main.py:920` | The market stance in force and the sentences the machine wrote when it decided | **already ported** → Baskfy `/regime` + `GET /desk/regime` (`routers/desk.py:371`) |
| 17 | `GET /regime/data` | json | `main.py:944` | JSON twin | **already ported** → `GET /desk/regime` |
| 18 | `GET /regime/backtest` | page | `regime_backtest.html` · `main.py:929` | The overlay's backtest evidence, read from `data/outputs/regime_backtest.json`, *"kept off the live status page on purpose"* | **needs porting** — Baskfy's `/build/backtests` backtests *screens*, not the regime overlay; this is the only record of why the overlay is trusted |
| 19 | `GET /ops` | page | `ops.html` · `main.py:953` | The operations console: 19 allowlisted equity jobs, the running job, daily-collection status and the last 10 runs | **needs porting** — `/admin/pipeline` covers the *screener's* pipeline, not the desk's 19 jobs |
| 20 | `POST /ops/run` | action | `main.py:965` | Starts one allowlisted job. *"Never a free-form command."* | **needs porting** (a write, not an order — see §4b) |
| 21 | `GET /ops/data` | json | `main.py:981` | Running job, history, and the operation catalog | **needs porting** |
| 22 | `GET /ops/job/{job_id}` | page | `ops_job.html` · `main.py:993` | One job's stored argv, exit code and captured output | **needs porting** |
| 23 | `GET /performance` | page | `performance.html` · `main.py:1065` | The TWR record **plus** `_holdings_block()` — the live holdings table with GTT and scan context | **already ported** (record half) → Baskfy `/performance` + `/holdings`, `GET /desk/performance` and `GET /desk/holdings`. Live-quote columns **need porting** |
| 24 | `GET /performance/data` | json | `main.py:1075` | JSON twin | **already ported** → `GET /desk/performance` |
| 25 | `GET /tradebook` | page | `tradebook.html` · `main.py:1100` | Every trade, open lots, closed trades, recorded corporate actions | **already ported** → Baskfy `/tradebook` + `GET /desk/tradebook` (`routers/desk.py:322`) |
| 26 | `POST /tradebook` | action | `main.py:1115` | Import a Console tradebook CSV and rebuild the lots it covers | **needs porting** (a write, not an order — see §4b). No Baskfy equivalent: no tradebook import route exists in `services/api/src/baskfy_api/routers/` |
| 27 | `POST /tradebook/corporate-action` | action | `main.py:1138` | Record a bonus or split, then rebuild that symbol's lots | **needs porting** (a write — §4b). No Baskfy write surface for corporate actions |
| 28 | `POST /tradebook/corporate-action/delete` | action | `main.py:1161` | Remove a recorded action; lots self-heal because they are derived | **needs porting** (a write — §4b) |
| 29 | `GET /tradebook/data` | json | `main.py:1172` | JSON twin | **already ported** → `GET /desk/tradebook` |
| 30 | `GET /settings` | page | `settings.html` · `main.py:1181` | The desk process's own runtime config, with the locked risk ceilings shown but not editable | **dies with the desk** — it configures a process that will not exist |
| 31 | `POST /settings` | action | `main.py:1193` | Validate, persist, audit and apply | **dies with the desk** |
| 32 | `POST /settings/reset` | action | `main.py:1220` | Drop overrides back to `.env` | **dies with the desk** |
| 33 | `GET /settings/data` | json | `main.py:1232` | JSON twin | **dies with the desk** |
| 34 | `GET /status` | json | `main.py:1247` | `dry_run`, `force_ipv4`, `authed`, `cash`, and with `?ip=1` **the outbound IP the broker will see** | **dies with the desk** for liveness (`/brokers` answers "am I connected"). The `?ip=1` half **needs porting** — see §5 |
| 35 | `/static/{path}` | infra | `main.py:60` | The vendored stylesheet, fonts and marks | **dies with the desk** |
| 36 | `GET /docs` | infra | conditional on `DESK_DOCS` | Swagger UI | **dies with the desk** — Baskfy publishes its own OpenAPI |
| 37 | `GET /redoc` | infra | conditional on `DESK_DOCS` | ReDoc | **dies with the desk** |
| 38 | `GET /openapi.json` | infra | conditional on `DESK_DOCS` | The schema | **dies with the desk** |

### Nothing is reachable that is not in this table

`app/templates/` holds exactly 12 files and every one of them is named by a
`templates.TemplateResponse(...)` call above (`base.html` is extended, never rendered directly).
`app/static/` holds `app.css`, `src.css`, `fonts/`, `logo.svg`, `logo-mark.png`, `favicon.ico`,
`apple-touch-icon.png` — assets, no HTML. There is no second router, no `include_router`, no
sub-application: `grep -n "include_router\|mount(" app/main.py` returns the single `/static`
mount at line 60.

## 2. Classification tally

Every one of the 38 surfaces gets exactly one **primary** classification, so the four counts
sum to 38. Five routes are two things in one URL and carry a **secondary** classification too;
those are recorded in the table above rather than rounded away.

| Primary classification | Count | Routes |
|---|---|---|
| **already ported** | 10 | 9, 10, 13, 14, 16, 17, 23, 24, 25, 29 |
| **needs porting** | 9 | 15, 18, 19, 20, 21, 22, 26, 27, 28 |
| **dies with the desk** | 14 | 2, 3, 6, 7, 8, 30, 31, 32, 33, 34, 35, 36, 37, 38 |
| **ORDER-CAPABLE — blocked, see §3** | 5 | 1, 4, 5, 11, 12 |
| **Total** | **38** | |

The five split routes and their secondary halves:

| Route | Primary | Secondary half |
|---|---|---|
| 1 `GET /` | ORDER-CAPABLE | **needs porting** — the collection-health and stop-coverage panels (plan P2, P3) |
| 3 `GET /callback` | dies with the desk | **needs porting** — the post-login `autorun` trigger (§5 item 2) |
| 9 `GET /reconcile` | already ported | **needs porting** — the live-broker comparison (plan P5) |
| 23 `GET /performance` | already ported | **needs porting** — the live-quote holdings columns (folded into P3/P5) |
| 34 `GET /status` | dies with the desk | **needs porting** — the `?ip=1` outbound-IP report (§5 item 6) |

---

## 3. BLOCKED — routes that can place, confirm, modify or cancel an order, or set a GTT

**These are not ordinary porting work and are deliberately absent from the §4 plan.**

They are blocked on two things at once, and either alone is sufficient:

1. **Leaf 1.5.2 / `docs/DECISION-EXECUTE-ROUTE.md`** — whether Baskfy gets an execute route at
   all is an undecided question. D3 sign-off did not decide it.
2. **Non-negotiable #1** (root `CLAUDE.md`): *"Never auto-execute. Orders fire only from
   `POST /execute` with `confirm=true` and the `plan_id` issued by `/analyze`."* Plus the
   standing rail: *"The web app never gains an execute route."*

| Route | Why it is order-capable | Evidence |
|---|---|---|
| `POST /execute` | Places the batch. `gw.place(symbol=…, side=…, product="CNC", order_type="LIMIT", …)` per order | `main.py:515–650` |
| `POST /analyze` | Not itself an order — but it is the **other half of the confirm pair**. It mints `plan_id` into `PLANS`, and `/execute` accepts nothing else: *"Unknown or expired plan_id — re-run Analyze."* Porting `/analyze` without deciding 1.5.2 builds half a firing mechanism | `main.py:291`, `main.py:519–521` |
| `GET /` | Hosts both. `index.html:169` posts to `/analyze`; `index.html:401` posts `confirm=true` to `/execute` and the page renders the confirmation ticket | `app/templates/index.html:106, 169, 401` |
| `GET /stops` | Mints the `STOP_PLANS[plan_id]` that `/stops/arm` consumes, on the same 30-minute expiry as `/execute`. M26.2 already refused to port it: *"a stops page that cannot tell you whether your stops are live is a page that misleads by existing"* | `main.py:786–810`; `DECISIONS-MERGE.md` §M26.2 |
| `POST /stops/arm` | **Creates and deletes GTT triggers at the broker.** `k.delete_gtt(...)` then `k.place_gtt_stop(...)`. This is also the desk's one documented exception to "everything goes through the gateway" (non-negotiable #6) — GTTs go through `kite_client.place_gtt_stop`, and M16 owns closing that gap | `main.py:812–870` |

**One thing that is *not* on this list, and should be noticed.** Baskfy already ships a
non-executing hand-off: `GET /baskets/plan/kite` (`routers/kite.py:100`) turns *the desk's
latest plan* into a Kite Publisher basket — *"hand-off, not execution"* — and the web app
already has the components for it (`components/cb/kite-basket-invest.tsx`,
`components/cb/kite-basket-form.tsx`), wired today only for curated baskets via
`GET /explore/{slug}/kite`. If 1.5.2 decides against an execute route, **wiring the existing
`/baskets/plan/kite` endpoint to a page is the port**, and it needs no new decision: the order
is placed by Zerodha's own form, in the user's own session, with the user's own hands. That is
a §4 candidate the moment 1.5.2 says so, and it is listed there as P6 conditional.

---

## 4. Ordered port plan — the read-only pages

Effort is one engineer's rough person-days, including the API endpoint, the page, and the
read-only assertions M26.3 established (`test_desk_readonly.py`,
`lib/desk/__tests__/read-only.test.ts`).

| # | Page | Why this order | Baskfy API needed first | Effort |
|---|---|---|---|---|
| **P1** | **Desk operations — run history and job output** (ports `GET /ops`, `/ops/data`, `/ops/job/{id}`) | Highest daily value and zero regulatory surface. Right now a failed nightly collection is invisible without SSH, and the desk's own comment says why that matters: *"a silently broken daily job costs history that cannot be backfilled"* (`main.py:186`). Read-only first: show the 19 operations, the running job, the last runs, and one job's captured output | **New.** `GET /desk/ops/operations`, `GET /desk/ops/jobs?limit=`, `GET /desk/ops/jobs/{id}`. All three read `ops_jobs` in the desk SQLite/Postgres. No broker call, no writes | **2–3 d** |
| **P2** | **Daily-collection health strip** (ports the `collection` and `auto` panels of `GET /`) | The single most-looked-at thing on the console, and it is the read-only half of a page whose other half is blocked. Lifting it out means route #1's value survives 1.5.2 whatever 1.5.2 decides. Belongs on Baskfy `/home` or as a banner on `/performance` | **New.** `GET /desk/collection` — wraps `analytics/daily_runs.status()` + `history(limit=10)`. Pure SQL read | **1–2 d** |
| **P3** | **Stop coverage, reported not armed** (the `protection` panel of `GET /`) | Non-negotiable #4 says *every buy gets a GTT stop the same session*. Today the only browser that can check that is the console. **Reporting** coverage is read-only and is now permissible — D3-B is signed and `brokers.py` already holds an encrypted token — while **arming** stays in §3. This is the safe half of what M26.2 refused | **New.** `GET /desk/protection` — needs a live Kite read (`analytics/protection.from_kite`). First endpoint in this list that touches a broker, so it must degrade to `null` on an expired token exactly as `main.py:200–206` does, and must be covered by the M26.3 assertion that the *desk record* module still cannot reach a broker (put this on the brokers module, not on `routers/desk.py`) | **3–4 d** |
| **P4** | **Index constituents drilldown** (ports `GET /indices/constituents`) | `/market/today` already shows the board; this is the missing click-through — the members of one index with a live quote and the desk's screen score beside each. Cheap because the board is already there and `market_data.py` already owns the index surface | **New.** `GET /indices/{index}/constituents`. Quotes degrade to blank columns without a session, as the desk's own handler already does (`main.py:900–918`) | **2 d** |
| **P5** | **Reconcile against live broker state** (completes `GET /reconcile`) | Baskfy `/reconcile` answers *"did the record say it happened"*; only the console answers *"does the broker agree"*. The gap is exactly the case that matters — a fill that never got recorded. Deferred to P5 because it is the largest live-broker read and because the recorded half is already useful | **New.** `GET /desk/reconcile/live` — reads `kc.orders()`, `kc.get_gtts()`, `holdings()`. **Must not** carry the desk's `reconcile_fills` write side-effect (`main.py:756`): reporting a disagreement and silently repairing it are two acts, and only the first belongs on a read-only page | **4–5 d** |
| **P6** | **Regime backtest evidence** (ports `GET /regime/backtest`) | Last because it is consulted rarely. It is still the only record of why the overlay is trusted, and it dies with the console otherwise | **New.** `GET /desk/regime/backtest` — serves the stored `regime_backtest.json` through `analytics/backtest_view.build()`. No broker, no DB write | **2 d** |
| **P6c** | *(conditional on 1.5.2)* **Plan → Kite Publisher basket** | Not new work — `GET /baskets/plan/kite` exists and the components exist. Only unblocked if 1.5.2 chooses the hand-off answer | Already exists: `routers/kite.py:100` | **1 d** |

**Read-only total: 14–18 person-days** (P1–P6), of which 7–9 are the two live-broker reads
(P3, P5) and 7–9 are pure database reads (P1, P2, P4, P6).

### 4b. Writes that are not orders — not in the read-only plan, not blocked on 1.5.2 either

These four change stored state but cannot reach an exchange. They are ordinary work, they need
an admin gate, and they should follow P1–P6 rather than interleave with them.

| Route | Port note | Effort |
|---|---|---|
| `POST /ops/run` | The natural completion of P1. Keep the allowlist verbatim — the browser sends an operation **name**, never a command — and keep `writes=False` visible per operation | 2 d |
| `POST /tradebook` (CSV import) | No Baskfy equivalent exists. Overlaps the CAS-import work in `PORTFOLIO_REDESIGN.md` §B4; decide there whether these are one importer or two before building | 3 d |
| `POST /tradebook/corporate-action` | Baskfy reads corporate actions (`routers/instruments.py`) but has no write surface. Lots are derived, so recording and rebuilding is the whole operation | 2 d |
| `POST /tradebook/corporate-action/delete` | Same surface, same rebuild path | 0.5 d |

---

## 5. What the operator loses if the console disappears first

Maulik is the operator. This is the honest version.

**1. He cannot rebalance at all.** This is not a degradation, it is a stop. `GET /` → `POST
/analyze` → `POST /execute` is the entire Friday, and every one of those is in §3, blocked on a
decision that has not been taken. The root agreement's own rail says *"The desk must be able to
rebalance on any Friday."* **The console cannot be switched off before 1.5.2 is decided and its
answer is built.** Everything below assumes a partial retirement where the trading flow stays.

**2. Logging in stops collecting data.** `GET /callback` does not just exchange a token — it
starts the `autorun` collection, and the code says exactly why: *"on 17-18 Aug 2026 both exited
2 for want of one — two sessions of straddle observations and an EOD snapshot lost, none of it
recoverable."* Kite's token expires overnight with no refresh, so the login **is** the trigger.
Baskfy's `/brokers` OAuth stores a token; it does not fire a collection. Retire the console
without replacing that hook and the data plant silently loses days it cannot backfill.

**3. Every operational job goes back to SSH.** Nineteen allowlisted operations — the daily
collection, the EOD snapshot, benchmark PRI, index history, tradebook capture and reconcile,
stop-coverage check, the regime backtests, `db --info`, the test suite — become
`ssh box && python -m scripts.…`. Two of them are time-boxed against the market: `snapshot`
must run after 15:30 IST, and `tradebook_capture` must run **the same session**, because *"Kite's
trade book is same-day only, so a fill not captured on its own session survives nowhere but a
Console export."* A job you have to SSH for is a job that gets skipped on a busy Friday, and
that one gets skipped into permanent data loss.

**4. He cannot see whether his stops are real.** `/stops` is the only surface that compares live
holdings against live GTT triggers and can cancel a wrong one. The desk carries a scar here:
18 Aug 2026 left 10,383 shares of GTT against 9,478 held — SONACOMS covered 2.5×, RADICO 3.1×,
and 438 shares of PARAS were stopped before a single share had filled. An over-covered trigger
is short delivery when it fires. P3 restores *seeing* it; nothing outside §3 restores *fixing*
it, so between P3 and 1.5.2 the honest state is "you will know, and you will have to go to
Kite's own web console to act."

**5. "Did it actually happen" gets softer.** Baskfy `/reconcile` compares the plan to the
desk's record. The console compares it to the broker. Those differ precisely when the record is
wrong — which is the case worth having a page for. Until P5, a fill that never got written down
is invisible on the web app and visible only on the console.

**6. The one thing a rejected batch turns on.** `GET /status?ip=1` reports the outbound IP the
broker sees. The code calls it *"the one thing a rejected batch turns on and the one thing you
cannot read off this machine"* — Kite authorises orders against an IP allowlist, and 18 Aug 2026
burned twenty-one orders into *"No IPs configured for this app"*. It is four lines of code and
it belongs somewhere in Baskfy's admin surface before the console goes.

**7. Small things that are only small until they are not.** The tradebook CSV importer (the only
way to recover fills Kite has already aged out); recording a bonus or split (the only way to fix
lots after a corporate action); the index-constituents drilldown with the screen's score; the
regime backtest that is the entire evidence base for the overlay; and the runtime settings page —
which genuinely dies with the desk, but whose disappearance means every desk knob becomes an
`.env` edit and a restart.

**What he keeps, and it is real:** the performance record, holdings, the tradebook, the market
stance, and plan-vs-fills are already on Baskfy in plain language, read-only, asserted by two
test suites. M26 moved the part of the console that mattered to *reading*. What is left on the
desk is the part that mattered to *operating* — and that is the harder half.

---

## Provenance

- `kite-momentum-rebalancer/app/main.py` (1266 lines) — the route table, read route by route
- `kite-momentum-rebalancer/app/templates/` (12 files), `app/static/` — reachable surfaces
- `kite-momentum-rebalancer/app/analytics/ops.py:212–340` — the 19 equity operations
- `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` — the live copy, compared
- `decile-blueprint/services/api/src/baskfy_api/routers/{desk,market_data,brokers,kite,admin}.py`
- `decile-blueprint/apps/web/src/app/(app)/` — 57 Next.js pages, checked for equivalents
- `docs/DECISIONS-MERGE.md` §M26.1–.5 — what M26 ported and what it refused, and why
- `docs/00-merge-status.md:129` — the M26 status line this leaf is answering
