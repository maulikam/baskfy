# Which copy of the momentum desk is the truth

**Leaf 1.1.2, desk retirement tree. Written 31 Aug 2026. Both source trees were read only.**

Two copies of the momentum execution desk exist and they have diverged:

| | Path | Git state at time of writing |
|---|---|---|
| **External** (the live desk's own repo) | `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` | branch `indices-board`, HEAD `1cb5cb5` (25 Aug 2026), working tree clean |
| **Subtree** (in this monorepo) | `/Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer` | imported at `f1bcbb8` (22 Aug 2026) by `git subtree add`, then evolved in-repo through M38 |

`diff -rq` over `app/`, excluding `__pycache__`, reports **58 differing entries**: 26 files
present in both whose content differs, plus 32 files present in only one tree. Every one of the
58 is enumerated in §2. Nothing here is a sample.

---

## 0. The one-line answer

**Neither copy is wholly authoritative, and the split is clean:**

* For **what the order path and the risk manager DO**, the **external repo at `1cb5cb5` is the
  truth**, and the subtree is provably four commits stale. All four of those commits were written
  on 25 Aug 2026 out of real production incidents on the live box.
* For **where code LIVES and what the merged product has since become** — the M6 options freeze,
  the M15/M16 moves into `packages/core` and `packages/execution`, P4.3 tenancy, the encrypted
  token store, the Postgres backend, the risk-ceiling hardening — the **subtree (plus
  `decile-blueprint/packages/*`) is the truth**, and the external repo has none of it.

A port leaf that reads only the subtree will ship a desk that is missing the market-order
protection band, sends resting LIMIT orders that lapse, does not fit the plan to available
margin, measures the daily loss cap against invested capital instead of NAV, and re-introduces
the CSRF header bug that refused every form on the live box for four days.

A port leaf that reads only the external repo will undo the entire merge.

**The correct instruction for every port leaf is in §4.** In short: take *structure* from the
subtree and `decile-blueprint/packages/*`; take *the four 25 Aug behaviours* from the external
repo and land them in `baskfy_execution` / `baskfy_core`, not in the desk shims.

---

## 1. Why the fork point is knowable exactly

The subtree import was byte-exact, which is what makes every classification below a fact rather
than a guess:

```
external: git rev-parse df6cb72:app          -> 2913aae9dcc91b7e0af08c4b18818de877ea3e1d
baskfy:   git rev-parse f1bcbb8:kite-momentum-rebalancer/app -> 2913aae9dcc91b7e0af08c4b18818de877ea3e1d
```

Identical tree hashes. The common ancestor of both copies of `app/` is external commit
**`df6cb72` "Carry every published index, not just the 139 NSE quotes live" (21 Aug 2026)**.

Both working trees are clean with respect to their own HEADs (external: `git status --porcelain`
empty; subtree: the only uncommitted change anywhere under `kite-momentum-rebalancer/` is a
pre-existing edit to `.gitignore` dated 26 Aug 2026, five days before this leaf ran — see G5).
So **every one of the 58 differences is fully explained by committed history on one side or the
other**, and the classification method is git, not heuristics.

### The complete set of external commits after the fork point

```
1cb5cb5  25 Aug 2026  Stop the desk's own hardening header from refusing its own forms
be93f38  25 Aug 2026  Give every market order the protection band Kite requires
5d1ff3b  25 Aug 2026  Measure the daily loss cap against NAV, not against invested
0a596f4  25 Aug 2026  Fit the plan to the money, and take the fill instead of resting on a limit
--------  fork point --------
df6cb72  21 Aug 2026  (imported into this monorepo as f1bcbb8)
```

Four commits. They touch exactly ten files under `app/`:

```
app/analytics/plan_store.py   app/analytics/regime_run.py   app/config.py
app/core/gateway.py           app/core/risk.py              app/core/websec.py
app/kite_client.py            app/main.py                   app/rebalance.py
app/templates/index.html
```

…and five files outside it: `.env.example`, `deploy/caddy/Caddyfile`,
`tests/test_execute_gateway.py`, `tests/test_websec.py`, and a new `tests/test_funding_fit.py`
(266 lines, **absent from the subtree entirely**).

The subtree, for its part, has touched 54 files under `app/` since the import. The two sets
overlap in six files. That arithmetic reproduces the measured count exactly:

```
54 (subtree-side) + 10 (external-side) - 6 (both) = 58
```

**Method used per file.** Git history in both trees, for all 58 — because the fork point is
byte-exact, membership in "changed since `f1bcbb8`" and "changed since `df6cb72`" is decisive on
its own. Content comparison (`diff -u`, `cmp`) was used *in addition* on every safety-relevant
file and on every file whose change turned out to be a module move, to distinguish a relocation
from a rewrite. Where a row below says something about *what* changed, that came from reading the
diff, not from the filename.

---

## 2. The complete 58-entry divergence table

Direction is relative to the common ancestor `df6cb72` / `f1bcbb8`:

* **external-ahead** — only the external repo changed it. The subtree still holds ancestor content.
* **subtree-ahead** — only the subtree changed it. The external repo still holds ancestor content.
* **forked** — both sides changed it independently. These six need a merge, not a copy.

Line counts are `diff -u external → subtree`: `+n` lines the subtree has that external does not,
`-n` lines external has that the subtree does not.

### 2a. Forked — both sides moved (6 files). These are the merge sites.

| File | Class | Δ | What each side has that the other does not |
|---|---|---|---|
| `app/config.py` | **forked** | +15 / -51 | External: `REBALANCE_ORDER_TYPE`, `MARKET_PROTECTION`, `FIT_PLAN_TO_FUNDS`, `FUNDING_HEADROOM_PCT`. Subtree: `SOLE_USER_ID`, `SOLE_BROKER_ACCOUNT_ID`, `SCAN_SOURCE_DEFAULT` |
| `app/core/gateway.py` | **forked** | +42 / -121 | External: real gateway + market-protection band + `ref_price` journalling. Subtree: an M16 shim over `baskfy_execution.gateway`, which adds `ProductGates` and P4.3 tenancy but lacks the band |
| `app/core/risk.py` | **forked** | +2 / -50 | External: `RiskManager` with `on_pnl(pnl, basis=...)`. Subtree: an M16 re-export of `baskfy_execution.risk`, whose `on_pnl` has no `basis` |
| `app/kite_client.py` | **forked** | +18 / -17 | External: market-protection band on the no-limit-price branch of `place_cnc_order`. Subtree: encrypted token store (`token_store.store_for`) replacing plain-JSON-at-0600 |
| `app/main.py` | **forked** | +209 / -81 | External: `fit_to_funds` at analyse time, `order_type=C.REBALANCE_ORDER_TYPE`, NAV-based loss cap. Subtree: generated-scan path (M13), telemetry (M20), startup risk-ceiling logging (M4) |
| `app/rebalance.py` | **forked** | +36 / -279 | External: the 91-line `fit_to_funds`. Subtree: an M15 boundary shim over `baskfy_core.basket` |

### 2b. External-ahead — the subtree is stale, nothing of its own to preserve (4 files)

| File | Class | Δ | What the subtree is missing |
|---|---|---|---|
| `app/analytics/plan_store.py` | external-ahead | +3 / -5 | Comment only: the SUBMITTED-vs-FILLED note updated for market orders (`0a596f4`). No behaviour. |
| `app/analytics/regime_run.py` | external-ahead | +2 / -6 | **Behavioural.** Subtree hardcodes `order_type="LIMIT"`; external reads `C.REBALANCE_ORDER_TYPE` so the regime path and `/execute` cannot disagree (`0a596f4`). |
| `app/core/websec.py` | external-ahead | +10 / -37 | **Behavioural.** The `Sec-Fetch-Site` CSRF fix and the `Origin: null` handling (`1cb5cb5`). |
| `app/templates/index.html` | external-ahead | +12 / -37 | The contract-note UI for the funding fit and the order type (`0a596f4`). |

### 2c. Subtree-ahead — the external repo is stale (48 entries)

**Sixteen** files that exist in both trees and were changed only on the subtree side:

| File | Class | Δ | Nature of the change |
|---|---|---|---|
| `app/analytics/autorun.py` | subtree-ahead | +24 / -8 | Options collection made conditional on `OPTIONS_ENABLED` + `ImportError` tolerated (M6 freeze) |
| `app/analytics/db.py` | subtree-ahead | +51 / -6 | Postgres backend selection (`DESK_DB_BACKEND`), M19 §3 |
| `app/analytics/ops.py` | subtree-ahead | +99 / -46 | Strangle ops controls made lazy and conditional (M6 freeze) |
| `app/analytics/regime_view.py` | subtree-ahead | +48 / -12 | Backend-portable SQL (`ROW_NUMBER()` instead of `MAX()`-group-by, PRAGMA probes guarded) |
| `app/analytics/settings.py` | subtree-ahead | +23 / -14 | **Risk-relevant, see §3.5.** Six `RISK_*` ceilings removed from the editable settings page |
| `app/core/guards.py` | subtree-ahead | +2 / -63 | M16 move only — `baskfy_execution/guards.py` is **byte-identical to external HEAD** |
| `app/core/ratelimit.py` | subtree-ahead | +2 / -75 | M16 move only — `baskfy_execution/ratelimit.py` is **byte-identical to external HEAD** |
| `app/core/regime.py` | subtree-ahead | +4 / -1008 | M15 move only — `baskfy_core/exposure/regime.py` is byte-identical to external |
| `app/core/regime_alloc.py` | subtree-ahead | +2 / -358 | M15 move — `baskfy_core/exposure/allocation.py`, 2 changed lines (header docstring) |
| `app/costs.py` | subtree-ahead | +18 / -97 | M15 move — `baskfy_core/costs.py` differs in 74 lines, **all reformatting and type annotations**; every statutory constant and every formula is identical |
| `app/scoring.py` | subtree-ahead | +21 / -81 | M15 boundary shim over `baskfy_core.score` |
| `app/static/app.css` | subtree-ahead | +1 / -1 | Built CSS, brand mark (M37/M38) |
| `app/static/src.css` | subtree-ahead | +2 / -1 | Source CSS, brand mark (M37/M38) |
| `app/strategies/strangle/__init__.py` | subtree-ahead | +22 / -6 | Docstring rewritten to record the M6 freeze and why four modules stayed |
| `app/templates/base.html` | subtree-ahead | +16 / -4 | Real favicon/logo assets; Options nav entry gated on `options_enabled` |
| `app/templates/settings.html` | subtree-ahead | +22 / -11 | Risk-limits block turned from editable form into read-only display (M4) |

Nine files that exist **only in the subtree** (all added post-import, all M13–M38 merge work):

| File | Class | Why it is absent from the external repo |
|---|---|---|
| `app/analytics/pg.py` | subtree-ahead (added) | 221 lines. Postgres connection wearing sqlite3's interface — M19 §3. Never existed on the live desk, which is still SQLite |
| `app/breadth_source.py` | subtree-ahead (added) | 114 lines. Where the cash-band breadth number comes from — M14 §1, part of cutting the cord to the external screener |
| `app/scan_source.py` | subtree-ahead (added) | 122 lines. Generates a weekly scan from the merged screener's database — M13 §2. The live desk still uploads a CSV |
| `app/telemetry.py` | subtree-ahead (added) | 176 lines. Observability over the order path — M20 §1 |
| `app/token_store.py` | subtree-ahead (added) | 67 lines. Encrypted at-rest Kite token — M16 / P3.7. The live desk still writes plain JSON at 0600 |
| `app/static/favicon.ico` | subtree-ahead (added) | M37/M38 brand mark |
| `app/static/apple-touch-icon.png` | subtree-ahead (added) | M37/M38 brand mark |
| `app/static/logo.svg` | subtree-ahead (added) | M37/M38 brand mark |
| `app/static/logo-mark.png` | subtree-ahead (added) | M37/M38 brand mark |

Twenty-three files that exist **only in the external repo**. Every one of them is the M6 options
freeze (`CLAUDE.md` D4: *frozen, not deleted*). **All 23 were verified byte-identical to their
copies under `frozen/strangle/kite-momentum-rebalancer/` — `cmp` over 27 files (these 23 plus the
four strangle modules the desk kept) reported `identical=27 differs=0 missing=0`.** Nothing was
lost; it moved out of `app/` so the equity desk's gates could stop importing it.

| File | Class | Frozen copy |
|---|---|---|
| `app/analytics/options_view.py` | subtree-ahead (moved to `frozen/`) | 350 lines, identical |
| `app/strategies/options.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/options_costs.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/options_experiment.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/options_market.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/options_skeleton.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/adjust.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/allocation.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/attribution.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/book.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/calibrate.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/fills_live.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/fills_paper.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/journal.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/levels.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/live.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/market.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/rules.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/selection.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/session.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/sizing.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/strategies/strangle/state.py` | subtree-ahead (moved to `frozen/`) | identical |
| `app/templates/options.html` | subtree-ahead (moved to `frozen/`) | identical |

> Four strangle modules — `calendar_nse.py`, `clock.py`, `config.py`, `instruments.py` — are in
> **both** trees and identical, so they do not appear in the 58. They are the NSE trading
> calendar, session clock, index registry and YAML loader; `scripts/autorun.py` depends on them,
> which is why M6 left them behind.

**Row count check.** §2a forked = 6. §2b external-ahead = 4. §2c in-both subtree-ahead = 16,
subtree-only = 9, external-only = 23, giving 48 subtree-ahead entries.
**6 + 4 + 48 = 58**, matching the measured `diff -rq` count exactly. ✔

---

## 3. Behavioural deltas that affect order placement or risk

These are the findings that make this leaf necessary. Each was read out of the diff, not inferred
from a filename.

### 3.1 Market-order protection band — MISSING FROM THE MERGED PRODUCT ENTIRELY

* **Commit:** `be93f38` "Give every market order the protection band Kite requires" (25 Aug 2026)
* **Why it exists:** the first live batch after the switch to MARKET was **refused in full** by
  Kite — *"Market orders without market protection are not allowed via API. Please set market
  protection or use a Limit order."* Nothing reached the exchange; the circuit breaker stopped the
  session after three.
* **What it does:** adds `MARKET_PROTECTION` (default `-1`, meaning "apply Zerodha's own
  guideline") to `app/config.py`; `OrderGateway.place()` attaches
  `params["market_protection"]` to **every** `MARKET` order, cast to `int` when integral because
  `-1` is a sentinel matched exactly and `"-1.0"` is a different string on the wire; and
  `kite_client.place_cnc_order` carries it on its no-limit-price branch so that path is not a trap.
* **Does the in-repo subtree have it?** **No — and neither does anywhere else in the monorepo.**
  A repo-wide search excluding `node_modules`, `.git`, `__pycache__` and `.venv` for the string
  `market_protection` returns **0 files**. The merged gateway
  `decile-blueprint/packages/execution/src/baskfy_execution/gateway.py` has no `if order_type ==
  "MARKET"` branch at all.
* **Severity:** today it is latent rather than live, because the merged product also lacks the
  MARKET switch (§3.2) and still sends LIMIT. The moment any leaf ports the MARKET behaviour
  without this, **every buy and every sell is refused by the broker before it reaches the
  exchange.** These two must be ported together or not at all.

### 3.2 Order type: MARKET, not resting LIMIT

* **Commit:** `0a596f4` "Fit the plan to the money, and take the fill instead of resting on a
  limit" (25 Aug 2026)
* **What it does:** `REBALANCE_ORDER_TYPE = os.getenv("REBALANCE_ORDER_TYPE", "MARKET")`. A limit
  at the plan's reference price is priced at the instant of analysis, and the names a momentum
  rebalance wants are the ones moving away from it — the order rests until the close and lapses,
  so the book ends the day holding what fell back and missing what ran. `ref_price` is still
  passed to the gateway: ignored as a limit under MARKET, but it *values the order for the risk
  manager*, and dropping it would zero every order and let the caps pass anything.
* **Where it is applied:** `app/main.py` `/execute` (external line 451:
  `order_type=C.REBALANCE_ORDER_TYPE`) and `app/analytics/regime_run.py` line ~237 (same knob,
  deliberately, so two paths that place the same kind of CNC order cannot disagree).
* **Does the in-repo subtree have it?** **No.** Subtree `app/main.py:579` reads
  `order_type="LIMIT", price=o["ref_price"]` and subtree `app/analytics/regime_run.py` hardcodes
  `order_type="LIMIT"`. `REBALANCE_ORDER_TYPE` appears nowhere in the monorepo.
* **Also missing:** the gateway's `ref_price` journal entry (`0a596f4`). Under MARKET there is no
  `price` in `params`, so without it the journal holds nothing to measure the fill against and
  slippage becomes unrecoverable after the fact. `baskfy_execution/gateway.py` journals
  `{"event": "placed", "order_id": oid, **params}` with no `ref` fallback.

### 3.3 Fit the plan to available margin — `fit_to_funds`

* **Commit:** `0a596f4` (same)
* **Why it exists:** a plan is sized from capital (book value + cash); margin is a different
  number. When they disagreed the batch went out anyway, `/execute` sends largest first, the money
  runs out partway down the queue, and **whichever names happen to sort last are refused** — a
  portfolio decided by sort order. On 25 Aug a Rs 19,593 shortfall across four ~Rs 6.6 lakh buys
  would have cost a whole position.
* **What it does:** 91 new lines in `app/rebalance.py`. `fit_to_funds()` takes shares off the
  **lowest-scoring buys** until the batch fits, holding back `FUNDING_HEADROOM_PCT` (default 0.5%)
  on top. Sells are never touched. It runs at **analyse** time and never after: trimming behind a
  confirmation would place something other than what was agreed to. A buy trimmed below
  `MIN_TRADE_VALUE` is dropped whole and marked `HOLD`. Called from `main.py` under
  `if C.FIT_PLAN_TO_FUNDS and plan["funding"].get("checked")`, followed by a re-check against
  Kite's own margin answer.
* **Does the in-repo subtree have it?** **No.** `fit_to_funds`, `FIT_PLAN_TO_FUNDS` and
  `FUNDING_HEADROOM_PCT` return zero hits across `kite-momentum-rebalancer/` and
  `decile-blueprint/packages/`. Subtree `main.py` imports `from .rebalance import build_plan`
  only. `tests/test_funding_fit.py` (266 lines) does not exist in the subtree.

### 3.4 Daily loss cap measured against NAV, not invested — the kill switch was wrong in both directions

* **Commit:** `5d1ff3b` "Measure the daily loss cap against NAV, not against invested" (25 Aug 2026)
* **Why it exists:** the kill switch fired at 12:44 on 25 Aug for a **Rs 24,29,636 loss that did
  not happen**. It compared the market value of the *stock* against the stock value at the last
  close. That quantity moves for two unrelated reasons — prices, and capital crossing between
  stock and cash — and the second one *is what a rebalance is*. The desk's own 12:04 batch sold
  seven positions; cash went from Rs 2,69,904 to Rs 26,44,575, the book fell Rs 24,29,636, and the
  cap read the sale as the loss and killed the four buys still owed. The day's real change was
  Rs -54,965.
* **The dangerous half is the other sign:** a day that deploys cash *raises* book value, so the
  old figure went **positive and a genuine loss was masked** — the switch was blindest on the days
  of heaviest turnover, which are the days it exists for.
* **What it does:** `pnl = (book_value + cash) - previous_snapshot_nav - today's_recorded_cashflows`.
  NAV is invariant to moving money between stock and cash. Recorded cashflows are netted out
  because a deposit is not a profit (an *unrecorded* transfer still distorts it, and nothing there
  can know it happened). `RiskManager.on_pnl` gains a `basis` argument so the kill message names
  the snapshot it measured against — the baseline is not always yesterday, because a missed
  session cannot be backfilled and a multi-day move can arrive looking like one day's.
* **Does the in-repo subtree have it?** **No, on both halves.**
  Subtree `app/main.py:540-542`:
  ```python
  prev_invested = float(rows[-1]["invested"]) if rows else 0.0
  if prev_invested > 0:
      _risk.on_pnl(float(plan.get("book_value") or 0.0) - prev_invested)
  ```
  That is verbatim the buggy version. And
  `decile-blueprint/packages/execution/src/baskfy_execution/risk.py:39` is `def on_pnl(self, pnl:
  float):` with no `basis` — the *only* difference between that file and external HEAD's
  `app/core/risk.py`.

### 3.5 Risk ceilings are un-editable — a subtree-ahead safety change the LIVE desk does not have

This one runs the other way, and it is the reason the recommendation is not simply "use external".

* **Origin:** merge module M4 / `docs/03` §3f / `DECISIONS-MERGE.md` M4.1 — *"a ceiling a user can
  raise is not a ceiling."*
* **What the subtree has:** `app/analytics/settings.py` moves `RISK_MAX_DAILY_LOSS_PCT`,
  `RISK_POSITION_HEADROOM`, `RISK_GROSS_MULTIPLE`, `RISK_MAX_ORDERS_PER_DAY`,
  `RISK_MAX_POSITION_VALUE` and `RISK_MAX_GROSS_EXPOSURE` into the read-only set;
  `app/templates/settings.html` displays them instead of offering a form; `app/main.py` gains
  `_log_risk_ceilings()`, which writes the effective values to the log at every boot so
  `journalctl` still answers "what were the limits on the day of that trade?" now that the
  `settings_audit` row is gone.
* **The external live desk still lets an operator raise its own kill-switch threshold, position
  cap, gross-exposure multiple and daily order cap from a web form.** That is a genuine security
  regression *in the live desk relative to the merged product*, and it is out of this leaf's scope
  to fix — flagged here so nobody "resolves" the divergence by copying external's `settings.py`
  over the subtree's.

### 3.6 CSRF: the desk's own hardening header refused its own forms

* **Commit:** `1cb5cb5` "Stop the desk's own hardening header from refusing its own forms" (25 Aug 2026)
* **Why it exists:** `Referrer-Policy: no-referrer` in the Caddyfile strips `Referer` and, per the
  Fetch spec, also makes the browser send `Origin: null` on any non-CORS POST — which is what a
  plain `<form method=post>` is. Both signals `core/websec.py` checks were erased by the desk's
  own security header. **Every plain form on the site was refused as forgery from the day TLS went
  up:** `/stops/arm`, `/settings`, `/settings/reset`, `/ops/run`, `/options/run`, both
  `/tradebook` posts. `/analyze` and `/execute` kept working for four days and hid it, because
  `fetch()` defaults to mode `cors` and sends a real Origin regardless of referrer policy.
* **Order-path relevance:** `/stops/arm` is how GTT stops are armed — non-negotiable #4 says every
  buy gets a GTT stop the same session. A CSRF layer that refuses that form silently is a risk
  control that does not run.
* **What it does:** accepts `Sec-Fetch-Site: same-origin` (a forbidden header name, so no script
  can set it and no referrer policy can erase it); stops treating `Origin: null` as an origin to
  check (an opaque origin is the absence of a claim, not a claim); names the likely proxy cause in
  the 403 body. The Caddyfile is changed to `Referrer-Policy same-origin` on the other side.
* **Does the in-repo subtree have it?** **No, on both sides.** Subtree `app/core/websec.py` is the
  pre-fix version, and subtree `deploy/caddy/Caddyfile` still says `Referrer-Policy no-referrer`.
  **Deploying the subtree as-is behind its own Caddyfile reproduces the outage exactly.**

### 3.7 Not a behavioural delta — four module moves verified as moves

Recorded so no port leaf spends time on them:

* `baskfy_execution/guards.py` — **byte-identical** to external HEAD `app/core/guards.py`. `diff -u` empty.
* `baskfy_execution/ratelimit.py` — **byte-identical** to external HEAD `app/core/ratelimit.py`. `diff -u` empty.
* `baskfy_core/exposure/regime.py` — **byte-identical** to external `app/core/regime.py` (0 changed lines).
* `baskfy_core/costs.py` — 74 changed lines vs external `app/costs.py`, **all of them line-wrapping
  and type annotations**. `STT_DELIVERY`, `SEBI_TURNOVER`, `STAMP_DUTY_BUY`, `GST`,
  `BROKERAGE_DELIVERY`, `DP_CHARGE_PER_SELL` and every arithmetic expression are identical. The
  cost model is numerically unchanged, so the no-trade band and `MAX_TRADE_COST_PCT` behave the same.

The untouchable-instrument guard, the overnight-option block, the rate limiter and the cost model
therefore need **no** reconciliation. That is four of the seven non-negotiables provably intact
across the fork.

### 3.8 Divergence outside `app/` that a port leaf will trip over

| File | Direction | Note |
|---|---|---|
| `deploy/caddy/Caddyfile` | external-ahead | `Referrer-Policy same-origin` vs subtree's `no-referrer` (§3.6) |
| `.env.example` | forked | External-only keys: `MARKET_PROTECTION`, `REBALANCE_ORDER_TYPE`, `FIT_PLAN_TO_FUNDS`, `FUNDING_HEADROOM_PCT`. Subtree-only keys: `BASKFY_SOLE_USER_ID`, `BASKFY_SOLE_BROKER_ACCOUNT_ID`, `DESK_DB_BACKEND`, `DESK_DATABASE_URL`, `DESK_DB_SCHEMA`, `DESK_UNIVERSE_SLUG`, `KITE_TOKEN_ENCRYPTION_KEY`, `SCAN_SOURCE_DEFAULT`, `SCREENER_DATABASE_URL`, `METRICS_PORT`, `OTEL_*`, `SENTRY_DSN` |
| `tests/test_funding_fit.py` | external-only | 266 lines, absent from the subtree. It is the executable spec for §3.3 and §3.1 |
| `tests/test_execute_gateway.py` | forked | External side carries the assertions for §3.1/§3.2/§3.4 |
| `tests/test_websec.py` | forked | External side carries the assertions for §3.6 |

---

## 4. Recommendation — which copy the port leaves read from

### 4.1 The rule

> **Structure from the subtree. The 25 Aug behaviours from external. Never a wholesale copy in
> either direction.**

The subtree is the *shape* of the product: `packages/core` and `packages/execution` hold the real
implementations, the desk's `app/core/*.py` are shims, the options lab is in `frozen/`, and the
gateway enforces P4.3 tenancy. Copying external files over that would undo M6, M15, M16 and P4.3
in one move.

The external repo is the *behaviour* of the live desk: it is the only copy that has been through a
real trading session since 21 Aug, and all four of its post-fork commits were written the day a
production incident exposed a defect. Reading the subtree for order-path behaviour ports code that
was already superseded by an incident.

### 4.2 Exactly which file to read for what

**Read `/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` at `1cb5cb5` for the
*semantics* of these ten files, and land those semantics in the merged packages:**

| Read from external | Land in | Carrying |
|---|---|---|
| `app/core/gateway.py` | `decile-blueprint/packages/execution/src/baskfy_execution/gateway.py` | market-protection band (§3.1), `ref_price` journalling (§3.2), `order_type` in the dry-run journal record |
| `app/core/risk.py` | `.../baskfy_execution/risk.py` | `on_pnl(pnl, basis="")` (§3.4) |
| `app/core/websec.py` | `kite-momentum-rebalancer/app/core/websec.py` (still a real module) | `Sec-Fetch-Site` + `Origin: null` (§3.6) |
| `app/rebalance.py` → `fit_to_funds` | `decile-blueprint/packages/core/src/baskfy_core/basket.py` (pure — it is arithmetic on the plan) with the desk binding in `app/rebalance.py` | §3.3 |
| `app/config.py` | `kite-momentum-rebalancer/app/config.py` | `MARKET_PROTECTION`, `REBALANCE_ORDER_TYPE`, `FIT_PLAN_TO_FUNDS`, `FUNDING_HEADROOM_PCT` — additive, no subtree key is touched |
| `app/main.py` | `kite-momentum-rebalancer/app/main.py` | the NAV-based loss-cap block, the `fit_to_funds` call at analyse time, `order_type=C.REBALANCE_ORDER_TYPE` — **hunks, not the file** |
| `app/kite_client.py` | `kite-momentum-rebalancer/app/kite_client.py` | the band on the no-limit-price branch only — **do not touch `_load_token`/`exchange_token`**, the subtree's encrypted store is ahead |
| `app/analytics/regime_run.py` | subtree same path | `order_type=C.REBALANCE_ORDER_TYPE` |
| `app/analytics/plan_store.py` | subtree same path | comment only; cosmetic, take it with the rest |
| `app/templates/index.html` | subtree same path | funding-fit / order-type UI |
| `deploy/caddy/Caddyfile`, `tests/test_funding_fit.py`, `tests/test_execute_gateway.py`, `tests/test_websec.py`, `.env.example` | subtree same paths | §3.8 |

**Read the in-repo subtree (and `decile-blueprint/packages/*`) as authoritative for the other 48
entries.** Specifically, never copy from external:

* `app/core/guards.py`, `app/core/ratelimit.py`, `app/core/regime.py`, `app/core/regime_alloc.py`,
  `app/costs.py`, `app/scoring.py` — these are M15/M16 shims and the real code is in the packages,
  verified equivalent in §3.7.
* `app/analytics/settings.py`, `app/templates/settings.html` — external is *less* safe (§3.5).
* `app/analytics/db.py`, `app/analytics/ops.py`, `app/analytics/autorun.py`,
  `app/analytics/regime_view.py` — Postgres and the M6 freeze.
* Any of the 23 options/strangle files — they are frozen under
  `frozen/strangle/kite-momentum-rebalancer/`, byte-identical, and `CLAUDE.md` D4 puts them out of
  every gate. Resurrecting one into `app/` reverses M6.

### 4.3 Ordering constraint, non-negotiable

**§3.1 and §3.2 must land in the same change.** Porting `REBALANCE_ORDER_TYPE=MARKET` without
`market_protection` produces a desk whose every order is refused by Kite at the API boundary —
which is precisely the incident `be93f38` was written to fix, re-created in the merged product.
Porting `market_protection` without the MARKET switch is harmless (the branch never fires), so if
they must be split, the band goes first.

### 4.4 One thing this leaf did not settle

The live desk's `/settings` page can still raise its own risk ceilings (§3.5). Bringing the M4
hardening back to the external repo is a change to the live desk, not to this monorepo, and is
outside this leaf's read-only contract. Recorded here as an open item for the desk-retirement
tree's owner.

---

## 5. Reproducing every measurement in this document

```bash
EXT=/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer
SUB=/Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer

# 58
diff -rq "$EXT/app" "$SUB/app" 2>/dev/null | grep -v __pycache__ | wc -l

# the fork point is byte-exact
git -C "$EXT" rev-parse df6cb72:app
git -C /Users/maulikdave/Documents/projects/baskfy rev-parse f1bcbb8:kite-momentum-rebalancer/app

# the four external commits and the ten app/ files they touch
git -C "$EXT" log --oneline df6cb72..HEAD
git -C "$EXT" diff --name-status df6cb72..HEAD

# the 54 subtree-side app/ changes
git -C /Users/maulikdave/Documents/projects/baskfy diff --name-status \
    f1bcbb8..HEAD -- kite-momentum-rebalancer/app

# the protection band exists nowhere in the monorepo
grep -rIl market_protection --exclude-dir=node_modules --exclude-dir=.git \
     --exclude-dir=__pycache__ --exclude-dir=.venv \
     /Users/maulikdave/Documents/projects/baskfy | wc -l      # -> 0

# guards and ratelimit moved without changing
diff -u <(git -C "$EXT" show HEAD:app/core/guards.py) \
  /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/execution/src/baskfy_execution/guards.py
diff -u <(git -C "$EXT" show HEAD:app/core/ratelimit.py) \
  /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/execution/src/baskfy_execution/ratelimit.py
```
