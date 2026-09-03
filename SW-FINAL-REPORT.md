# Baskfy — the swing book, code complete

**3 September 2026.** SW0 through SW16, one commit per module, branch `developer`. **`bf4168b`
(SW16) is live on `staging.baskfy.com`** — ten services, `DRY_RUN=true`, every swing flag false,
migrations at `0033`. No order was placed. Three documents matter more than this one:

* **[`NEEDS-MAULIK.md`](NEEDS-MAULIK.md) § Swing** — the nine things only you can do, in order.
* **[`docs/swing/STATUS.md`](docs/swing/STATUS.md)** — every module's numbers and its "did NOT do".
* **[`docs/swing/DECISIONS-SW.md`](docs/swing/DECISIONS-SW.md)** — your twenty decisions (MD1–MD20)
  and every call made without you, tagged `⚠ UNREVIEWED`.

Every number below was measured on 3 Sep 2026 by the command beside it, from `ROOT` unless said.

## Where it stands

| | | measured by |
|---|---|---|
| Modules | **SW0 → SW16**, 20 ledger rows · SW13 🟡 (`bf4168b` deployed; the desk vhost waits on DNS, S4) | `sed -n '/^## Module ledger/,/^States/p' docs/swing/STATUS.md \| grep -c '^| SW'` |
| Desk suite | **1,706 passed, 17 skipped**, 59 s | `cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests -q -p no:cacheprovider` |
| Decile suite (core + providers + execution + api + worker, `test_load` deselected) | **5,694 passed, 3 skipped, 3 deselected**, 21 min (measured before SW15/SW16 added their tests) | `cd decile-blueprint && uv run pytest packages/core/tests packages/providers/tests packages/execution/tests services/api/tests services/worker/tests -p no:cacheprovider --deselect services/api/tests/test_load.py` on `baskfy_sw_test` |
| The re-pinned gate tests | **59 passed** | `uv run pytest services/api/tests/test_api_swing_journal.py services/worker/tests/test_swing_eod.py` |
| Lint | **clean** (ruff, ruff format, mypy strict, TS lint/typecheck), 26 s | `cd decile-blueprint && make lint` |
| The DRY_RUN drill | `confirms=2 fills=2`, exposure **25.0 %** of the sleeve, **0 orders reached a broker**, `DRILL OK`, 4.5 s | `cd decile-blueprint && BASKFY_DATABASE_URL=<baskfy_sw_test> BASKFY_SOLE_USER_ID=1 DRY_RUN=true uv run python ../tools/swing/drill.py` |
| Goldens for the Go lane | **79 cases**, byte-stable | `ls go/testdata/golden/L1/swing \| wc -l` |
| Mutation score, the nine swing targets | **88.2 %** (654 mutants, 77 survivors, every one justified by name) | `grep 'mutation score' decile-blueprint/reconciliation/MUTANTS.md` |
| Unreviewed decisions | **65** headings (the grep says 66; one is the MD table's own heading, "not ⚠ UNREVIEWED") | `grep -cE '^#+ .*⚠ UNREVIEWED' docs/swing/DECISIONS-SW.md` |
| Live on the box | `bf4168b`, ten services, alembic `0033`, sleeve ₹25,00,000 at 0.5 %, the 2 Sep gate **RED** | `bash tools/deploy/verify-swing.sh` |
| Orders placed during any of this | **zero** | every drill line ends `broker client touched: 0` |

## Built

| Module | Commit | One line |
|---|---|---|
| SW0 | `35f2a03` | The pack adopted; both suites measured at baseline (desk 1,330, screener 3,383) |
| SW1 | `1a99405` | The pure core re-verified; mutation score 41.0 % → 83.4 % |
| SW2 | `8a8f26d` | Twelve `sw_` tables, `sw_config` at ₹0, three flags + three ceilings, no ceiling is a form field |
| SW3 | `03edec8` | `baskfy.swing.detect` as the chain's twelfth step; `make swing DATE=…` |
| SW4 | `f83530f` | Five `/swing` routes, the Setups and Market pages, read-only on both sides of the wire |
| SW5 | `1c675f4` | The evening: manage, plan, watchlist, email, session count; four more routes |
| SW6 | `6fd8f5a` | Premarket levels + gap scan behind their flag; the opening-range monitor with no gateway |
| SW7 | `e6ed2b6` | The desk page and `/swing/execute`: LIMIT buy + GTT in one call through the real gateway (DRY_RUN) |
| SW8 | `53f21c8` | The journal in R; the ladder settled by the evening; `/swing/journal` |
| SW9 | `951b438` | The EOD backtest: engine, runner, table, CLI, the card with its caveats verbatim |
| SW9.5 | `b7a507e` | His rules, quoted (`07`): stop ≤ 1 ADR, 3 entries/session, 10-over-20, the 15 %/10 % lock-out |
| SW10 | `f17412f` | Every Track B/C claim a test; the confirm-time gate under the session lock; the drill at 25 %, not 34 % |
| SW9.6 | `182b254` | The backtest carries the index rule, its own drawdown, gate-on vs gate-off |
| SW10.5 | `7bdd427` | A7–A10, A14: `PENDING_RANGE`, marketable LIMIT + partial fills, half risk at plan time, real closes from day one, the focus funnel |
| SW13-prep | `17fd2f6` | The desk as a Baskfy compose service (`Dockerfile.desk`, `desk` + `swing-monitor`), smoke-tested locally |
| SW11 + SW11B | `beb5ff5` | Five alerts + runbook 6, the desk as its own clock (10:45 / 15:15), tick-built range, the notifier, the S2 probe, the catalyst feed |
| SW13-run | `2431066` | **Deployed `beb5ff5`**: images, the `desk.` record, alembic `0032`, the sleeve seeded, ten services up, box-side verify green; `desk.staging.baskfy.com` NXDOMAIN until S4 |
| SW14 | `b38de36` | The web hub does everything `05` §2 says and only that: five money-free server actions, `/me/swing` settings form, the gate and lock-out on Setups, add-by-hand on Watchlist |
| SW12 | `6884d1b` | 79 goldens byte-stable, 654 mutants 88.2 % with every survivor named, `02` §3 rewritten under your name, this report |
| SW13-run #2 | `715eda5` | **Deployed `6884d1b-fix1`** and ran the first real scan on the box: 2,317 names → 402 liquid → **9 candidates** (8 flags, 1 parabolic) |
| SW15 | `151ce21` | **Scan now**: `POST /swing/scan`, provisional intraday bars from Kite quotes during market hours, replaced by the close; a button on the hub and the desk |
| SW13-run #3 | `b552e24` | **Deployed `151ce21`**; a queued scan ran through the sweep unaided in 13 s |
| SW16 | `bf4168b` | The box's index snapshots were the fixture builder's random walk — repaired (7,403 rows, 24–26 Aug filled), a > 40 % guard on the writer, `make index-repair`; the 2 Sep gate corrected **GREEN → RED** |
| SW13-run #4 | `e7853e3` | **Deployed `bf4168b`** — what is live now |

## Decided

Your decisions are the MD table at the end of `docs/swing/DECISIONS-SW.md` (MD1–MD20; MD8′ is
the one that rewrote the gate). Everything else is a `⚠ UNREVIEWED` heading in that file —
**62** of them — each with the choice, the alternatives and how to reverse it. The ones I
would most want a second opinion on: **SW10.4** (a BUY is re-sized under the session lock rather
than refused), **SW11.4** (the gap scan at 09:16 reading `ohlc.open`, before S2 has run),
**SW12.1** (flat goldens carrying raw adjusted bars), and **Q-SW12-1** in `QUESTIONS.md` (the plan
sizes on unsnapped levels; ≤ one tick × quantity; not fixed).

## The gate — `docs/swing/02` §3, as you rewrote it (A11), with the evidence

| § | Condition | Evidence today |
|---|---|---|
| 3.1 | SW10 green: flag false → no path to `OrderGateway.place`; `DRY_RUN=true` → simulated fill + GTT per confirmed line, 0 orders reach a broker | ✅ `test_swing_safety_properties.py`, `test_swing_execute.py` (spy over the real gateway, both `DRY_RUN` values); the drill above: `broker client touched: 0`, journal `dry_run, gtt_dry_run, gtt_dry_run, order_cancel_dry_run` |
| 3.2 | One DRY_RUN drill morning on a real session, on the box | ⬜ **not run** — the box is ready on `beb5ff5`; needs the desk's DNS record (S4) and your morning (SW-5). The session counter (`sessions.required = 20`) is information |
| 3.3 | The backtest on `/swing/journal` under "Backtest, EOD approximation" | ⬜ **not run on real bars** — engine and card are in (68 core tests, 26.9 s for three books on the fixture); run `tools/swing/backtest.py` on the box after the deploy |
| 3.4 | Your written risk decision: ₹25,00,000, 0.5 %/trade, half risk × 5 sessions, 15 %/10 % lock-out, rung 0 | ✅ MD1, MD2, MD12, SW9.5, MD13 in `DECISIONS-SW.md`; the box's `sw_config` seeded ₹25,00,000 / 0.5 % (SW13-run, `sw_config_sleeve: 1`); `first_live_sessions_left = 5` (the drill prints it) |
| 3.5 | The flag flipped by your hand, never by the run | ✅ false on the box: `verify-swing.sh` (SW13-run) read `DRY_RUN=true` and all four `BASKFY_SWING_*=false` in both desk containers; the desk's dry-run is its own `BASKFY_DESK_DRY_RUN` since SW13-run, so the api's `BASKFY_DRY_RUN=false` cannot reach it |

## The DRY_RUN drill morning (§3.2) — one weekday on the box

Flags for the morning, `DRY_RUN=true` throughout, `BASKFY_SWING_EXECUTION_ENABLED` **stays false**:

```
# the evening before, in an SSM shell on the box (box.sh never carries a value):
#   /opt/baskfy/.env.staging.compose  →  BASKFY_SWING_MONITOR_ENABLED=true      (desk, swing-monitor)
#   /opt/baskfy/.env.staging          →  BASKFY_SWING_EP_PREMARKET_ENABLED=true (worker, beat)
#                                        BASKFY_SWING_TIMING_PROBE=true        (SW-4, the same morning)
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml up -d worker beat desk swing-monitor'
```

1. **The evening before, after 21:05.** `baskfy.swing.detect` (21:00) and `.eod` (21:05) ran:
   the EOD email arrived; `https://staging.baskfy.com/swing/positions` shows tomorrow's plan.
   `box.sh '… logs --since 2h worker | grep swing'` if it did not.
2. **Before 09:00** — log in to Kite through `https://staging.baskfy.com` (Portfolio → connect
   Zerodha; Baskfy redeems the token into the shared store; the desk read it read-only on the
   box already — `"authed": true` in SW13-run). Then, because the desk caches its client
   (Q-SW13-1):
   `AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml restart desk'`
   `https://desk.staging.baskfy.com/status` must say `"authed": true, "dry_run": true`.
3. **08:50** levels (`swing-premarket-levels`) · **09:04** the S2 probe samples `/quote` and the
   09:20 candle, writes `docs/swing/status/S2-kite-timing.md`, disables itself · **09:14** the
   monitor starts (`swing-monitor` logs `app.swing_monitor`), builds the range from ticks
   09:15–09:20, writes `sw_session.monitor_ran` on its first tick · **09:16** the gap scan
   (`ohlc.open`, ≤ 500 names/call) and the MORNING plan · **09:17** the catalyst feed ·
   **09:20** `SWING_MONITOR_DID_NOT_START` would fire if `monitor_ran` is still false.
4. **09:20–10:45** — open `https://desk.staging.baskfy.com/swing` (basic auth; needs S4's DNS
   record first — until then the desk is reachable only inside the box). A range break on
   a watched name is a `TRIGGERED` row and a one-line `SIGNAL` plan; the focus names also reach
   email. **Click Confirm on one** — under `DRY_RUN=true` it returns `SIMULATED`, writes
   `sw_position` / `sw_fill` with `simulated=true`, a `DRY-…` GTT id, and the journal line
   `dry_run, gtt_dry_run`. Watch the status bar: exposure ≤ the rung's 25 %, entries ≤ 3.
5. **10:45** the desk's clock runs the cutoff (open remainders cancelled, `PENDING_RANGE` slots
   freed; `sw_session.notes` gains a line) · **10:50** `SWING_ORDER_OPEN_AFTER_CUTOFF` check ·
   **15:15** the GTT sweep re-arms any naked position · **15:20** its check · **21:00 / 21:05**
   detect and the evening: `manage`, the ladder settled from real closes, the session counted
   (`mode=DRY_RUN`), the email · **21:30** `SWING_DETECT_STALE` check.
