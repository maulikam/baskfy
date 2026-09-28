# Plan: LV — live at any login time (from `docs/trading-readiness-review-2026-09-27.md`)

Depth: tree 3   Mode: orchestrated-lite (one driver; leaves worked in sequence or at most two at once — the
16 GB Mac rule)   Started: Sunday 27 Sep 2026.
Budget note: the review is five delivery steps; steps 1–4 are engineering against existing rules and are
built here as LV1–LV6. Step 5 (new strategy variants, pyramiding, targets) needs Maulik's decisions and
backtests on data LV5 only starts collecting; it is **not built** in this run and is handed back as
questions with options (DECISIONS-LV LV0.1).

## Contract

Decided before fan-out. Everything a leaf could get wrong about its neighbours.

### Naming
* Module prefix **LV**; commits `LV<N>: green — …`; decisions in `docs/live/DECISIONS-LV.md` numbered
  `LV<N>.<k>`, every entry `⚠ UNREVIEWED` until Maulik reads it; gates in `gates/live-<N>-<name>.md`.
* New env: `BASKFY_LIVE_QUOTES` (API, default `"true"`, read-only market data; **not** a money flag).
  No other new flag. No default changes to any existing flag (non-negotiable 1).
* New desk tables live in the **public** schema beside `tw_*`/`vb_*` (the desk's stores prefix `public.`; the `desk`
  schema is the migrated SQLite's) and are created by **one** Alembic migration owned by LV2:
  `0055_live_desk_state.py` — `lv_protection_issue`, `lv_heartbeat`, `lv_exit_order`, `lv_adoption`, `risk_ledger`
  (amended while building LV2: VBT's pending sell needed a row of its own; DECISIONS-LV LV2.4).
  LV5 owns `0056_eq_minute_bar.py`. Nobody else adds a migration.

### Interfaces
* **LV1** `baskfy_api.live_prices`: `market_data_enabled() -> bool` (the `BASKFY_LIVE_QUOTES` read);
  `quotes_permitted()` = `market_data_enabled() and api key and a real, unexpired, non-sim token` — it no
  longer reads `DRY_RUN`. `LiveQuote` gains `as_of: dt.datetime | None`. Wire: `LiveQuoteOut` gains
  `as_of: datetime|null`, `stale: bool`; `LiveMarksOut` gains `served_at: datetime`, `requested: int`,
  `covered: int`, `stale_after_seconds: int` (120). Web `LiveMarks` gains `receivedAt`, `requested`,
  `covered`; `LiveQuote` gains `stale`, `asOf`. The overlay is dropped in the browser when the last
  successful answer is older than `LIVE_MAX_AGE_MS = 90_000` or the last fetch errored.
* **LV2** desk `app/reconcile.py`:
  `class SleeveHooks(Protocol)`: `name: str`; `open_orders(store) -> list[dict]` (rows with
  `broker_order_id`, state SENT/PARTIAL); `on_order_update(store, gateway, payload, *, now)` — payload is
  Kite's order-book row (`order_id`, `status`, `filled_quantity`, `average_price`, `transaction_type`,
  `tradingsymbol`). `reconcile_once(book, sleeves, *, now, issues) -> ReconcileRun` (counts: seen,
  applied, dead, released, issues). `class OrderBook(Protocol)`: `orders() -> list[dict]`,
  `get_gtts() -> list[dict]`, `holdings() -> list[dict]`. Issues go to `lv_protection_issue`
  (`sleeve, position_id, kind in {NAKED, GTT_MISSING, GTT_OVERSIZED, GTT_UNDERSIZED, EXTERNAL_EXIT,
  GTT_TRIGGERED_UNFILLED, STOP_REJECTED}, detail, seen_at, resolved_at`). `protection_unresolved(conn, sleeve) -> list[dict]` is
  the buy guard every sleeve's buy calls: a non-empty answer refuses with `PROTECTION_UNRESOLVED`.
  TWT `on_order_update` handles partial fills exactly as swing's does (position for the filled quantity,
  GTT resized as more fills arrive, never a second GTT). VBT gains `on_order_update` for buys and sells;
  `_sell_at_open`'s real path records the sell `SENT` and books nothing until the broker reports a fill.
  `python -m app.reconcile` runs one pass (restart recovery) and exits.
