# Baskfy — the NIFTY options book, code complete

**23 September 2026.** OP0 through OP15, one commit per module, branch `developer`. Five sleeves
(O1-M monthly condor, O1-W weekly condor, O2 directional long option, O3-A range-break and O3-B
gap-hold debit spreads) are built end to end **on paper**: scan → plan → confirm on the desk →
the real gateway's dry-run branch → the monitor's exits → the journal and the ledger. **Every money
flag is false and no order was placed.** **`df6698d` is live on `staging.baskfy.com`** (23 Sep 2026, 21:50 IST): thirteen services, the new `options-monitor` idling with its flag off, alembic at `0051`.

Three documents matter more than this one:

* **[`NEEDS-MAULIK.md`](NEEDS-MAULIK.md)**, sections "OPT" and "F&O": what only you can do.
* **[`docs/options/STATUS.md`](docs/options/STATUS.md)**: every module's acceptance criteria mapped
  to tests, and each module's "What is NOT done".
* **[`docs/options/DECISIONS-OP.md`](docs/options/DECISIONS-OP.md)**: **126** calls
  made without you, each `⚠ UNREVIEWED`, with the rejected alternatives and how to reverse it.

Every number below was measured on 23 Sep 2026 by the command beside it, from the repo root unless
said.

## Where it stands

