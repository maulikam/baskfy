# Rust rewrite — work remaining (snapshot: 30 Oct 2026, 18:30 IST)

All agents were stopped on Maulik's instruction and the in-flight work was saved as WIP commits:

| Branch | Commit | What it holds |
|---|---|---|
| `claude/jolly-brown-6aeyjb` (main tree) | `358822a` RS6: WIP | wave 6 — the rest of the worker, as far as it got |
| `rs-desk` (worktree `../baskfy-wt-desk`) | `7e5fcfa` RS-D2: WIP | wave D (`2bec81e`) + the D2 fix round, as far as it got, incl. Python desk fixes |

Neither WIP commit has been through an integrator: **the workspace build, clippy and the full test
suite are unverified on both.** Python stays live for everything; nothing is cut over.

Done and graded (for context): baskfy-core (1,702 goldens), baskfy-providers, API GET routes
(2,285/2,285), 82/94 API write routes (1,175/1,175), the nightly chain (RS5, 7/7 scenarios).

Ground rules that still apply to every step below: no live order/GTT/Kite write; every execute/auto
flag defaults false; DRY_RUN defaults true; never touch `portfolio.db`, the live `baskfy-local-*`
stack or ports 8480/5432; parity on bed clones only (`baskfy-rs-pg` :55432).

---

## 1. Finish wave 6 — the rest of the worker (main branch)

**State:** 65 of the 76 registered Celery tasks have Rust bodies (`PORTED` lists in
`rust/crates/baskfy-worker/src/tasks/*.rs`). 6 of 15 lanes returned results to the workflow;
the rest were interrupted (usage limit, then the stop). No integrator ran.

### 1a. Per-lane status and what is left