6. **That evening**, read: the `sw_session` row for the date (`monitor_ran=true`, its signal /
   confirm / fill counts), `S2-kite-timing.md`, and Prometheus's `baskfy-swing` rules silent. Then
   revert the three flag lines and `up -d` the same four services, or leave the monitor and
   premarket flags on if the morning was clean — they place nothing.

## The first live morning — only after §3.1–3.4 hold

1. **The evening before**: your risk decision is in `DECISIONS-SW.md` (it is: MD1/MD2/MD12);
   `GET /swing/config` shows ₹25,00,000, 0.5 %, `first_live_sessions_left: 5`, rung 0;
   `/swing/positions` shows tomorrow's plan **at risk × 0.5** ("first live sessions: 5 left ·
   risk 0.25 %").
2. **The flip, by your hand — two files, three lines** (compose.prod.yml says why): in
   `/opt/baskfy/.env.staging.compose` set `BASKFY_SWING_EXECUTION_ENABLED=true`,
   `BASKFY_SWING_MONITOR_ENABLED=true` **and `BASKFY_DESK_DRY_RUN=false`** (the desk's own
   variable since SW13-run; the api's `BASKFY_DRY_RUN` is already `false` there and does not
   reach the desk); in `/opt/baskfy/.env.staging` set `BASKFY_SWING_EXECUTION_ENABLED=true`,
   `BASKFY_SWING_EP_PREMARKET_ENABLED=true`. A swing order is real only when **both** the desk's
   `DRY_RUN` is off and the swing flag is on (`swing_gates()`, `app/swing_execute.py`) — and
   `BASKFY_DESK_DRY_RUN=false` also takes the **weekly book** out of dry-run on the same desk:
   its Friday `/execute` still needs your confirm click, but it is then live too.
   Then `up -d worker beat desk swing-monitor`; `https://desk.staging.baskfy.com/status` must
   say `"dry_run": false`. Record the date under NEEDS-MAULIK SW-7.