* **LV3** `baskfy_execution.risk`: `class RiskStateStore(Protocol)`: `lock()` (context manager),
  `load() -> dict | None`, `save(payload: dict) -> None`. `RiskManager(cfg, *, state_path=None,
  store=None)`; with a store, `pre_order`, `on_pnl`, `kill`, `release` reload under `lock()` before
  deciding and save after. New `release(symbol, value)` (an unfilled reservation given back) and
  `seed_positions(values: dict[str, float])` (holdings counted toward exposure). Desk
  `app/core/risk_store.py`: `PgRiskStateStore(connect)` over `desk.risk_ledger` (one row per IST day,
  `SELECT … FOR UPDATE`). `app.main.gateway()` passes it when `DB_BACKEND == "postgres"`.
* **LV4** desk `app/session_supervisor.py` (+ `scripts/session_supervisor_loop.py`, Dockerfile command
  `session-supervisor-loop`, compose service `session-supervisor`, deploy scripts' service lists).
  Heartbeats: `lv_heartbeat(process, state, detail, at)` upserted by process name; writers:
  `supervisor`, `reconciler`, `swing_monitor`, `twt_auto`. Swing monitor: `run_until_close(...,
  reload=None, reload_every_seconds=60, heartbeat=None)`; `main()` waits for a Kite session until 15:20
  instead of exiting. API `GET /sleeves/state` → `list[SleeveStateOut]` with
  `sleeve in {swing, twt, vbt}`, `state in {closed, waiting_for_login, scanning, signal_ready,
  plan_ready, monitoring, missed_window, blocked, idle}`, `reason`, `as_of`, `next`, `updated_at`.
  Web `<SleeveState sleeve=…/>` chip beside each Scan button. API login callback also queues
  `baskfy.twt.scan` and `baskfy.vbt.rescan` (closed-session, idempotent, labelled as such).
* **LV5** `eq_minute_bar(instrument_id, ts timestamptz, open, high, low, close numeric(18,2), volume
  bigint, source text)` PK `(instrument_id, ts)`; worker `baskfy_worker/eq_bars.py`:
  `reconcile_session(session, provider, day)` over the liquid universe (swing's `liquid_universe`,
  as of the last published session) and `backfill(session, provider, start, end)` resumable per
  instrument per 60-day window; Beat `baskfy.eq_bars.session` 15:45 Mon–Fri; CLI `eq_bars_cli`.
  Core `baskfy_core/eq_bars.py`: pure readings only (windows, 5-minute bars, opening range).
* **LV6** desk `app/lifecycle.py`: `TradeLifecycle` rows over the three sleeves' open positions
  (`sleeve, symbol, filled_qty, open_qty, entry_avg, initial_stop, stop, stop_state in {ARMED, NAKED,
  GTT_MISSING, GTT_OVERSIZED, TRIGGERED_UNFILLED}, stop_qty, rupee_risk, exit_rule, next_action,
  overdue, issues`), page `GET /lifecycle`, `POST /lifecycle/adopt` (`confirm=true`, sleeve, symbol,
  quantity, avg_cost, gtt_id optional) → a position in that sleeve's store + `lv_adoption` row + a stop
  armed through the gateway when no `gtt_id` is given. Each sleeve page's trade card shows entry,
  quantity, rupee risk, initial and current stop, and the exact exit rule.

### Data ownership (no two leaves touch the same file)
| Leaf | Owns |
|---|---|
| LV0 | `docs/live/AUDIT-2026-09-27.md`, `NEEDS-MAULIK.md` (append) |
| LV1 | `decile-blueprint/services/api/src/baskfy_api/live_prices.py`, `routers/meta.py`, `schemas.py` (LiveMarks* only), `apps/web/src/lib/screens/live-marks.ts`, `components/screens/live-price.tsx`, their tests, `openapi.json` + TS client regen |
| LV2 | desk `app/reconcile.py`, `app/twt_execute.py`, `app/vbt_execute.py`, `app/swing_execute.py` (guard only), stores' `open_orders`, `alembic/versions/0055_live_desk_state.py`, desk tests |
| LV3 | `packages/execution/src/baskfy_execution/risk.py`, desk `app/core/risk_store.py`, `app/main.py` (gateway wiring), tests |
| LV4 | desk `app/session_supervisor.py`, `scripts/session_supervisor_loop.py`, `app/swing_monitor.py`, `Dockerfile.desk`, `compose.prod.yml`, `tools/deploy/*.sh` service lists, API `routers/sleeves.py` + `sleeve_state.py` + `brokers.py` (queue list), web `components/screens/sleeve-state.tsx` + three page headers |
| LV5 | `alembic/versions/0056_eq_minute_bar.py`, `baskfy_core/models/eq_bars.py`, `baskfy_core/eq_bars.py`, `baskfy_worker/eq_bars.py`, `celery_app.py` (one Beat entry + route), `eq_bars_cli.py`, tests |
| LV6 | desk `app/lifecycle.py`, `templates/lifecycle.html`, sleeve templates' trade card, `app/main.py` (router include), tests |
| LV7 | `docs/00-merge-status.md`, `docs/DECISIONS-MERGE.md` (pointer), `docs/live/DECISIONS-LV.md`, `CLAUDE.md` (clock table row for the price column), deploy ledger |

LV2 and LV3 both need `app/main.py`? No: LV3 owns it (gateway wiring); LV6 adds its router include
**after** LV3 is committed. LV4's `brokers.py` edit is one tuple line.

### Conventions
* Tests assert the spec (house rule 2); no `# type: ignore`, no `Any` outside the desk's existing
  `# noqa: ANN401` idiom; ruff + ruff format + mypy strict on the screener trees; the desk suite is run
  with `.venv/bin/python -m pytest` and nothing else touches `baskfy_test` while it runs.
* Every buy path keeps: guards → risk → rate limit → journal → broker; `client_id = plan_id:symbol`.
* `DRY_RUN=true` in every environment an agent creates. No live order. No flag flipped on the box.
* Doc updates ride with the module that changed behaviour.

## Tree

- 1 LV — live at any login time .......................... gates/live-root.md
  - 1.0 LV0 box + account audit (read-only) .............. gates/live-0-audit.md
  - 1.1 LV1 market data off DRY_RUN; freshness end to end . gates/live-1-market-data.md
  - 1.2 LV2 fill reconciliation, all three sleeves ....... gates/live-2-reconcile.md
  - 1.3 LV3 account-wide risk ledger ..................... gates/live-3-risk.md
  - 1.4 LV4 login as the event; sleeve state; monitor reload gates/live-4-login-event.md
  - 1.5 LV5 intraday equity bars (store, reconcile, backfill) gates/live-5-eq-bars.md
  - 1.6 LV6 trade lifecycle, adoption, trade card ........ gates/live-6-lifecycle.md
  - 1.7 LV7 integration: suites, lint, deploy, docs, report gates/live-7-integration.md
  - 1.8 LV8 live scans for TWT and VBT; entries now at market (Maulik, 28 Sep) gates/live-8-live-scans.md

## Added 28 Sep 2026 04:00 IST — LV9 and LV10 (Maulik's Q3–Q5 answers, DECISIONS-LV LV9.0)

**LV9 — Qullamaggie exits on TWT and VBT (`gates/live-9-qulla-exits.md`).** The swing book's
`StopConfig` rule replaces TWT's 20 % high-water trail and VBT's 21-EMA exit: a third sold into
strength between bar 3 and bar 5 after entry if green, the stop to breakeven after the partial or
at +1R, the remainder trailing the 10-day MA (ADR ≥ 6 %) or the 20-day MA, sold at the next open on
a close below it; the GTT stays the hard stop. One rule module (`baskfy_core.swing.stops.manage`),
two adapters. TWT's auto-execute sends the partial `SELL_AT_OPEN` too (TW20). Contract: positions
gain `partial_done`, `trail`; TWT positions gain the queued-sell columns VBT already has; VBT gains
`RAISE_GTT_STOP`; migration 0058; `ExitConfig.qulla_exits` (default true) is the one-line reversal.

**LV10 — his pyramiding on all three sleeves (`gates/live-10-pyramiding.md`).** A fresh qualifying
setup in a held name is a new entry with its own size and stop, counted against slots and exposure;
at most `max_entries_per_name` (2) open entries per name; re-entry after an exit on a new signal.
`SizingConfig.pyramiding` (default true) per sleeve. Plan lines carry `position_id` so exits and
raises name the position they act on when a name holds two.

**Deploy:** after today's close (15:30–18:40 IST window), not into the morning session.

## Status log

Append-only.

- 2026-09-27 ~13:00 IST plan written, contract fixed; AWS SSO expired (login opened in Maulik's browser); Kite connector has no session.
- 2026-09-28 00:20 IST LV0 audit read off the box: desk live, every book empty, TWT's six live buys of 23–24 Sep rejected by Zerodha for missing market protection; token rewritten 00:04, dies 06:00.
- 2026-09-28 01:10 IST LV1 agent died mid-web (network); driver finished the web tests, docs and lint fixes. LV2 code and 21 tests written; migration 0055 test green on local Postgres.
- 2026-09-28 02:30 IST LV0 committed (84cbb71). LV2 gates 10/10, LV3 gates 6/6 met. LV1 6/7 — M6 waits on the tree-wide lint after LV4. LV4 written: /sleeves/state + chip, login queues all three scans, session-supervisor service, monitor reload + late-login wait; gate check running.
- 2026-09-28 01:20 IST LV4 gates 8/8, LV5 6/7 (lint: a duplicate test-module name, renamed), LV6 tests 21 green; web page tests fixed (the chip carries its own QueryClient). Status page, review annotation and NEEDS-MAULIK step-5 questions written. Next: lint-clean gate re-runs, six commits, deploy before the open.
- 2026-09-28 01:20 IST every leaf gate met (LV0 5, LV1 7, LV2 10, LV3 6, LV4 8, LV5 7, LV6 6). Committed LV1 00725f5, LV2 2195d88, LV3 cfccfa2, LV4 7a89488, LV5 4af83e5, LV6 ece537a. ship.sh started from a clean worktree of ece537a; the screener's full suite running beside it.
- 2026-09-28 01:35 IST Maulik, in session: TWT/VBT Scan builds a live bar like swing; entries now at market with protection (TWT auto confirms live plans; VBT by hand); LV5 off on the box, code stays. Added leaf LV8 (DECISIONS-LV LV8.0).
- 2026-09-28 02:30 IST LV8 written: live_scan.py (provisional bars, live detect, LIVE plan builders), the two Scan tasks branch on decide_session with a Kite quote source bound in Celery, migration 0057, VBT `BUY_AT_MARKET` desk handler, twt-auto accepts LIVE plans and the supervisor drains once a minute, `provisional` on the wire and the pages, SCAN_MEANS reworded. Tests: worker test_live_scan (20), migration 0057 (3), desk 87 across the three touched suites, desk suite 2,527 green, web 292 green. Tests pinning the reversed decision rewritten to cite LV8.0. TW19, VB16, LV8.1 written. Next: lint, gates V1–V9, commits LV7 + LV8, deploy.
- 2026-09-28 03:50 IST LV8 gates 9/9; committed dde3c1e; deployed from a clean worktree 02:42–02:53 IST (`✓ DEPLOYED dde3c1e`, running=16, alembic 0057_live_scans, live_quotes true, twt_execution_true=1 twt_auto_true=1, EQ_BARS off). LV7 I1 run by hand (the runner's cap is 30 min): `1 failed, 13554 passed, 19 skipped, 1 xfailed in 32:13` — the one failure pinned the pre-LV1 quote shape, fixed, its file 33 green. Supervisor docstring/compose comment reconciled with LV8's drain. Root ledger running; LV7 commit and report next.
- 2026-09-28 05:40 IST LV9 built: core `exits/qulla.py` (adapter over swing stops.manage) + tests (12); TWT/VBT ExitConfig.qulla_exits; TWT evening `run_twt_manage_qulla` replaces the ratchet (tests 7), VBT evening qulla branch + RAISE_GTT_STOP kind (tests 7); migration 0058 (3); desk: TWT `_sell_at_open` + exit-order bookkeeping + auto-execute sells, VBT `_raise_gtt_stop` + partial_done; exit_rules text; tests pinning the reversed decisions rewritten to cite LV9.0. Desk suite 2,538 green; screener sleeve suites green. Gate run done; commit next, then LV10.
- 2026-09-28 06:35 IST LV10 built: `pyramiding`/`max_entries_per_name` on the three sizing configs; the planners count entries per name (`open_entry_counts`) and refuse at the cap; the builders (twt book_state, vbt evening, live_scan, swing_eod) hand the counts; swing exit lines carry position_id; the desks' buy guards count entries (`_held_refusal`) and their sell/raise handlers act on the named position. Tests: core test_pyramiding (14), worker test_pyramiding_worker (3), desk pins flipped + swing sell-by-position test; sleeve suites green. TW21, VB18, SW28, LV10.1 written. Gate run next, then commit and deploy (every sleeve book is empty on the box, so no position's exit changes mid-flight).
- 2026-09-28 05:20 IST LV10 gates 4/4; committed 669cf4b; deployed from a clean worktree (`✓ DEPLOYED 669cf4b`, running=16, alembic 0058_qulla_exits, books empty, flags unchanged). Root ledger R1–R5 met: 76 of 76 leaf gates across LV0–LV10. Pack complete; report written.
- 2026-09-28 10:50 IST Incident 09:35–10:17 (box thrash, Laya) recovered by an EC2 reboot; Laya off by Maulik ("leave it off") behind a compose profile; the session-cap defect (signal-date count) fixed on both desks and both planners with tests; docs amended; lint clean; desk suite re-run. Commit next; deploy after 15:30 (never into the open session).
- 2026-09-28 15:50 IST Fourth deploy of the day, after the close (Maulik, 09:45: "deploy it now, also remove laya completely"; held by deploy-swing.sh's market-hours guard under five open TWT positions): c600217 (LV10.2 session cap by day sent, LV10.3 Laya removed, F3-0…F3-4 with migration 0059 and every F3 flag absent or false) from a clean detached worktree 15:33–15:45 IST, `DEPLOYED c600217`, `SWING OK`; on the box afterwards: 15 services running (no laya container), alembic `0059_f3_directional`, the five TWT positions still OPEN with their GTTs (CUPID 467 GTT 337780639 stop 212.85; ETERNAL 377 / 337780641 / 265.15; LALPATHLAB 64 / 337767114 / 1547.10; NAUKRI 99 / 337767116 / 997.75; SYRMA 70 / 337780643 / 1413.15), the stopped Laya container and the `baskfy-staging_baskfy-laya` volume removed by hand (no laya image either), disk 27 %, free memory 1,372 MB (4,991 MB available). verify-fno at c600217 failed one check — its auto-name scan trips on the F3 flag named in a core docstring; the amended script (F3-5, uncommitted at deploy time) admits that one spelling and instead asks for the two F3 env lines the next deploy's compose adds. Worktree removed.
