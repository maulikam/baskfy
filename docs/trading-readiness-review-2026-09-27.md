# Trading readiness review — 27 September 2026

Scope: repository review at `3eea54a`, focused on the Tight (TWT), Swing and Volume (VBT) screens and their execution paths. **Updated 27 Sep 2026 (evening)** after Maulik restated the requirement: the sections *The requirement, restated*, *Structural finding* and *Gaps against "live at any login time"* are new, the delivery order is rewritten around them, and every claim in them was re-checked against the tree (see *Verification boundaries*). The P0–P2 sections below them stand as written. This is a proposed backlog, not a change to trading policy. No live services, broker account, credentials, production database or deployed flags were inspected or changed. Code presence is distinguished from verified operation.

## Assessment

Baskfy has substantial screening, execution, stop management and backtest code. The missing product is a reliably connected, observable trade lifecycle: discover → validate now → allocate account risk → submit → reconcile actual fills → protect → manage → exit → measure. Some of these connections are missing even though their component functions exist.

The login hour is not the point (Maulik, 27 Sep 2026: *"don't stick to the timing. Stick to the live thing."*). The point is that TWT is designed to enter near the open, VBT scans closed sessions and requires confirmation, and Swing can scan intraday but its already-running monitor does not reload the watchlist — so at *any* hour, two of the three screens can only ever show last night's close and none of the three reacts to a login as an event. A live price overlay does not change any of those facts.

The first work should be execution correctness and live-state visibility. Adding more signals or pyramiding before completing fill reconciliation would compound the existing gaps.

## The requirement, restated (27 Sep 2026)

Maulik's words, paraphrased only where marked: he wakes, logs in to Baskfy, connects Kite, presses Scan on the Tight, Swing or Volume screen, and takes the resulting trades by hand in Kite. They are profitable, but the system "is currently just giving me stock suggestions". Prices on the screens are yesterday's because the job is nightly. What he wants: **everything live from the moment he connects — whether that is 09:00 before the open or the middle of the session — not based on the previous day's data**; a suggestion per stock with stop loss and target built in; the system taking the trade itself; pyramiding into a position that is working (and re-entry if allowed); all of it backtested and profitable. Explicitly: *"I'm not suggesting I'll log in at 10:30 or 11 … Wherever I log in … we should have everything live."*

So the bar this review is measured against is **live at any login time**, not a better cron.

## Structural finding

**Only one of the three screens has a live path at all.** Swing's *Scan now* (`services/worker/.../tasks/swing_scan_now.py`, SW15) builds a provisional bar per liquid name from a Kite quote — open/high/low from the quote's `ohlc`, close = `last_price`, volume so far — appends it to the published bars and re-runs the detectors over "today so far" from 09:15 onwards. That is the live design being asked for. **Tight and Volume refuse it by design:** `tasks/twt_scan.py` re-detects "a session that has already been **published**" and its docstring states that a provisional bar built from a live quote is not an input; `tasks/vbt_rescan.py` re-runs the same closed-session detector. On `/twt` and `/vbt`, therefore, Scan can never produce anything newer than the last published close, whatever the hour. This is a recorded decision in the code (DECISIONS-TW TW4.3 and the `twt_scan` docstring), not a scheduling defect, and reversing it means a new strategy variant, not a flag.

Two more facts frame everything below:

- **There is no intraday bar store for equities.** Minute bars exist only for the options collector's index levels (`baskfy_worker/options/index_bars.py`, `baskfy_core/options/bars.py`). Every equity strategy reads `ohlcv_daily`, written by the 18:15 bhavcopy → 18:45 nightly → 21:00–21:20 detect chain. Nothing exists to run a live equity scan on continuously, and nothing exists to *backtest* an intraday variant on.
- **Every execution switch defaults off on the box.** `infra/docker/compose.prod.yml` defaults `BASKFY_DRY_RUN=true`, `BASKFY_DESK_DRY_RUN=true`, `BASKFY_SWING/VBT/TWT_EXECUTION_ENABLED=false`, `BASKFY_SWING/TWT_AUTO_EXECUTE=false`, `BASKFY_SWING_MONITOR_ENABLED=false` (TW18 records `BASKFY_TWT_AUTO_EXECUTE` set true in `.env.staging.compose`; the others were not checked on the box). And `live_prices.quotes_permitted()` returns `False` whenever `DRY_RUN` is on (`services/api/src/baskfy_api/live_prices.py:174`), so **the same switch that keeps execution in rehearsal also removes every live price from every screen**. If the API container still runs with `BASKFY_DRY_RUN=true`, that alone explains "all the stocks are outdated, showing yesterday's price". This is the first thing to check on the box, before any code.