3. **Before 09:00**: Kite login, `restart desk`, `/status` says `"authed": true`. The EIP must be
   Zerodha's registered order IP by now (docs/08 §5) or the first order is refused by Kite.
4. **09:14–10:45**: the same morning as above, except that Confirm now sends a **marketable
   LIMIT** at `min(trigger × 1.005, range high + 0.25 ADR)`, polls the order ≤ 10 s, arms a real
   GTT at the stop (limit 3 % under it) for exactly the filled quantity, and returns `SENT` (partial) or
   `FILLED`. Later fills raise the GTT through `on_order_update`. At most **3** entries, exposure
   ≤ rung 0's 25 %. `EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP` / `STOP_TOO_WIDE` are refusals,
   not errors.
5. **10:45** remainders cancelled for real · **15:15** the sweep arms a GTT on anything naked
   (`SWING_POSITION_NAKED` is the alert if it cannot) · **21:05** the evening counts the LIVE
   session, `first_live_sessions_left` → 4, the ladder reads the real close.
6. **What to watch on `/swing`**: the status bar (`authed`, `dry_run=false`,
   exposure, entries today, rung), the positions panel (every row has a GTT id that is not
   `DRY-…`), and the five `SWING_*` alerts in runbook 6. On the web hub, `/swing/journal`'s
   real card starts filling; the simulated card stays apart.