| Lane | Tasks ported | Graded (Python vs Rust) | Remaining |
|---|---:|---|---|
| foundation | registry 76, Beat table | harness self-checks 12/12 | `nightly_diff --chain` not re-run after the wave-5 refactor into `tasks/context.rs` — re-run fresh + rerun |
| screener | 2/2 | warm_screen_cache 9/9, alerts.dispatch 16/16 (rewritten case `dispatch-no-previous-run` + new `dispatch-no-change` ungraded) | grade the 2 rewritten cases; ranking_terms screens still raise NotPorted (none on the bed) |
| api_routes | 2 (+ routes) | overlap 35/35, explore 49/49, writes_screens 260/260 (R7.15 closed), GET 2,284/2,285 (the 1 is the known BASKFY_FIXTURE_DIR setup) | 11 of 12 `task_cases/api_routes.json` cases ungraded (purge, resync); `baskfy.overlap.catalyst_scan` written but disabled until swing_eod exposes its per-name loop with a `symbols` arg (R8.ar4); the **7 screener-running write routes stay on Python** (crate cycle API→worker + 4 unported core modules, R8.ar6) |
| curated | 5/5 | 0 of 35 cases run | grade all of `task_cases/curated.json`; Python's DB-backed tests have no Rust equivalent; `WideDecimal::sqrt` added to core (verify against CPython vectors in the integrator run) |
| backtest_engine | 1/1 | 12/12 incl. CSV sha256 | 2 dropped edge cases (delisted holding, invalid inline definition) to re-add and grade; inline executor stays on Python (R6.28) |
| sleeve_backtests | 2 | vbt 9/9, swing 5/5 (4 written cases unrun), twt 2/5 | re-run the 3 twt cases (payload key-order fix applied, not re-graded); run the 4 swing cases; **options.backtest NOT ported** — needs ~6,000 lines of options core (backtest, suite, replay_day, executor, exits, plan/o2/o3, scan, structures) with no Rust port and no goldens |
| swing_eod | 5/5 | 7 of 27 | grade the other 20 (one `seed_sql` was fixed, re-run); 14 DB tests ignored until `BASKFY_RS_SWING_EOD_DB_URL` points at a clone |
| swing_live | 8/8 | 26/26 (r3) + earlier 17/25 | the full 43-case file in one run |
| twt_jobs | 5/5 | 30/30 | no case reaches a signalling live scan (bed's provisional bar gives 0 signals) — build one |
| vbt_jobs | 9/9 | batch 1: 16/16 | batches 2+ of the 57 cases; merge the lane's private copies (alert names, Kite quote source, Celery producer) with twt_jobs' |
| portfolio | 2 (eod_nav, capture_kite_trades) | 12/12, 9/9 | holdings_sync, desk.py, desk_score, resync live elsewhere per R8.pf3 — confirm each has an owner; lane carries its own ledger subset (R8.pf2) to collapse later; `cargo fmt` its files |
| ops_alerts | 6 | unknown (lane interrupted) | read its report/state, finish remaining modules (kite_login_nudge, purge, fundamentals, listings, action_recovery…), grade |
| fno | 7 | unknown | finish + grade `task_cases/fno.json` |
| options | 7 (master, collect_chain, index_bars×3, checks, weekly) | unknown | `scan`, `plan_o1/o2/o3` are gated stubs (R8.op2) — need the options scan/plan core; `plan_o1` runs but is missing from `PORTED` (breaks the registry test) |
| backfills | **0** — never started | — | port backfill, deep_backfill, bhavcopy_backfill, breadth_backfill, index_backfill, index_repair; resumable + idempotent (house rule 7) |
| clis | **0** — never started | — | ranking_backfill, reference_backfill, eq_bars + every `*_cli.py` as `baskfy-worker` subcommands; `reprocess_instrument` honours `as_of` (house rule 5) |

### 1b. Integration (one agent, after the lanes)

1. Fix `tests/task_registry.rs::every_stub_refuses_with_task_not_ported` — it hard-codes
   `REGISTRY.len() - 4`; make it count from the lanes' `PORTED` lists. (Every lane hit this.)
2. Clear cross-lane clippy: `panic!` in non-test code (backtest_engine job/loader/payload,
   sleeve_backtests/twt), `&mut *tx` in api_routes, eprintln in tests; `cargo fmt` both crates.
3. `cargo fmt --check`, `clippy --workspace --all-targets -D warnings`, `cargo test --workspace` on the bed.
4. Every `task_cases/*.json`, `nightly_diff --chain` (all 7 scenarios), `api_diff` GET + all writes files — all EQUAL.
5. Audit: no order/GTT/Kite write in worker/API; no stray `rst_*/rsn_*/rsw_*` DBs; `baskfy` unchanged.
6. Collapse duplicated helpers (vbt/twt private copies, portfolio ledger subset, WorkerSettings vs API Settings).
7. Commit `RS6: green — …`.

---

## 2. Finish the desk fix round (D2), then merge `rs-desk`

**State:** wave D ported all 17 desk lanes (`2bec81e`, 485/491 desk_diff EQUAL) but every lane
reviewer left findings (73 total, `scratchpad/deskD_findings.json`). D2 closed 7 lanes; 8 were
interrupted mid-fix; 2 never started.

### 2a. Lanes closed in D2 (results recorded)

| Lane | Graded | Leftover for the integrator |
|---|---|---|
| exec_guards | 30/30 + 51 websec | `AppState.kite` is the bare broker — session, twt, swing, options, weekly read through it and **skip the Kite read limiter** (must fix before cutover); password-variant run unfinished |
| exec_kite | 8/8 | `/status` must call `exec_kite::status_session` (session lane's file) |
| swing_desk | 42/42 + breadth 7/7 | one test flaky under load (`…re_sizes_a_signal_line_to_the_ceiling`) |
| swing_exec | 27/27 | swing_desk must return 500 on an inverted stop band; wire `DeskLevel` into `open_store`; rename `order` field in breakout.rs |
| twt_desk | none (stopped for code-first) | grade the new `BEGIN / FOR UPDATE NOWAIT / COMMIT` session lock on the bed; run the ignored Postgres test |
| weekly_books | partial | session lane must wire `run_service('session-supervisor')` (one line); add `baskfy-desk snapshot` subcommand in main.rs; 7 /reconcile & /performance cases 404 until routes exist |
| weekly_plan | 0 graded (queued) | run `weekly_plan.json` (38 cases) + `plans_weekly.json --plans` + the generated-scan variants |

### 2b. Lanes interrupted mid-fix — remaining findings

- **exec_gateway** — lifecycle routes and the FO/GTT test files were finished in its run (33/33, 69 live goldens; runs C and D 0 diffs); run **B2** (lifecycle_gtts fixture) still to grade. Order-verb scan: `KiteWrites` named in swing/fno test files must go.
- **swing_monitor** — service wired, AutoConfirm real, 122 Python tests ported, SMTP/Telegram send, `run_chore` Python import fixed; final rerun 21/21. Just needs its result recorded (re-run returns quickly).
- **twt_exec** — port `test_twt_auto.py::TestTheRealPathStillRefuses` fully; explain 17 vs 20 Kite reads; the "confirmed for real" claim was wrong (all buys refused PROTECTION_UNRESOLVED on the bed) — build a case that confirms.
- **vbt** — **the three-entries-a-session cap lock**: fix in Python first (lock + count in one transaction), then Rust; Python files `vbt_desk.py`/`vbt_execute.py` are modified in the WIP commit — verify complete, add the two-concurrent-confirms spec test. Also: wire `on_order_update` (nothing in Rust calls it); exact-expiry boundary rounds to ns in Rust vs µs in Python (404 vs 410); 44 vs 109 tests.
- **fno_desk** — ~20 DB-backed `test_fno_ledger.py` tests and the confirm-success/refusal-world tests (bed has no ISSUED/EXIT plan — seed one); `KiteSources::quotes` should raise where Python raises.
- **fno_exec** — **half of fno_monitor.py unported** (09:20 F1/F2/F3 entry plans, F3 add, scan_candidates, sleeve_capital, margin/cost checks, `_f1/_f2/_f3` watch decisions, `_raise_roll`, the night's marks); `run_service('fno-monitor')` must return 0 when disabled; end-to-end DRY_RUN tests of every fill path. F3 auto-exit stays exit-only, flag default false.
- **options** — `strategies/nifty_options.py` unported (needs `baskfy_core.options.exits` + bars); 31 monitor loop tests; timestamp-per-step vs one `now`; error order when no gateway; add R8.F1–F6 entries.
- **analytics_regime** — `regime_run.py` + the regime engine (`core/regime` evaluate, `core/regime_alloc`), RegimeState writers, `enrich_orders`, `tax_detail`; boot must call `lanes::session::startup` (stored settings).

### 2c. Lanes never started in D2

- **analytics_views** — port `metrics.py` (853 lines), `benchmark.py`, `performance_view.py` and `/performance`, `/performance/data`, with test_metrics 58, test_benchmark 20, test_drawdown 14, test_xirr 14, test_twr_chaining 13; 13 test_autorun tests.
- **session** — Starlette `redirect_slashes` app-wide (307 + Location; fixes the trailing-slash reds in every lane); port the unowned main.py routes (`/reconcile`, `/reconcile/data`, `/performance*`, `/tradebook*` GET/POST, `/tradebook/corporate-action(+/delete)`, `/tradebook/data`, …); wire `session-supervisor` (needs a `ReconcilePass` impl); the two order-path telemetry tests; TWT drain error class.

### 2d. D2 integration, then merge

1. `cargo fmt/clippy/test --workspace` in the worktree; `desk_diff` over every `desk_cases/*.json` and `--plans` (all plan kinds incl. twt MORNING/LIVE, vbt, fno exits — builders still missing) — all EQUAL incl. recorded order/GTT calls.
2. The desk's Python suite (`kite-momentum-rebalancer/tests`) green on the worktree's code (D2 changed Python).
3. Safety audit in STATUS.md (gateway is the only order path; flags false; DRY_RUN true; read limiter on every Kite read).
4. Commit `RS-D2: green`, then **merge `rs-desk` into the main branch**. Expected conflicts: `rust/Cargo.toml` members, `Cargo.lock`, `docs/rust-rewrite/{STATUS,DECISIONS-RS}.md`, `tools/parity/README.md`, `baskfy-providers/src/{kite,py}.rs` (desk made parsers `pub`). Renumber DECISIONS-RS lane labels (R8.*, R-*) once merged.
5. Remove the worktree (`git worktree remove ../baskfy-wt-desk`) after the merge.

---

## 3. Python desk defects found — need Maulik's image rebuild

Fixed on `rs-desk` (in Python first, then Rust). The live desk carries none of them until the
branch is merged and the desk image rebuilt (`tools/deploy/local.sh`). The desk is in DRY_RUN, so
nothing is at risk today, but **all four matter before any live trading**:

1. **swing_clock `run_chore` missing import** (`app/swing_clock.py`) — every watch-triggered chore
   failed; the 10:45 cutoff and 15:15 GTT sweep only ran from the 15:30 backstop. Fixed + regression test.
2. **TWT three-entries cap lock locked nothing** (autocommit connection) — fixed with
   `BEGIN / FOR UPDATE NOWAIT / COMMIT`; a held row returns 409 SESSION_BUSY before the broker.
3. **VBT cap lock — same defect.** Fix in progress in the WIP commit; finish + verify (see 2b).
4. **Swing lock (SW10.4) waits inside an async route** — two near-simultaneous swing confirms can
   freeze the desk's event loop. Not fixed; decide NOWAIT like TWT.

Also from the worker: Python's scan-after-login re-runs an in-flight scan row instead of skipping
(two writers on one row) — reproduced in Rust; change in Python first if skip was the intent.

---

## 4. The deferred test-and-comparison pass

Maulik switched to code-first on 30 Oct (14:29). Everything written after that compiles but is
not proven equal to Python. This pass, per lane, is:

1. Port the Python tests the lane skipped (each lane's report lists them; the desk lanes' counts
   are in 2b/2c).
2. Run its parity file until EQUAL — `task_diff.py` (worker), `desk_diff.py` (desk, incl.
   `--plans` and `--strict-reads`), `api_diff.py --writes` (API).
3. Re-run `nightly_diff --chain` all scenarios after everything is merged.

Practical notes learned this session:
- The machine-wide lock `rust/target/nightly_diff.lock` serialises every heavy run; with 15+
  lanes it was the bottleneck. Run parity in a dedicated, sequenced workflow (one agent per
  case file, no concurrent porters), or give lanes a lighter step-only mode.
- Launch long parity runs with the tool's background mode; `nohup` children die with the shell.
- Two concurrent workflows at full width exhausted the 5-hour usage limit in ~4 hours.

---

## 5. Cutover (each step reversible; Python stays the fallback)

**API and worker — piece by piece, after their tests are green:**
1. API: one Caddy route at a time to `baskfy-api` (GETs first, then writes per router). Keep the
   cutover gates: live Kite overlay routes, `/baskets` live build, `/brokers/callback`, in-flight
   `/events`, and the 7 screener routes stay on Python until ported.
2. Worker: the Celery beat entry keeps its Python guards and shells out to
   `baskfy-worker nightly --date D` (R7.1); same advisory lock, so Python and Rust never run a
   chain together. Then the other tasks one by one via `baskfy-worker task <name>`. Pass the real
   Celery task id through (the scan-after-login `task_id`).
3. Before the nightly runs on Rust in production: Parquet fixtures (R4.12), Kite transport
   timeout known bug (R4.10, fix both languages).

**Desk and order execution — only on Maulik's explicit go:**
1. Fix the bare-broker reads (read limiter on every Kite call) and every item in section 3.
2. Two **shadow Fridays in DRY_RUN** (earliest 6 and 13 Nov 2026) with byte-equal plans from the
   Python and Rust desks on the same inputs.
3. Maulik's go → switch the desk service; execution flags stay as Maulik sets them (the three
   named auto exceptions only; an agent never flips them).
4. Then: merge the desk's pages into the web app (one product), retire the Python services.

---

## 6. Open decisions for Maulik (all ⚠ UNREVIEWED in DECISIONS-RS)

- R7.1 cutover scheduling (Celery shells out to the Rust binary) · R8.0 desk ported in parallel
  (porting ≠ cutover) · LOCAL-1 DRY_RUN off for the API container only
  (`docs/DECISIONS-MERGE.md`) · the TWT/VBT lock fixes (stricter boundary) · R8.ar6 screener routes
  stay on Python · the swing lock NOWAIT question (section 3.4).
- Hand-offs still with Maulik: baskfy.com nameservers, Gmail app password for alert mail, removing
  the old AWS static IP / postback URL in the Kite console, desk image rebuild for section 3.