## Gaps against "live at any login time"

Numbered for reference; the P0–P2 sections that follow carry the file:line evidence and acceptance criteria where they already existed.

**A. Data is end-of-day by construction**

1. No intraday equity bars (above). Consequence: no continuous live scan for any sleeve, and no data to validate one.
2. The only live equity feed the web app has is Kite `/quote` through `quotes_permitted()`, which is gated on `DRY_RUN` (above). Market-data permission and order permission are one switch and must become two (P1.1).
3. `KiteTicker` is used by the Swing monitor alone (`kite-momentum-rebalancer/app/core/ticker.py`), subscribed to a watchlist read once at process start (`app/swing_monitor.py:1109`) and never reloaded (P1.2). No streaming feed serves the screens, the portfolio marks, or the TWT/VBT sleeves; the ticker registers `on_ticks`/`on_connect` only, no order-update callback.

**B. Signals are end-of-day for two of three strategies**

4. TWT and VBT have no "today so far" detection mode (Structural finding). Their *tested* entries are "next session's open, at market" (TWT, `04` §5.1) and "limit at the signal close" (VBT). Entering at a mid-session LTP is a different, untested strategy (P2.2) — it cannot be delivered by relabelling the EOD scan.
5. Swing's provisional scan is fire-and-forget: it writes `provisional=True` rows that the nightly upsert replaces, does not push new names into the running monitor (P1.2), and nothing re-runs it on an interval while the session is open. There is no "rescan every N minutes until close" loop for any sleeve; `swing-scan-sweep` / `vbt-rescan-sweep` / `twt-scan-publish` (every 60 s in `celery_app.py`) publish queued runs, they do not initiate them.

**C. Execution is clock-bound and not closed-loop**