## Deploy — in order, from the laptop (run four times so far; `bf4168b` is live)

```bash
aws sso login --profile baskfy-poc
AWS_PROFILE=baskfy-poc bash tools/deploy/push-images.sh      # web, python, desk → ECR
AWS_PROFILE=baskfy-poc bash tools/deploy/tf.sh plan          # expect +1 A record (desk.), ~1 IAM policy change
AWS_PROFILE=baskfy-poc bash tools/deploy/tf.sh apply
AWS_PROFILE=baskfy-poc bash tools/deploy/deploy-swing.sh     # ships compose + Caddyfile, migrate, seed swing ₹25,00,000 / 0.5 %, up -d
AWS_PROFILE=baskfy-poc bash tools/deploy/verify-swing.sh     # 401s where expected, dry_run true, all four swing flags false, alembic at head
```

Rollback: the three `BASKFY_*_IMAGE` lines in `/opt/baskfy/.env.staging.compose` back to the
previous tag (`151ce21` now), the `.bak-sw13-<stamp>` files back, `up -d --force-recreate caddy`,
`up -d` (`deploy-swing.sh` prints the exact lines). Before the drill morning: the GoDaddy A
record `desk.staging → 3.108.148.38` (S4), `tools/swing/backtest.py` on the box for §3.3, and
the `tools/migrate-desk` decision (SW-3).