| | | measured by |
|---|---|---|
| Modules | **OP1–OP2, OP4–OP15 ✅**; OP0 and OP3 🟡 (one of OP0/OP3's six live reads needs an expiry to have passed; the full index backfill has not run) | `sed -n '/^## Module ledger/,/^## OP0/p' docs/options/STATUS.md` |
| Desk suite | **2,317 passed**, 17 skipped, 90 s | `cd kite-momentum-rebalancer && .venv/bin/python -m pytest -q -p no:cacheprovider` (with `BASKFY_TEST_DATABASE_URL`) |
| Screener suite | **10,842 passed**, 6 skipped, 1 xfailed, 27 min. The one failure was `test_api_admin.py`'s override test: its hardcoded 21 Sep 2026 expiry had passed. It is now relative to now, and the file passes 22/22 | `cd decile-blueprint && uv run pytest packages services -p no:cacheprovider --deselect services/api/tests/test_load.py` |
| Lint | **clean** (ruff, ruff format, mypy strict) on every screener file this run touched | `uv run ruff check …; uv run mypy …` |
| The drill: a paper day per sleeve against Postgres | `sleeve=O1M confirms=1 fills=8 orders_to_broker=0` · O1-W the same · O2 `fills=2` · O3-A and O3-B `fills=4` · the O1-W skip day `NOT_CONTAINED` · **0 orders reached a broker** | `cd kite-momentum-rebalancer && BASKFY_TEST_DATABASE_URL=… .venv/bin/python ../tools/options/drill.py` |
| Goldens for the Go lane | **47 cases**, six functions, byte-stable (two dumps, one checksum) | `ls go/testdata/golden/L1/options \| wc -l` |
| Mutation score, the 23 options modules | **72.0 %** (1,360 mutants, 979 killed; the ten OP1 modules 86–94 %, the thirteen newer ones 35–81 %). **229 survivors are unjustified**: the first job after this run (OP15.4) | `grep 'mutation score' decile-blueprint/reconciliation/MUTANTS-options.md` |
| Budgets | tick → mark → decision **p99 0.15 ms**; a plan-builder minute **≤ 2.3 ms** | `tools/options/budgets.py` |
| `verify-options.sh` | dev stack **OK**; the box **`OPTIONS OK`** (every money flag false in desk and `options-monitor`, no auto-execute, next expiry 2026-09-29, Kite authed) | `LOCAL=1 bash tools/deploy/verify-options.sh`; `AWS_PROFILE=baskfy-poc bash tools/deploy/verify-options.sh` |
| Orders placed during any of this | **zero** | the drill's `orders_to_broker=0` per sleeve; every gateway in every test is a spy |

## Built

| Module | What exists |
|---|---|
| OP0 | Verified facts (costs, the expiry circular, the F&O segment, the algo rules), `OPTIONS_ENABLED`'s blast radius |
| OP1 | `baskfy_core.options`: the pure core (calendar, greeks, chain, costs, sizing, session, risk, journal, execution, backtest) |
| OP2 | Migration `0050_options` (17 tables), the nightly NFO master and `op_expiry`, the flags and ceilings, `options_gates()`; the gateway's product gate tightened (MIS only on a derivative venue, and only with `INTRADAY_ENABLED`) |
| OP3 | Option quotes with depth and OI, index minute bars, basket margins; the collector and index-bar tasks; the per-family shared limiter. Collector and scan are **on** on the box since 22 Sep |
| OP4 | Each sleeve's signal core and the once-a-minute scan into `op_scan` |
| OP5 | The web Options tab (`/options`, journal, calendar, `/me/options`): read-only, with the SEBI caveat on every page |
| OP6–OP8 | The O1, O2 and O3 plan builders (worker, dark behind the monitor flag): itemised costs, sizing, never-naked legs, a 30-minute plan |
| OP9 | The desk's `options_monitor`: marks from depth ticks, bars from index ticks, each open position's exit per tick, restart resume |
| OP10 | `/nifty-options` and `POST /nifty-options/execute`: the confirm, legs through the real gateway's dry-run branch, the exit sweep |
| OP11 | The journal at every close, §9.1/§9.3 pauses, `OPTIONS_WEEKLY` |
| OP12 | Backtests: Tier 1 signals, Tier 2 on a Black-76 chain, Tier 3 on stored chains, each one sleeve and one tier with its caveat on the row; ±25 % sensitivity |
| OP13 | The safety proof: every Track B/C claim a test; the `/ops` side door closed; the drill |
| OP14 | Five Prometheus rules, four in-process checks, immediate `HARD_EXIT` on a dead Kite token, runbook 12, budgets |
| OP15 | The Go goldens, the mutation re-run, `options-monitor` in compose with every money flag pinned false, `verify-options.sh`, this report |

## Decided

Your decisions for this run are in `docs/fno/DECISIONS-FO.md` M.1: the F&O questions, answered
23 Sep; FO starts after OP15. Everything else is a `⚠ UNREVIEWED` heading in `DECISIONS-OP.md`.
These are the ones I would most want a second opinion on:

* **OP13.2** — found by a property test: a condor with no index level (a desk restart before the
  first index tick) **raised** instead of closing. Now every premium rule and the clock judge
  without a spot, and only the strike-touch rule waits for one.
* **OP14.3** — a refused Kite token raises `HARD_EXIT / FEED_LOST` for every open position **at
  once**, instead of at 14:00. The close still needs a quote, so NEEDS-MAULIK "OPT" says what you
  do.
* **OP12.3** — the options master only knows expiries from 22 Sep 2026, when the collector started.
  A backtest day before that is counted `uncalendared`, never guessed, so **Tier 1 over years of
  history needs the F&O bhavcopy loaded into `op_contract` first**.
* **OP12.6** — on an expiry-day monthly at VIX 14, Tier 2's flat-vol chain has no 50-point strike
  in O1-M's 0.20–0.25 delta band, so the model skips `REJECTED_NO_SHORT_CALL`. Recorded, not tuned.
* **OP11.1** — `06`'s "four −1R weeks → monthly pause" cannot reach `04`'s 8R month, so it is
  tested with four −2R weeks. If you meant a 4R month, `04` §9.1 changes and the test follows.
* **OP14.1** — the 09:20 check "sessions exist" is read as "every sleeve has scanned", because no
  `op_session` exists by design at 09:20.

## The gate — `docs/options/02` §3, per sleeve, with the evidence

| § | Condition | Evidence today |
|---|---|---|
| 3.1 | OP13 green, including the `OPTIONS_ENABLED` side door | ✅ `test_options_safety.py` (the side door, the four-flag AND with a spy, NRML refused with every switch on), `test_options_safety_proof.py` (Hypothesis never-naked, exact close, never overnight; the repo scans), the drill |
| 3.2 | The paper period on the live chain, through the deployed desk, `DRY_RUN=true`, zero rule violations | ⬜ **not started.** It needs `BASKFY_OPTIONS_MONITOR_ENABLED=true` on the box (worker and desk), which starts the plan builders and the monitor. **I have not flipped it; see the question at the end** |
| 3.3 | Tier 1 and Tier 2 on the page with caveats; Tier 3 over the paper period's chains, positive after costs, or your written waiver | ⬜ the code and the cards exist (OP12); nothing has run on the box's data. Tier 1 over history needs the calendar (OP12.3) |
| 3.4 | Your written capital and risk decision per sleeve | ⬜ every `op_sleeve_config` capital is ₹0 (paper at one lot) |

## The first paper morning — step by step

The evening before, from the laptop (outside 09:15–15:30 and 18:40–21:15):

1. **Turn the monitor on in both places** (operational, moves no money; PACK.11). On the box, in an
   SSM shell:
   * `/opt/baskfy/.env.staging.compose`: `BASKFY_OPTIONS_MONITOR_ENABLED=true` (desk and
     `options-monitor`);
   * `/opt/baskfy/.env.staging`: `BASKFY_OPTIONS_MONITOR_ENABLED=true` (worker and beat: the
     plan builders, the checks, the weekly summary).

   Then `docker compose --env-file .env.staging.compose -f compose.prod.yml up -d worker beat desk
   options-monitor`. The collect and scan flags are already on (OP3.11).
2. `bash tools/deploy/verify-options.sh`: every money flag false in desk and monitor, the monitor
   flag reported `true`, the next NIFTY expiry printed.

On the morning:

3. **Before 09:14**: log in to Kite through the web app (the desk reads the shared token).
   `https://desk.staging.baskfy.com/status` must say `"authed": true, "dry_run": true`.
4. **09:14** `options-monitor` starts (`box.sh '… logs options-monitor'`: `options-monitor: next
   start …` then the monitor's lines). **09:15** the collector and scan run every minute.
   **09:20** the `SCAN_STARTED` check.
5. **09:30–13:30** O2 may raise a plan; **09:45** O3-B on an expiry day; **10:00** O1 on its expiry
   day; **10:19+** O3-A. Each plan is an `OPTIONS_PLAN` email and a row on
   `https://desk.staging.baskfy.com/nifty-options`, which expires in 30 minutes.
6. **Click "Confirm — simulated"** on a plan. The legs go through the gateway's dry-run branch,
   wings first; fills come from the live depth; the position opens `simulated=true`.
7. The monitor marks it every tick and raises its exit (profit, stop, time stop, invalidation, hard
   exit at 14:30/14:45/15:00). The sweep closes it within a second; `op_journal` gets the row.
   **Hard exit + 3 min**: the `FLAT_AFTER_HARD_EXIT` check. **15:35**: the collector's full-day
   check.
8. **That evening**: `/options/journal` shows the row under the paper card. Friday 16:30 brings
   `OPTIONS_WEEKLY`.

## The per-sleeve paper checklist (`02` §3.2)

A skipped day counts as a session; a day the desk was down or had no Kite login does not.

| Sleeve | Sessions | Of which carried a position | Zero of these | About |
|---|---|---|---|---|
| O1-M | 6 consecutive monthly expiries | ≥ 3 | stale-quote entry · orphan leg · late exit · journal gap | 6 months |
| O1-W | 12 consecutive weekly (non-monthly) expiries | ≥ 6 | the same | 3 months |
| O2 | 60 consecutive trading sessions | ≥ 25 | the same | 3 months |
| O3 | 20 consecutive expiry days (weekly or monthly) | ≥ 8 | the same | 5 months |

Then, per sleeve: Tier 3 over those observed chains with its sample on the page, positive after
every cost (or your written waiver); your capital and risk decision in `DECISIONS-OP.md`; and the
flip, by your hand, of four lines (`OPTIONS_ENABLED`, `INTRADAY_ENABLED`, `BASKFY_DESK_DRY_RUN=false`,
and the sleeve's execution flag). **LIVE is not built even then**: `execute_entry` refuses
`LIVE_NOT_BUILT` (OP10.3) until fills can be read back from Kite. That is a module of its own.

## Deploy

```bash
aws sso login --sso-session baskfy          # when the session has expired; it opens your browser
bash tools/deploy/ship.sh                   # builds, pushes, ships compose, migrates, restarts, verifies
bash tools/deploy/verify-options.sh         # also run by ship.sh from OP15-deploy on
```

`df6698d` went out at 21:43–21:50 IST on 23 Sep. The swing verify was green, and the swing and TWT
flags were unmoved (the book is live and auto-executing, as you set). The first ship did **not**
start `options-monitor`: `deploy-swing.sh` restarts an explicit list of services, and the new one
was not on it. It was started by hand (`up -d options-monitor`), and `verify-options.sh` then read
`OPTIONS OK`. The list, `ship.sh`'s count (thirteen running) and a `verify-options.sh` step are
fixed in the commit after OP15, so the next ship does all of this itself. Rollback: the image tag
lines in `/opt/baskfy/.env.staging.compose` back to the previous sha, then `up -d`.

## Not done

* **The paper periods** (`02` §3.2), which start with the monitor flag.
* **OP3's backfill**: the index minute history (2015→) has not been pulled, and one of the six live
  reads waits for an expiry to pass.
* **A historical expiry calendar** for backtests before 22 Sep 2026 (OP12.3), and `07` §2's
  bhavcopy check of Tier 2 (OP12.7).
* **LIVE execution**: reading fills back from Kite (OP10.3).
* **The desk page's status and ledger panels** (`05` §3, OP10.8).
* **The limiter share with other mornings running** is measured only on the box (OP14.2).
* The e2e spec for the web tab is written and has not been run (OP5).

## Needs you

1. **The monitor flag** — the question below.
2. **Each sleeve's capital and risk** — only after its paper period (`02` §3.4).
3. **NEEDS-MAULIK "OPT"** — what to do if the token dies with a position open.
4. **FO** (the F&O pack) starts next, per your "Finish OP first".

## Caveats I will not bury

* **No options code has seen a live Kite session trading.** The collector and scan have run on the
  box since 22 Sep; the plans, the monitor, the confirm and the exits have run only on fixtures and
  on `baskfy_test`.
* **Tier 2 is a shape, not a number** (`07` §4). O2's Tier 2 flatters it (`04` §13.2); O1-M's skips
  most expiry days on the flat-vol chain (OP12.6). **None of the numbers in this run is evidence of
  an edge.** The F&O research found every stock family negative and the index monthly condor
  +0.033R on 100 trades (not significant).
* **The drill's chains are synthetic** (a flat or two-level vol). They prove the machinery, not
  the prices.