6. TWT auto-execute is confined to 09:15–09:35 because it confirms only the 09:05 MORNING plan, which expires at 09:35 (`scripts/twt_auto_loop.py:8–11`, `app/twt_auto.py:37–40`); nothing rebuilds a plan from current prices at a later login. VBT has no auto-execute flag by rule (`app/config.py:185`). Swing's auto-drain is the only path that can enter any time 09:15–15:30 (SW26), and only when its monitor is running.
7. No live fill reconciliation for TWT or VBT: `twt_execute.on_order_update` has no caller in `app/` or `scripts/` (only Swing's handler is wired, `app/swing_desk.py:1686`, `app/swing_execute.py:1191`); VBT has no handler and `_sell_at_open` books the fill at the reference price before the broker fills. Until this exists, "take the trade itself" means holding shares the sleeve has not recorded or protected (P0.1).
8. No account-wide live state: `RiskManager(cfg)` is built per process without persistent state (`app/main.py:206`) and the desk, Swing monitor and twt-auto are separate processes; there is no continuous P&L/holdings/margin feed; a manual Kite trade is invisible until `capture-kite-trades` at 15:50 (P0.2).
9. Stops are per-order GTTs with no live supervision: a GTT trigger is not checked for a fill, and no loop watches an open position on ticks and moves its stop (P1.4).

**D. Login is a schedule assumption, not an event**

10. Kite tokens expire at 06:00, so login is daily; the system reacts to a login only for Swing (`SWING_SCAN_AFTER_LOGIN_TASK` in `routers/brokers.py:81`). Everything else assumes the token was present at its cron minute — `swing-premarket-levels` 08:50, `twt-morning`/`vbt-morning` 09:00–09:05, `twt-auto` 09:15, `swing-catalyst` 09:17 — and silently produces nothing if it was not. A login at 09:00 or 13:00 should *be* the trigger that starts quotes, scans, plans, the monitor and reconciliation; today it is not.

**E. Requested behaviours that current rules forbid**

11. Pyramiding / re-entry: every planner and both executors reject `ALREADY_HELD` ("never averages down") — `swing/plan.py:353`, `twt/plan.py:375`, `vbt/plan.py:226`, `app/twt_execute.py:601`, `app/vbt_execute.py:269` (P2.1).
12. A built-in target on every suggestion: stops exist for all three; fixed targets were tested and rejected for TWT and VBT (P2.3), so a target is a new variant, not a default.
13. Profitability of the *live* system is unproven: the archived backtests cover EOD entries only, and Swing's backtest has no intraday data (P2.2).

## What exists today

| Capability | Repository evidence | Practical limit |
|---|---|---|
| Live screen prices | `services/api/.../live_prices.py`; `routers/meta.py:get_live_marks`; web `lib/screens/live-marks.ts` and `components/screens/live-price.tsx` | 30-second browser polling, 20-second API cache, market/session gates. Prices can fall back to close. This is not a streaming scanner. |
| Swing intraday scan | Worker `tasks/swing_scan_now.py`; `celery_tasks.py:swing_scan_after_login_task`; `tasks/swing_intraday.py` | Login/Scan now can detect provisional setups and rebuild a plan. Daily ranks and historical bars remain separate. |
| Swing automatic entries | Desk `app/swing_monitor.py:drain_auto_execute` | Requires the monitor and execution flags, a session, capital, gates and an eligible watched trigger. Watches until 15:30; not a universal auto-confirm for every plan. |
| TWT automatic entries | Desk `app/twt_auto.py`; `scripts/twt_auto_loop.py` | Today's MORNING plan only; 09:05 build, 30-minute expiry, 09:15 launch and a bounded session wait. No 10–11 am catch-up path. |
| VBT execution | Desk `app/vbt_execute.py`, `app/vbt_desk.py` | Explicit confirmation; no VBT auto-execute flag. Limit entry at the signal close. |
| Stops and exits | Core `swing/stops.py`, `twt/exits.py`, `vbt/exits.py`; desk execute modules; execution gateway GTT methods | Rule calculation and dry-run support do not prove actual live fills receive and retain protection. |
| Backtests | Core and worker backtests for all three sleeves; `research/tight-close/STRATEGY.md`, `research/volume-breakout/STRATEGY.md` | Existing results apply to their tested timing, sizing and exit rules. They do not validate new intraday or pyramiding variants. |
| Position sizing, gates, idempotency | Core sleeve planners; execution gateway, risk and client IDs | Useful foundations, but account-wide risk state is not coordinated across the separate execution processes. |

Paths abbreviated `services/...`, `packages/...` and web above are inside `decile-blueprint/`; desk paths are inside `kite-momentum-rebalancer/`.

## Prioritized gaps and acceptance criteria

### P0.1 — Complete live fill reconciliation and same-fill stop protection

**Confirmed code gap, highest priority.** TWT `_buy_at_open` records a real placement as `SENT` and returns. Only the dry-run branch directly calls `_apply_fill`, which creates the position, fill record and GTT. `twt_execute.on_order_update` provides the live counterpart, but the production Python call-site search found no caller. It also ignores any status other than `COMPLETE`, so partially filled orders need explicit protection handling. `twt_auto.drain_plan` submits lines but does not reconcile them afterward.

VBT similarly returns `SENT` for real buys; its position/fill/stop helper is `_simulate_fill`. No production VBT fill handler was found in the reviewed paths. Swing does have initial polling, an idempotent update handler, a manual reconcile route and cutoff reconciliation. Its ticker registers tick/connect callbacks, not order updates; continuous post-submission fill reconciliation is not wired there.

There is also an exit-side defect: VBT `_sell_at_open` records a sell fill at the reference price and reduces/closes the position immediately after an accepted placement, including on the real path, while marking the plan line `SENT`. It has not established the broker's filled quantity or actual average price at that point. A delayed/rejected/partial sell can therefore make the internal book disagree with the broker (`app/vbt_execute.py:474`). Entry and exit reconciliation must be repaired together.

**Consequence:** a broker can hold shares that the sleeve has not recorded or protected. A successful order submission and a successful dry-run test are insufficient evidence of a protected live trade. This review does not establish whether any actual account position is currently affected.

**Build:** a durable desk-owned order reconciliation service using broker updates plus periodic order-book reconciliation and restart recovery. Apply cumulative fill deltas idempotently; protect each filled quantity, resize the existing stop as more shares fill, reconcile exits/GTT-triggered orders, and surface rejected/expired protection. Block additional entries when protection is unresolved. Keep all broker mutations through the gateway.

**Acceptance:** simulated broker tests covering partial → complete, partial → cancel, duplicate/out-of-order updates, a fill after timeout, restart after broker acceptance, GTT rejection and manual broker exits. Verify actual recorded fill quantity equals protected quantity; zero duplicate buys/stops. Test with the live-shaped asynchronous path, not just the immediate dry-run branch.

Evidence: `kite-momentum-rebalancer/app/twt_execute.py:578`, `:699`, `:1389`; `app/twt_auto.py:223`; `app/vbt_execute.py:250`; `app/swing_desk.py:1670`; `app/core/ticker.py:47`.

### P0.2 — Make risk and capital truly account-wide

**Confirmed wiring gap.** `app/main.py:gateway` constructs `RiskManager(cfg)` without its available persistent state path. Swing monitor, TWT auto runner and desk run as separate processes, each with its own module globals. Sharing the manager inside the desk process does not share it across those processes.

The three sleeve buy callers pass the proposed order's value as `gross_exposure`; the risk manager compares that supplied number to the account gross cap. Its per-symbol map tracks accepted checks, not a reconciled holdings snapshot. The production `on_pnl` call found is in the weekly execute route, rather than a continuous account-P&L feed.

**Build:** one durable account risk ledger and atomic capital reservations shared across sleeves and processes. Reconcile existing holdings, pending buys/sells, manual Kite trades and broker cash; update actual intraday P&L. Apply combined symbol, sector, gross exposure, open stop-risk and loss limits. Ensure a kill switch reaches all execution processes while preserving protective actions.

**Acceptance:** simultaneous Swing and TWT entries cannot spend the same cash or bypass a combined cap; rejected orders release reservations; restarts preserve limits; an existing manual holding counts toward exposure. Persisting each process's private JSON file alone does not solve coordination.

Evidence: `app/main.py:189`; `app/swing_desk.py:1490`; `app/twt_desk.py:1027`; `app/vbt_desk.py:836`; `app/swing_execute.py:889`; `app/twt_execute.py:642`; `app/vbt_execute.py:287`; `decile-blueprint/packages/execution/src/baskfy_execution/risk.py:120`.

### P1.1 — Make the data clocks explicit and test the live overlay end to end

**Partly built; operational cause unverified.** The four screen surfaces already use the live-mark overlay. During a valid session, yesterday's displayed price therefore deserves investigation, not an assumption that all quote support is absent. The published `as_of`, daily factors and completed weekly patterns should still refer to the last completed session.

The live quote shape retains last price and previous close, but not exchange timestamp/last-trade time. `/meta/live-marks` reports the table as live if any quote returns; missing symbols fall back individually. The browser query can retain its previous successful data when a request throws, with no display age cutoff in `useLiveMarks`.

**Build:** separate labels for signal date, signal price, current LTP, exchange/receive time and last scan. Show per-row stale/missing status and coverage counts. Remove the live badge once freshness expires. Investigate deployed API version, session visibility, quote errors, market-calendar state and `quotes_permitted` (which also checks DRY_RUN). Separate market-data capability from order permission in a deliberate follow-up design.

**Acceptance:** closed market, expired token, partial quote response, old last trade, timeout, suspended tab and reconnect cannot leave stale rows looking live. Execution requires independently validated fresh data; it must not borrow the browser's fallback close.

Evidence: `services/api/src/baskfy_api/live_prices.py:124`, `:189`; `routers/meta.py:264`; web `lib/screens/live-marks.ts`; `components/screens/live-price.tsx:69`.

### P1.2 — Connect fresh Swing scans to the running monitor

**Confirmed code gap.** `swing_monitor.main` loads the watchlist once, builds the strategy and quote fallback once, and `run_until_close` creates subscriptions once. The loop has no watchlist reload. A later scan can add a database watch without adding it to that already-running process. An empty-watchlist startup takes a separate clock path and does not enter the watch loop.

**Build:** a versioned watchlist update channel or bounded refresh inside the monitor. Add/remove subscriptions and revalidate setup levels while preserving trigger/order deduplication. On late login, refresh the broker session and catch up eligible setups; explicitly skip stale or overextended entries. Provide a session supervisor with feed, scanner, monitor and fill-consumer heartbeats.

**Acceptance:** start at 09:15, add a new eligible setup at 11:00, then deliver a valid trigger: the existing monitor observes it exactly once without restart. Also exercise startup with an empty list, late authentication, reconnect and watch expiry.

Evidence: `kite-momentum-rebalancer/app/swing_monitor.py:1030`, `:1099`; worker `tasks/swing_intraday.py:131`.

### P1.3 — Define late-login behavior separately for each strategy

**Current product behavior, not merely a scheduler bug.** TWT's automatic service runs around 09:15, waits up to ten minutes for a session, and refuses missing/expired plans. The loop's latest join time is 09:35. A login at 10–11 am cannot revive that morning run.

VBT Scan now reruns a published-session detector; TWT does the same over completed-session/weekly inputs. Neither button creates an immediate live entry. Swing has a different provisional intraday scan. These distinctions should be visible beside the buttons.

**Build:** clearly show missed-window / waiting-for-login / signal-ready / plan-ready / monitoring / blocked states and their reasons. Make existing closed-session scans and plan generation run without manual clicks, with retries and visible failures. Treat a TWT late-entry variant or live-volume breakout variant as a new, versioned strategy requiring its own study. Do not simply extend an expired plan or substitute today's LTP for the tested entry price.

**Acceptance:** a 10:30 login produces an explicit, deterministic outcome per sleeve. Repeated scans cannot duplicate orders, and an EOD detector is never labelled a new intraday strategy.

External constraint: ordinary Kite access tokens expire at 06:00 the following day, so the supported daily authentication requirement remains even if scanning and management are automated. Source checked 27 September 2026: [Kite authentication documentation](https://kite.trade/docs/connect/v3/user/).

Evidence: `scripts/twt_auto_loop.py:24`; `app/twt_auto.py:88`, `:156`; worker `tasks/twt_scan.py:29`; `tasks/vbt_rescan.py:11`.

### P1.4 — Finish unattended position management, not just unattended entry

**Existing rules, incomplete automation coverage.** Swing manages partial profit-taking, breakeven and moving-average exits. Its auto-drain confirms fresh triggered entry plans; that does not automatically drain all EOD sell/stop plans. TWT auto confirms morning stop-arm/raise lines as well as buys, but its separate naked-position sweep is deliberately not scheduled. VBT's exits and cancellations remain confirmed desk actions.

**Build:** one visible lifecycle per trade: filled quantity, stop status, stop quantity, exit condition, next action and any overdue management. Reconcile actual GTT status/order results, not merely an ID in the database. Provide an explicit workflow to adopt a manually bought Kite holding into a strategy, including cost, quantity and existing stops, instead of silently claiming ownership of unrelated holdings.

Any wider automatic exit/repair policy must be recorded as a strategy-policy change; this review does not enable it. The existing web/desk separation can remain: the web observes and links to the authenticated desk; execution remains in the desk/gateway.

**Acceptance:** a trade can be followed from entry to confirmed exit with no orphan position, over-sized residual GTT or unexplained discrepancy after a manual broker action. A stop-triggered but unfilled/rejected limit order remains an unresolved position.

Kite GTT creates a LIMIT order; a trigger firing is not proof of a fill. The API explicitly exposes trigger status and order results. Source: [Kite GTT documentation](https://kite.trade/docs/connect/v3/gtt/).

### P2.1 — Add pyramiding as a new tested position-management model

**Absent by deliberate rule.** All three planners reject `ALREADY_HELD`; TWT and VBT also refuse held names at execution. This blocks additions to winners as well as averaging down. Removing that guard alone would bypass a position model built for one entry.

**Research/build:** versioned add-on rules for a profitable position, a fresh qualifying continuation signal, maximum additions, minimum spacing, per-addition sizing, combined symbol/sector exposure and total remaining stop-risk. Persist each entry leg, reconcile partial fills, recalculate blended cost, and resize protection for the total position. Stops must not loosen merely to fit an addition. Define re-entry after a completed exit separately from pyramiding an open position.

**Acceptance:** compare baseline against pyramiding on untouched periods after costs, drawdown and concentration; stress gap losses and reversals. Reject adds without sufficient risk/cash capacity. Test duplicate signals and concurrent adds. No numerical thresholds are approved by this review.

Evidence: core `swing/plan.py:352`, `twt/plan.py:375`, `vbt/plan.py:226`; desk `twt_execute.py:597`, `vbt_execute.py:268`.

### P2.2 — Validate the exact strategy that will run live

**Backtests exist; profitability of the requested system is unproven.** TWT research reports 20.9% CAGR, approximately 24.7% maximum drawdown and 164 trades; VBT research reports 18.2% CAGR, approximately 27.9% maximum drawdown and 761 trades. These are archived research results, not freshly reproduced results or a forecast. Both research files disclose costs, modelled fills and history limitations; TWT notes ten trades contributed 53% of gross profit.

Swing's backtest explicitly says it has no intraday data/ORH filter and incomplete circuit history. It therefore cannot validate the exact timing and fills of a live opening-range or 11 am provisional scan strategy. Corporate-action and point-in-time coverage must be audited for each actual data loader; historical membership/delisting flags alone do not establish a complete historical universe.

**Build:** reproducible data/config/strategy versions; baseline reproduction; chronological train/validation/untouched-test periods; walk-forward and parameter sensitivity; minute-data replay for intraday rules; realistic costs, spread, gap, partial/no-fill and circuit behaviour. Keep EOD and intraday results distinct. Compare like-for-like benchmarks and combined account equity, and log every rejected/missed signal so paper/live divergence can be explained.

**Acceptance:** report net expectancy, drawdown, exposure, turnover, trade count, concentration, cost sensitivity and uncertainty per strategy and account. Then collect forward paper/shadow evidence from the same runtime used for execution. Historical success cannot guarantee future profit; promotion criteria must include operational reliability as well as returns.

### P2.3 — Make targets strategy-specific

TWT currently uses a 20% initial stop and 20% high-water trailing rule. VBT uses a 12% initial stop and a close below its 21-day EMA for the working exit. Both deliberately omit fixed targets; their research says tested target/partial variants reduced returns. Swing already has partial profit-taking and trailing rules.

Show entry, quantity, rupee risk, initial/current stop and the exact profit-exit rule on each trade card. A fixed target may be a separately tested variant; inventing one for every momentum trade would change the strategy being sold as backtested.

## Recommended delivery order (rewritten for "live at any login time")

1. **Separate market-data permission from `DRY_RUN`** (gap 2; P1.1). One change, unblocks live prices on every screen and Swing's provisional scan while execution stays in rehearsal. Check the API container's `BASKFY_DRY_RUN` on the box first.
2. **Login as the event** (gap 10). On token arrival, whatever the hour, a session supervisor starts: quote/tick ingestion, an interval rescan per sleeve, the Swing monitor with a reloadable watchlist (gaps 3, 5; P1.2), plan builds, and the order-update consumer. Cron becomes a fallback, not the trigger. Per-sleeve state visible beside each Scan button (P1.3).
3. **Reconcile fills and coordinate risk** (gaps 7–9; P0.1, P0.2, P1.4). The precondition for any sleeve taking its own trade; without it, flipping the execution flags is unsafe.
4. **Capture intraday equity bars** (gap 1) for the liquid universe, so live TWT/VBT variants can be *defined and backtested* rather than guessed.
5. **Then the new strategies, each versioned and backtested on the same runtime** (gaps 4, 11–13; P2.1–P2.3): live-entry TWT and VBT, pyramiding/re-entry, targets. Promote only on their own evidence.

The intended experience at any login: connect once → every screen is live → each sleeve shows ready / monitoring / blocked and why → the desk monitors eligible setups → authorised rules submit entries → every fill gets a verified stop → management runs and reports exceptions → every action is attributable to a strategy version. None of it depends on a page being open or on the hour of login. The daily Kite login itself remains, by Kite's token rules.

## Verification boundaries

This review traced source, schedules, deployed-compose definitions, tests and research notes. The 27 Sep evening update re-checked, by grep against the tree at `3eea54a`: the `on_order_update` call sites, the `SENT` returns and `_apply_fill`/`_simulate_fill` callers in `twt_execute.py`/`vbt_execute.py`, `RiskManager(` construction, `ticker.py` callbacks, `ALREADY_HELD` in the three planners and two executors, the `twt_auto` window, `swing_monitor.py` watchlist loading, the compose defaults, `quotes_permitted()`, the Beat schedule in `celery_app.py`, the `twt_scan`/`swing_scan_now` docstrings, and the absence of any equity minute-bar module. No code was changed. It did not verify running AWS containers, live flags, current broker holdings, protected quantities, fresh quote responses or the latest persisted backtest results. Those remain a separate read-only operational audit. No execution settings were changed.

The full pipeline/database/browser suites and historical backtests were not rerun for this documentation-only review.

The initial desk run, with dotenv disabled, DRY_RUN enabled and empty broker credentials, produced **2,432 passed, 147 skipped, 4 failed** (12 additional subtests passed). Three failures were test-environment assumptions: two constructor tests require a nonempty API-key string despite mocking the client; one secrecy assertion tests that the secret is absent from HTML and fails for an empty string. A targeted rerun with explicit non-secret dummy strings resolved those three. The remaining test asserts `FULLY_INVESTED=True`, whereas `app/config.py:110` defaults it to false. This is a test/configuration mismatch, not evidence to change a trading setting. No source or test assertion was changed. A second full run uses those dummy strings and an explicit process-only `FULLY_INVESTED=true` to exercise the profile that test requires; its result follows below. No real credentials or existing token store were used.

## Status, 28 Sep 2026 — what each item became (LV pack, `docs/live/PLAN.md`)

Written by the agent that executed this review under `/unlazy`; every claim below is a commit and
a gate (`gates/live-*.md`), not a plan. `docs/live/DECISIONS-LV.md` carries the judgement calls.

| Item | Became | Where |
|---|---|---|
| "Check the API container's `BASKFY_DRY_RUN` first" | Read on the box: **already false**; every sleeve book empty; TWT's six live buys of 23–24 Sep rejected by Zerodha for missing `market_protection` | LV0, `docs/live/AUDIT-2026-09-27.md` |
| Gap 2 / P1.1 — market data off `DRY_RUN`; freshness end to end | `BASKFY_LIVE_QUOTES` is the market-data switch; `as_of`/`stale` per quote, `served_at`, `requested`/`covered`; browser drops the overlay after 90 s or a failed fetch; stale rows muted | LV1 |
| Gap 7 / P0.1 — fill reconciliation and same-fill protection | `app/reconcile.py`: order book, GTT list and holdings once a pass; TWT partial fills and dead orders; VBT buys and **exits booked from the fill**; `lv_protection_issue`; every sleeve's buy refuses `PROTECTION_UNRESOLVED`; TWT market buys carry `market_protection` | LV2 |
| Gap 8 / P0.2 — account-wide risk | `RiskStateStore` — one `risk_ledger` row per day, `SELECT … FOR UPDATE`, shared by every process; `release`, `seed_positions`, kill switch across processes | LV3 |
| Gap 10 / P1.2 / P1.3 — login as the event; monitor reload; per-sleeve state | `session-supervisor` service (token arrival → seed exposure, reconcile once, every 10 s in session, heartbeats); swing monitor reloads its watchlist every minute and waits for a late login until 15:20; `GET /sleeves/state` and the chip beside each Scan button; the login queues the TWT and VBT scans too | LV4 |
| Gap 1 — no intraday equity bars | `eq_minute_bar` hypertable; 15:45 session reconcile for the ~570-name liquid universe; resumable backfill CLI; pure readers | LV5 (no live tick collector — DECISIONS-LV LV5.1) |
| P1.4 — one lifecycle per trade; adoption; P2.3 trade card | `/lifecycle` on the desk: stop judged at the broker (`ARMED`…`TRIGGERED_UNFILLED`, `UNVERIFIED`), findings, next action, overdue; `POST /lifecycle/adopt`; exit rule, initial stop and ₹ at risk on every sleeve page | LV6 |
| Gap 4 / P2.1 — live-entry TWT/VBT | **Built as Maulik decided (28 Sep, DECISIONS-LV LV8.0):** the Scan buttons read today so far from Kite quotes with the same detectors; a signal is a `LIVE` plan bought **now, at market with protection** (TWT auto-confirmed within a minute; VBT's new `BUY_AT_MARKET` by hand); rows `provisional`, replaced by the nightly. **No backtest of the live entry, by his choice.** | LV8 (TW19, VB16) |
| Gaps 11–13 / P2.2 (pyramiding, re-entry, targets, backtests of the live system) | **Not built.** Maulik's decisions; `NEEDS-MAULIK.md` LV7 Q3–Q5. LV5's minute bars are off on the box by his choice, so no intraday data accumulates for a backtest | — |
| Gap 3 / 9 — streaming feed for screens and stop supervision on ticks | **Not built.** The overlay polls (30 s), the reconciler polls (10 s); a tick-driven stop supervisor is a policy change the review does not enable | — |