## Not done

The full list is `STATUS.md` § "Not done (kept loud)". The short form: the desk vhost is
NXDOMAIN until S4; the box's index still carries 360 synthetic rows for twelve slugs NSE never
publishes and `nifty-consumer-services` holds Kite's *Services Sector* (SW16, 42 refusals); no morning on a real session; S2 unrun;
the 2017→ backtest unrun on real bars; the desk's 43,411 rows of history unmigrated;
`sw_position` written only by tests and the drill; Playwright cannot sign in since M46;
Q-SW12-1; `backtest.py:755` (`cash_available` at a full top-rung book) has no killing test.

## Needs you

`NEEDS-MAULIK.md` § Swing, SW-1 → SW-8 and S4: the sleeve confirmed; the desk-history migration yes/no; the S2 probe morning; the DRY_RUN
drill morning; the Telegram token (optional); the flag flip; the S3 rotation call; **S4, the
GoDaddy A record for `desk.staging`** — without it the desk page has no public name. S1 is
resolved (MD3), S2 superseded by the probe.

## Caveats I will not bury

* **The code has never seen a live Kite morning.** Every morning number here is from fixtures
  and the drill. A4's timing assumptions (pre-open `volume`, the forming 09:20 candle) are the
  probe's to confirm; the range is tick-built so the answer changes at most one Beat time.
* **The drill's book is synthetic** (five names, sixty bars). It proves the machinery — the lock,
  the re-size, the partial fill, the cutoff, the sweep — and nothing about real prices.
* **The session counter still says "paper sessions"** on the page and in the email. The wording
  is information; the gate no longer reads it.
* **The desk on the box runs on an empty `desk` schema** (20 tables, 0 history) until SW-3 is
  decided. The swing book does not need the history; the weekly book's pages do. And its
  unauthenticated `/status` returns `cash` beside `dry_run` / `authed` — SW13-run's observation,
  worth closing before S4 makes the vhost public.
* **A live flip is two files and three lines, not one.** The desk reads compose defaults;
  api/worker/beat read `.env.staging`; and the swing flag alone changes nothing while the desk's
  `DRY_RUN=true` — `BASKFY_DESK_DRY_RUN=false` is the line that makes the desk (weekly book
  included) able to place. SW13-run found the box's `BASKFY_DRY_RUN` already `false` for the api
  and split the desk's variable off before the first `up`; that split is the rail now. `verify-swing.sh` asserts only the desk's copy; check the worker's with
  `box.sh '… exec -T worker env | grep BASKFY_SWING'`.
