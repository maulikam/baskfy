# 00 — Merge status

The live status page for the Baskfy merge run (`MERGE-PROMPTS.md`). Updated at the end of every
module. **Loud about what is NOT done** — both source repos keep honest open-items lists, and
that culture continues here.

**Run started:** 22 Aug 2026 · **State:** **M0 → M34**, and continuing on request.

The run reached M22 and was declared finished; Maulik then logged in to Kite, which unblocked the
path everything else was waiting on, and M23–M28 followed. **M29–M34 came after that**, each from
a direct request rather than the script: deep history, the pages that history made possible, and
the sleeve planner.

**The ledger below is in the order modules were written, not numerical order.** M22 sits last
because M23 and M24 were appended before it while the file was being edited under time pressure —
recorded rather than tidied, because a status page that quietly reorders itself is a status page
nobody can diff.

**M11 and M12 are no longer red for the reason they were.** The corporate-action history that
blocked both — `NEEDS-MAULIK.md` item 4, "the biggest blocker in the project" — was **recovered
from data already on disk** (M24) and applied (M28). `corporate_action` went from 4 rows to 289.

| | at M22 | now |
|---|---|---|
| `/baskets` "unadjusted corporate action" banner | 41 symbols | **5** |
| M12 top-25 membership | 23 / 25 | **25 / 25** |
| `SHILPAMED` in the M12 comparison | #10 → #57 | #10 → **#12** |
| Corpus exact matches — 1M / 3M / 6M | 264 / 260 / 255 | **266 / 264 / 263** |
| Corpus exact matches — 9M / 12M | 1 / 1 | 1 / 1 |

**M12's delta table is still not empty** (16 rank deltas, 14 within ±3), so rule 7 keeps M13's
generated-scan flag **off**. 9M and 12M did not move because their residual is the **window
length**, not the adjustment — the seeded calendar is short about nine lunar-calendar holidays a
year. M27 predicted that before M28 ran.

⚠️ **Do not run `make backfill`.** As built it writes Kite's *adjusted* prices into `close_raw`,
the column house rule 6 defines as the exchange print (M24.1). `baskfy_worker.deep_backfill` is
what M29 used instead, and it says what it stores.

### Where the data stands after M29–M32

| | |
|---|---|
| `ohlcv_daily` | **3,534,860 bars, 2017-01-02 → 2026-08-21** (was 1,145,922 from 2024) |
| `corporate_action` | 289 rows (was 4) |
| `index_snapshot_daily` | 2017 → today for 64 indices (was six weeks) |
| `factor_daily` | sampled weekly from 2021; **daily is a ~30-hour job** and has not been run |
| `index_member_daily` | published for last week; earlier dates **carried backwards, `source='derived'`** |

**Coverage, measured per instrument against its own listing date:** 2,263 of 2,294 reachable
instruments — **98.7%** — start within 30 days of expected. The 31 exceptions were checked against
Kite directly and it serves nothing earlier. A further **231 instruments are absent from Kite's
instrument master entirely** and cannot be reached at any depth (`NEEDS-MAULIK.md` item 11).

Start here: [`FINAL-REPORT.md`](../FINAL-REPORT.md) — what is green, what is queued, and the 58
decisions I made without asking. Then [`RUN-AND-TEST.md`](../RUN-AND-TEST.md) to run it, and
[`NEEDS-MAULIK.md`](../NEEDS-MAULIK.md) for the seven items only you can do.

Since 22 Aug 2026 this is an **autonomous run** under `CLAUDE.md` §Autonomy charter:
judgement calls are decided, recorded in `DECISIONS-MERGE.md` (tagged `⚠ UNREVIEWED`
where nobody has reviewed them) and the run continues. Anything only Maulik can supply
is queued in [`NEEDS-MAULIK.md`](../NEEDS-MAULIK.md) while work continues around it.

---

## Module ledger

| # | Module | State | Date | Note |
|---|---|---|---|---|
| M0 | Preflight, baselines, safety copy | ✅ done | 22 Aug 2026 | Baselines + verified safety copy recorded. Blocker resolved: decile's overnight work committed as `ea5dd0f` (M0.8) |
| M1 | Umbrella repo (subtree) | ✅ done | 22 Aug 2026 | One repo at the root; 117 commits; both histories reachable from HEAD. `--follow` does not prove it — see M1.1 for the commands that do |
| M2 | The rename | ✅ done | 22 Aug 2026 | 425 files, 2347+/2347−. Namespaces are Baskfy's; the D1–D6 vocabulary is kept by design. Enforced by `tools/check-namespace.sh` (M2.1) |
| M3 | Working agreement + status page | ✅ done | 22 Aug 2026 | Root CLAUDE.md absorbed the screener's nine house rules and the GTT caveat it had dropped; both sub-files marked; status page linked first |
| M4 | One env schema | ✅ done | 22 Aug 2026 | Root `.env.example`, 371 lines, every var marked (16 user-editable / 122 system-only). Four risk ceilings locked out of `/settings` (M4.1) |
| M5 | One CI workflow | ✅ done | 22 Aug 2026 | Root `.github/workflows/ci.yml`, 5 jobs. `tools/ci-local.sh` runs it here: 14 pass / 1 fail (M6's) / 5 skip. Found and fixed nested `.git`s (M5.1) |
| M6 | Freeze the strangle lab | ✅ done | 22 Aug 2026 | 16 of 20 strangle modules + 12 test files + scripts + planners frozen. Desk suite **1202 passed, 17 skipped, 0 failed** — green for the first time |
| M7 | Local stack up | ✅ done | 22 Aug 2026 | `baskfy` stack up, **backfill restored (1,138,300 bars)**, API + worker healthy. `make seed` was overwriting 25,256 real bars — guarded (M7.1) |
| M8 | NSE endpoints verified | ✅ done | 22 Aug 2026 | All 5 capabilities live-verified; **4 universes were 404ing** and are fixed (M8.1). `reconciliation/NSE-ENDPOINTS.md` |
| M9 | Kite creds + backfill | ⚠️ partial | 22 Aug 2026 | Bars current to 2026-08-21 (**1,145,922**) via bhavcopy. **Depth before 2024 needs a Kite login** — queued, not blocking (M9.1) |
| M10 | Calendar + corporate actions | ⚠️ partial | 22 Aug 2026 | Calendar observed; **3 of 5 windows exact** (was 0). §21.9 look-ahead **fixed and proven**. 3M/6M off by one → M11 (M10.1) |
| M11 | 271-row parity test | 🔴 **red** | 22 Aug 2026 | Test **runs** for the first time. Base fix landed (1M reproduces); **6,934/9,166 cells still fail** on window length. Needs a decision (M11.2) |
| M12 | Desk parity | 🔴 **red, informative — now reproducible** | 22 Aug 2026 | **23/25 top-25 shared**, unchanged when re-run through `reconciliation/desk_parity.py` after M15–M17. Gap is **missing corporate actions**, not the window |
| M13 | MomentumScan — CSV cord cut | ⚠️ **built, not switched on** | 22 Aug 2026 | Contract + `/analyze?generate_for=` + `screen_run_id`, all tested. **Upload stays the default**: a generated scan passes 223 symbols where the upload passes 239 (M13.1) |
| M14 | Breadth + shadow harness | ⚠️ **built; shadow red** | 22 Aug 2026 | P1.9 **reconciled exactly** — same 271 symbols, 68.6347% both sides. Shadow mode's first run: **4 order deltas**, one a corporate-action substitution. Flag off (M14.1–.5) |
| M15 | Desk brains → core | ✅ done | 22 Aug 2026 | score, costs, basket and the exposure overlay all in core. Outputs **verified identical**; `regime.py` byte-identical. Typing debt recorded (M15.7) |
| M16 | Execution package + broker split | ✅ done | 22 Aug 2026 | `packages/execution` created; guards/risk/ratelimit **byte-identical**; 7 non-negotiables have 16 named tests; token encrypted at rest (M16.1–M16.4) |
| M17 | Rank buffer demoted | ✅ done | 22 Aug 2026 | One band predicate, called by both; plans unchanged. **First-ever Playwright run: portfolios 4 passed** (M17.1–M17.2) |
| M18 | Database migration | ✅ **green** | 22 Aug 2026 | 19 tables / 42,285 rows, all assertions green, idempotent. **Cutover completed at M19**; forever archive taken, rehearsal copy superseded |
| M19 | Schedules → Celery, desk on merged backend | ✅ **green** | 22 Aug 2026 | Beat tasks + timer-retirement protocol. **Cutover done**: 10/11 pages byte-identical, 11th differs only in journal mode + file size. Four dialect defects fixed (M19.1–.6) |
| M20 | Observability + runbook 6 | ✅ **green** | 22 Aug 2026 | Spans/metrics/Sentry over plan→execute→GTT, all optional and unable to raise into the order path. 4 alert rules, runbook 6 committed (M20.1–.4) |
| M21 | Verification + handover | ✅ **green** | 22 Aug 2026 | `RUN-AND-TEST.md` at the root; the DRY_RUN Friday drill runs end to end with **0 orders reaching a broker**; M12 corpus unchanged through M13–M20 |
| M23 | The Kite path, on first contact | ✅ **green** | 22 Aug 2026 | Token bridge writes both stores; instrument upsert batched (10,222 tokens, was 40); a stale token no longer falls through to **fixture prices for real symbols** (M23.1–.5) |
| M24 | Corporate actions, recovered from the ratio | 🟡 **method proven, not built** | 22 Aug 2026 | Kite's bars are adjusted — docs/09 said otherwise. **85 actions over 65 symbols**, 83 confirmed. Dividend half queued as item 8 (M24.1–.4) |
| M34 | Sleeves — a portfolio run as several screens | ✅ **green** | 22 Aug 2026 | Capital split across screens plus a slice run by hand. **Amounts and weights, never unit counts** — structural, since the allocator receives no quote. Stance stated, cap opt-in (M34.1–.6) |
| M33 | The Market Health page says what it cannot show | ✅ **green** | 22 Aug 2026 | The range filter was fine; the page was not. Truncated-range notice + the survivorship-bias disclosure, both asserted over the source (M33.1–.3) |
| M32 | Breadth history | ✅ **green** | 22 Aug 2026 | The charts draw. `factor_daily` held **one date**; computing any historical date crashed on a schema M29 broke. Sampled weekly: 45s of computing per date (M32.1–.5) |
| M31 | Index and sector levels | ✅ **green** | 22 Aug 2026 | 136 NSE indices from Kite, **146,049 rows**, 2017→today. A test caught `PSE`≠`PSU BANK` in the abbreviation table (M31.1–.3) |
| M30 | The basket, precomputed | ✅ **green** | 22 Aug 2026 | `/baskets` **67s → 0.21s**. Step 11, after publish, and a cache that can never fail the run (M30.1–.5) |
| M29 | Deep history from Kite | ✅ **green** | 22 Aug 2026 | **1.1M → 3.53M bars, 2017-01-02 onward.** Not via `make backfill`, which would have written adjusted prices into `close_raw` (M29.1–.7) |
| M28 | Corporate actions applied | ✅ **green** | 22 Aug 2026 | **285 actions written, 151,922 bars rebuilt.** Banner 41→5, M12 23/25→**25/25**. Two defects found by reading the prices, not the insert (M28.1–.5) |
| M25 | Baskfy, everywhere | ✅ **green** | 22 Aug 2026 | The app said "Decile" in 35 strings, its email, its invoices and four legal docs. Renamed, `X-Decile-*` headers included; a brand gate found 24 more (M25.1–.4) |
| M26 | The desk's record on the web app | ✅ **green** | 22 Aug 2026 | Performance, Holdings, Trades, Market stance, Plan vs fills — Next.js, plain language, read-only asserted twice more. Live-broker pages stay in the console pending D3 (M26.1–.5) |
| M27 | The dividend question, measured | ✅ **green** | 22 Aug 2026 | **Price return.** 42–3, and **25 of 25 exact** on 1M/3M/6M. Confirms M11's base fix and M24's splits for free (M27.1–.4) |
| M22 | The merged face (read-only) | ✅ **green** | 22 Aug 2026 | `/baskets` and `/baskets/plan` on real data. Every mutation **405**; two suites hold it read-only. Desk suite untouched |
| M41 | Broker connect catalog | ✅ **green** | 23 Aug 2026 | Ten-broker `/brokers` grid + gated OAuth. Live redirects held on D3 (`BROKER_OAUTH_REVIEW`). CSV import remains the holdings path (M41.1–.3) |

---

## ⛔ Open blocker — M0 acceptance not met

`decile-blueprint` has **7 uncommitted entries** on branch `overnight-20260821-0455`.
M0 requires both sub-repos clean, and M1's `git subtree add` takes **committed content only** —
so proceeding would silently revert the 5 modified tracked files while rsync restores only the
2 untracked ones. Full detail and the question for Maulik are in the M0 report; nothing is
touched until he answers.

### What the blocker actually contains

The 7 entries are not stray edits. They are an **overnight run that got partway through what
M8–M11 describe** (`docs/DECISIONS.md` gains a "Prompt 21 — The real backfill" section, §21.1–21.10),
loaded real NSE bars, and ran the parity comparison. Its findings:

- **§21.7 — the return window is off by one bar, and `docs/05` §1 is the bug.** Measured over all
  271 export rows on real bars, the reference product's base is `P_{t-(N-1)}` (N bars inclusive of
  both endpoints), not `P_{t-N}`. At our shift the exact-match count is **0, 1, 0, 0, 0**; at the
  reference's it is **90–98%**. Everything downstream — `sharpe_N`, `vol_N`, `rsi_N`, `high_1y`,
  `away_from_high_1y` — fails 271/271 today for this one reason. The engine was deliberately **not**
  changed: `docs/05` is the source of truth, so it needs a hand-edit first, then a one-line change
  in `factors.py`.
- **§21.9 — `apply_adjustments` applies actions with a *future* ex-date.** That is a look-ahead, and
  decile's House Rule 5 is "No look-ahead, ever."
- **§21.10 — adjusting `open`/`high`/`low` is destructive**; there is no raw counterpart stored.
- **§21.1–21.3 — bars come from the NSE bhavcopy, not Kite** (new `bhavcopy_backfill.py` + `make
  bhavcopy`), with resumability as per-day upsert rather than `ingest_cursor`; **§21.2 says the
  bhavcopy cannot serve `docs/09`'s fifteen years.**
- **§21.6 — `factor_daily`'s upsert chunk was sized in rows, not bind parameters**, and a real
  ~2,400-instrument day blew PostgreSQL's 32,767-parameter cap. Fixed in `factors.py`.
- `test_reference_parity.py` (+297 lines) corrects a false claim: pointing `DECILE_PARITY_BARS` at
  real bars was **not** sufficient. `beta` needs a benchmark passed as an argument, `marketcap` is
  an *input* never computed, `high_ath` is a `cum_max` wearing an all-time label on truncated
  history, and five `circuits_*` columns were being dropped through an absent dict key.

**This changes M9 and M11 as written.** M9's Kite HUMAN GATE may not be needed for bars; M11's
"all 93 columns green" is not reachable until `docs/05` §1 is corrected and the missing inputs are
supplied. Raised under rule 5 and settled: **M11's criterion is amended at M11, with real numbers
in hand, for Maulik's approval — not weakened silently and not amended speculatively now**
(`DECISIONS-MERGE.md` M0.9). Neither the `docs/05` §1 hand-edit nor the `factors.py` one-liner has
been done; both are M10/M11 work, in that order.

The local stack is **already up and holding data**: `decile-postgres` (timescaledb) and
`decile-redis` have been running 29 hours on volume `decile_decile-pgdata`, and §21.4 records that
the synthetic fixture bars were deleted before the real load. M7 will not be starting from empty.

---

## Baselines (M0)

Recorded 22 Aug 2026 on macOS 24.0.0 (darwin, arm64).

### Repository state at run start

| | `decile-blueprint` | `kite-momentum-rebalancer` |
|---|---|---|
| HEAD | `eef67d11858d2fff266d807c5701bf3b1fef6d38` | `df6cb7270f4e359c650c91d061ec14b3db723104` |
| Branch | `overnight-20260821-0455` | `indices-board` |
| Working tree at start | dirty — 7 entries (now committed as `ea5dd0f`) | clean |

### Test baselines

| Suite | Result |
|---|---|
| decile — `uv run pytest` | **1385 passed, 794 skipped, 0 failed** (82s) |
| decile — `pnpm -r run test` (vitest) | **405 passed, 24 files, 0 failed** (4.8s) |
| decile — `make lint` | **exit 0** — mypy strict clean (253 files), tsc clean, eslint 1 warning / 0 errors |
| desk — `pytest`, repo-default env | **1524 passed, 1 failed** (44s) |

The 794 decile skips are the `db`- and `redis`-marked suites plus the network-blocked parity
test — expected without `make up` (that is M7's job).

**The one desk failure** is `tests/test_strangle_runtime.py::test_both_plists_are_valid_and_point_at_this_checkout`.
It asserts a committed plist's `WorkingDirectory` equals `os.getcwd()`; the plist hardcodes the
tree's pre-move path. A relocation artifact in the strangle subsystem, which **M6 removes from
every gate** — so it is recorded as the baseline of one, not fixed. (`DECISIONS-MERGE.md` M0.5.)

### Tooling

`git` 2.39.5 · `uv` 0.11.21 · CPython **3.12.13** via uv · `node` v25.2.1 · `pnpm` 11.22.0 ·
`npm` 11.17.0 · `docker` 29.1.2 + compose v2.40.3 (daemon running) · `sqlite3` 3.43.2 ·
`rsync` (openrsync). No bare `python3.12` on PATH — see `DECISIONS-MERGE.md` M0.1; not a blocker.

Both venvs were rebuilt in place: they carried absolute paths to their pre-merge locations and
the desk's was activating a *different checkout's* environment (`DECISIONS-MERGE.md` M0.2).

---

## Safety copy — verified

| | |
|---|---|
| `python -m scripts.backup` | reported `integrity: ok`, exit 0 |
| In-repo backup | `kite-momentum-rebalancer/data/backups/20260822-000953/` |
| **Offsite copy** | **`~/baskfy-safety/2026-08-22/data/`** (24 MB, 851 files) |
| Copy verified | `pragma integrity_check` → `ok` on the *copied* database |
| Row counts (live == copy) | trades 8198 · fills 9262 · rebalance_orders 133 · snapshots 8 · regime_evaluations 13 |

---

## How to prove the histories survived (M1)

`git log --follow -- <prefixed-path>` returns **0** and always will: `git subtree add` does not
rewrite historical paths, so pre-merge commits still name `app/scoring.py`, not
`kite-momentum-rebalancer/app/scoring.py`. Use these instead (`DECISIONS-MERGE.md` M1.1):

```
git merge-base --is-ancestor df6cb72 HEAD    # desk tip        -> yes
git merge-base --is-ancestor ea5dd0f HEAD    # decile tip      -> yes
git merge-base --is-ancestor 36d6ba1 HEAD    # decile's root   -> yes
git rev-list --count HEAD                    # 117
git log --oneline df6cb72 -- app/main.py     # 29 commits of desk history
git log --oneline ea5dd0f -- docs/DECISIONS.md   # 9 commits of decile history
```

## The local stack — how to bring it up (M7)

```bash
cd decile-blueprint
make up                       # baskfy-postgres (timescale), baskfy-redis, baskfy-mailpit
make migrate                  # already at head (0010) if you restored the dump
make seed                     # idempotent; it will NOT touch bars that already exist
make integrity                # 1 known failure — see below
make doctor                   # nse + fixture OK, kite needs a token (M9)
uv run uvicorn baskfy_api.app:get_app --factory --port 8000   # or `make api`
uv run celery -A baskfy_worker.celery_app:app worker -Q ingest,compute,backtest,default -l info
```

**Routes are under `/api/v1`** (`/health` is the exception). `GET /api/v1/meta/status` currently
answers `as_of 2026-08-18, data_version 1, degraded false` from the restored backfill.
**Port 8000 is often already taken on this machine** by an unrelated python process — if `/health`
answers 200 but `/api/v1/*` 404s, you are talking to something else. Use another port.

### Restoring the backfill dump — the order matters

Getting this wrong is **silent**: `pre_restore` fails, the restore looks fine, and `factor_daily`
comes back with no primary key, which then breaks every upsert.

```bash
docker exec baskfy-postgres psql -U baskfy -d postgres -c "CREATE DATABASE baskfy;"
docker exec baskfy-postgres psql -U baskfy -d baskfy -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "SELECT timescaledb_pre_restore();"
docker exec -i baskfy-postgres pg_restore -U baskfy -d baskfy --no-owner --no-privileges \
  --disable-triggers < ~/baskfy-safety/2026-08-22/pg/decile-preM2.dump
docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "SELECT timescaledb_post_restore();"
```

Then **re-add the three user policies by hand** — `bgw_job` cannot restore (it references the
pre-rename role), so the compression policy on `ohlcv_daily` and the two continuous-aggregate
refresh policies must be recreated from migrations `0001` and `0008`. Without that the aggregates
stop refreshing silently. (`DECISIONS-MERGE.md` M7.2.)

### Two things that are known-not-green

- **`make integrity` fails one check:** `published_runs_have_steps`. The overnight backfill
  published `data_version 1` without going through the orchestrator, so no `pipeline_run_step`
  rows exist. It came from the dump. **M9/M10 should produce a run that carries its steps**; if it
  still fails after that, it is a real defect (`DECISIONS-MERGE.md` M7.3).
- **`make doctor` reports kite unavailable** — no encrypted token. That is M9's external
  dependency. `nse` and `fixture` both serve everything.

## ⚠️ Superseded — M7 restored the backfill (kept for the record)

`decile-postgres` holds the overnight backfill, and M2 renamed the database in **configuration
only** — the live cluster was deliberately left alone:

| | |
|---|---|
| `ohlcv_daily` | **1,138,300 rows**, 2,546 instruments, 2024-01-01 → 2026-08-18 |
| `instrument` / `index_member_daily` / `trading_day` | 2,553 / 16,633 / 5,844 |
| Verified dump | **`~/baskfy-safety/2026-08-22/pg/decile-preM2.dump`** (30 MB, custom format, 76 table-data entries, `pg_restore -l` verified) |

**M7 must restore that dump into the new `baskfy` database rather than migrating an empty one.**
`make up` under the renamed compose creates a *new* volume (`baskfy_baskfy-pgdata`); the 1.1M bars
stay in the old `decile_decile-pgdata` — recoverable, but only if someone remembers they are there.
That backfill is hours of NSE fetching and it is what M10–M12 are graded on.

Note the range is 2024-01-01, **not** D5's 2011: `decile-blueprint/docs/DECISIONS.md` §21.2 records
that the bhavcopy cannot reach `docs/09`'s fifteen years. Whether Kite is wanted for the deeper
history is an open question for M9 (`DECISIONS-MERGE.md` M0.9).

## The namespace rule, in code

`tools/check-namespace.sh` is the check — the raw grep in M2's text over-reaches into vocabulary.
It allows exactly `decile_1`…`decile_6` (the D1–D6 public API values), `DECILE_RANK_KEY` and
`decile_bucket`, and excludes both trees' `docs/` plus the two documents that instruct the rename.
Run it from the repository root; it exits non-zero and prints `file:line: token` on any violation.

## Running the browser suite locally

`playwright.config.ts` migrates and seeds `baskfy_e2e` but does **not create** it. CI does that
explicitly; nothing local did, which is why the suite had never run. Create it once:

```bash
docker exec baskfy-postgres psql -U baskfy -d postgres -c "CREATE DATABASE baskfy_e2e;"
docker exec baskfy-postgres psql -U baskfy -d baskfy_e2e -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
pnpm --filter @baskfy/web exec playwright install chromium
pnpm --filter @baskfy/web exec playwright test e2e/portfolios.spec.ts --reporter=line
```

**`e2e/portfolios.spec.ts` passes: 4 tests, 33s** (M17). The other nine journeys are still
unexecuted.

## The token bridge (point 2, after M18)

`make token-sync TARGET=momentum-desk` pulls the token the box minted into the laptop's encrypted
store and verifies it with one `profile()` call, without printing it. **The Kite app's Redirect URL
stays `desk.modelbasket.in/callback`** — nothing about the live app changed.

Exercised against the real box this morning: it read Friday's token, refused to store it, and
correctly reported the token as expired rather than blaming the api_key. **One daily human login is
still required and always will be** (NEEDS-MAULIK 3), so **M9's deep backfill and M10's
corporate-action work stay queued**. New: NEEDS-MAULIK 6, a one-time check that the app carries the
paid historical-data tier — `profile()` succeeding does not prove bars can be pulled.

## ✅ THE DIVERGENCE WINDOW IS CLOSED (M19, 08:30 IST)

The sequence below was run exactly as written. The re-run earned its place: Postgres held 13 plans
and SQLite held 91, and the gap showed up as a wrong plan id on `/regime` rather than as an error.

The forever archive is `~/baskfy-safety/sqlite-archive/portfolio-2026-08-22T08-30-IST-FINAL-pre-postgres.db`
(0444, integrity ok, taken through the sqlite backup API). The 06:48 copy is renamed
`SUPERSEDED-rehearsal-…`.

**The desk reads and writes Postgres locally.** The code default is still `sqlite`, because the box
has no Postgres — see M19.2.

<details><summary>The sequence, as it was specified at M18</summary>

## ⚠️ THE DIVERGENCE WINDOW (M18 → M19)

**The `desk` schema in Postgres is rehearsal output, not truth.** The desk still writes SQLite;
nothing under `app/` reads Postgres. That projection is a faithful copy of the database as it stood
at **06:48 IST on 22 Aug 2026** and is stale the moment the desk writes another row.

**Do not read it as current, and do not archive today's SQLite as the forever copy.**
`~/baskfy-safety/sqlite-archive/portfolio-2026-08-22-pre-postgres.db` is a *rehearsal* archive.

The real sequence is M19's, in this order:

1. stop every desk writer (web service + daily/autorun timers);
2. re-run `scripts/migrate_to_postgres.py --drop-existing` against the live file (proven idempotent);
3. full assertions again — counts, checksums, NAV recompute; nothing commits unless all pass;
4. switch the desk's backend to Postgres;
5. **only then** archive that SQLite state read-only as the forever copy, superseding the rehearsal one.

Precondition (c)'s one-command rollback becomes real at step 4 — today "point the desk back at
SQLite" is the status quo, not a rollback.

</details>

## Two open defects the root agreement now carries

M3 folded the screener's house rules into the root `CLAUDE.md`, and two of them are **currently
violated** — recorded there with ⚠️ so nobody reads the rule as a description of the code:

- **House rule 5, "No look-ahead, ever"** — `apply_adjustments` applies corporate actions with a
  *future* ex-date (`decile-blueprint/docs/DECISIONS.md` §21.9). **M10 owns the fix.**
- **House rule 6, "Adjusted by default"** — adjusting `open`/`high`/`low` is destructive because
  no raw counterpart is stored (§21.10).

And one documented exception to non-negotiable 6, carried from the desk's own wording: GTT stops
go through `kite_client.place_gtt_stop` with its own guard, not through the gateway, which has no
GTT method. **M16 owns closing that.**

## The risk ceilings are no longer editable from the browser (M4)

`RISK_MAX_DAILY_LOSS_PCT` (the kill switch), `RISK_POSITION_HEADROOM`, `RISK_GROSS_MULTIPLE` and
`RISK_MAX_ORDERS_PER_DAY` moved to `.env`-only. `docs/03` §3f names this exact case. Nothing in
force changed — no `RISK_*` override had ever been stored — and the ceilings are still *displayed*
read-only, plus logged at every startup to replace the audit row that locking removed.

**This reaches the live Mumbai box only on its next deliberate `git pull`, and that should be
outside market hours.** Queued as item 1 in [`NEEDS-MAULIK.md`](../NEEDS-MAULIK.md).

## CI, and how to run it here

`.github/workflows/ci.yml` **at the repository root** is the workflow GitHub runs — 5 jobs:
`python`, `client`, `web` (the screener's, with `working-directory: decile-blueprint`), plus
`desk` and `namespace`. **`decile-blueprint/.github/workflows/ci.yml` is now INERT** — GitHub
reads only the root — so edit the root one; the sub-tree copy is kept because it is the screener's
own history.

`tools/ci-local.sh` runs every step that can run on this machine and prints `SKIP — <reason>` for
the rest. Five skips today: the coverage gate and the query-plan baseline need the M7 stack; three
web steps need Playwright browsers, a seeded `baskfy_e2e` and a production build.

**M1 left a live `.git` inside each subtree** (rsync `--ignore-existing` re-imported it, because
`git subtree add` leaves none). Any git command run from inside a subtree resolved to the orphaned
pre-merge repo — it silently failed a CI step and could have swallowed a commit. Both were moved
to `~/baskfy-safety/2026-08-22/nested-git/` after verifying the rollback copies and that both tips
are ancestors of HEAD. **If you repeat M1's recipe anywhere, add `--exclude=.git`.**
(`DECISIONS-MERGE.md` M5.1.)

## The options lab is frozen (M6)

`frozen/strangle/` — 68 files, 43 modules, 12 test suites. Read `frozen/strangle/README.md`
before touching anything under it; the safety rails say it is untouched, and a repository-wide
sweep must exclude the directory rather than edit a file inside it.

**Four modules did not freeze.** `calendar_nse`, `clock`, `instruments` and `config` stayed in
`kite-momentum-rebalancer/app/strategies/strangle/` because `scripts/autorun.py` derives the NSE
trading-day answer from them, and autorun runs on every login collecting data that cannot be
backfilled. They are dependency-free and cannot reach an order path.
**M15 should fold these into `baskfy_core.trading_calendar`** — an NSE calendar has no business
living in a package named after an options strategy (`DECISIONS-MERGE.md` M6.2).

**Two things deliberately stayed behind**, both box concerns rather than repo state:
`deploy/systemd/strangle-collect@.{service,timer}`, and `data/outputs/strangle_*` (the
observation series, which is not rebuildable). **The live box runs the pre-freeze layout until
its next deliberate deploy, at which point those units will name paths that have moved.**

## The `db`-marked suites now run (and did not before)

792 tests were skipped at the M0 baseline and had **never been executed**. With the stack up they
run. One shared fixture bug was failing most of them: `DROP SCHEMA public CASCADE` does not reset a
TimescaleDB database, because chunks and materialisations live in `_timescaledb_internal` and
survive it, keeping foreign keys that point at dropped tables. The reset now drops the extension
too (`f93f690`, `DECISIONS-MERGE.md` DB.1).

To run them:

```bash
export BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test"
export BASKFY_REDIS_URL="redis://localhost:6380/0"
uv run pytest -m db
```

`uv run` does **not** load `.env`, so exporting these is required — without them every db test
skips with a message that reads like the stack is down.

## Things a future session must know

1. **The desk's `.env` is LIVE** — `DRY_RUN=false`, commented "real orders". It is untracked
   operator config and was deliberately not changed. Anything that boots the desk from this
   directory inherits a live-order configuration; pass `DRY_RUN=true` explicitly.
   (`DECISIONS-MERGE.md` M0.6.)
2. **Two byte-identical copies of the desk exist** — `~/Documents/portfolio/kite-momentum-rebalancer`
   and this one. Same HEAD, same branch, identical `app/`, identical `portfolio.db` (md5
   `ae13b32f…`). `baskfy/` is the merge source. Which one the Mumbai box syncs from is Maulik's
   call. (`DECISIONS-MERGE.md` M0.7.)
3. **Decile's suite prints no summary line if you pass `-q`** — `addopts` already carries it, and
   two `-q`s means `-qq`. Run `uv run pytest` bare.
4. `frozen/` does not exist yet; the strangle subsystem is still in `app/strategies/`. M6 moves it.
5. **M0 produced no root commit** — there was no git repository at `baskfy/` yet. Its two
   deliverables (`docs/00-merge-status.md`, `docs/DECISIONS-MERGE.md`) are committed as part of
   M1's initial root commit, which is what `MERGE-PROMPTS.md` M1 step 2 specifies.
6. `decile-blueprint` HEAD moved from `eef67d1` to **`ea5dd0f`** during M0. The subtree in M1
   carries the later commit.
7. **`../_baskfy_subtree_tmp/` is the rollback** and must survive until M21. It holds both
   pre-merge repositories exactly as they were.
8. `baskfy/` is a git repo **nested inside** the one at `~/Documents/projects` (which tracks
   nothing here). Git commands run from inside `baskfy/` resolve to the new repo, as intended.
9. M1 restored untracked state by `rsync --ignore-existing` from the rollback copy. Verified
   afterwards: `portfolio.db` md5 `ae13b32fb9069e62327b02e712139bde` unchanged, integrity `ok`,
   8198/9262/8/133 rows, the 6-CSV `data/uploads/` corpus intact, and **no `.env` in any commit**.

### M37 — the redesign, the plain English, and the mark ✅

The web app no longer looks like the product it was reproduced from, and it says what it means.

**The look.** `--accent` is the ink itself: buttons, links and the active state are near-black on
warm paper, and green and red appear only when a number went up or down. The **left sidebar is
gone** — navigation is a two-row bar along the top, which is the largest deliberate departure from
docs/08 in the merge (`DECISIONS-MERGE.md` §M37.3). Type is Bricolage Grotesque for headings, Geist
for interface, Geist Mono for **every figure on the site**.

**The words.** `apps/web/src/lib/vocabulary.ts` is the one place jargon becomes English — "Above
200 DMA" renders as "Above their one-year trend", with the professional name and an explanation one
hover *and one tab-stop* away. Nothing was renamed away; the two swapped places.

**The mark.** The woven-basket logo now ships across `src/app/icon.png`, `apple-icon.png`,
`public/brand/`, both OG cards, and the desk console's Jinja templates. `brand-src/build_brand.py`
rebuilds the set from the master; the master itself is gitignored.

**Open, and honest about it:**

1. **The 16px favicon is a crop, not the mark.** The whole weave turns to mush at actual size —
   measured, not assumed. 64px and above is the full mark; 48/32/16 and every `.ico` entry are the
   strongest ribbon-crossing (`DECISIONS-MERGE.md` §M37.7).
2. **The brand orange is 2.54:1 against the light canvas as a graphical fill**, marginally under
   WCAG 1.4.11's 3:1. The active nav tab carries a hairline and `aria-current` so the colour is
   never the only "you are here" signal. Worth a second look if the canvas ever lightens.
3. **Answer-first is on Market mood only.** `components/shell/answer.tsx` states a page's finding
   in a sentence above its evidence, and it is the change that most alters what a page is *for*.
   The other surfaces still open with their measurements.
4. **`DESIGN.md` was replaced three times during this module.** §M37.1 records the rule that came
   out of it: read that file as a token architecture, never as a look to reproduce.
5. **The desk console got the favicon and the brand mark, and nothing else.** Its palette is still
   the smallcase-derived teal/blue; the two faces now share an icon, not a design.

### M38 — the supplied vector is now the source of the whole brand ✅

`logo.svg` at the repo root drives everything: `scripts/build-brand-svg.mjs` optimises it and
rasterises a transparent 4096px master, `brand-src/build_brand.py` cuts that into every icon, and
both the web app and the desk console draw the vector on screen. M37's white-background matte is
deleted — the vector's channels are genuinely empty.

**Two calls reversed or recorded:**

1. **M37's 16px crop is reversed.** Re-measured against the vector: 16px keeps a silhouette and
   32px is unambiguously the mark, while the crop is sharper and reads as anonymous stripes. One
   icon at every size (`DECISIONS-MERGE.md` §M38.3).
2. **The trace has a visible artefact above ~400px** — ribbon overlaps become hard-edged blocks,
   because an autotrace cannot express the original's translucent shadow. Left alone on purpose;
   sub-pixel at every size we render. **Regenerate the trace before any large or print render**
   (§M38.2).

### M41 — broker connect catalog ✅

Ten brokers on `/brokers` (Zerodha, HDFC, Kotak, ICICI, Upstox, Angel One, Groww, Fyers, 5paisa,
Dhan). Click a tile → three-step flow → Connect. Live OAuth is source-gated
(`BROKER_OAUTH_REVIEW.signed_off = False`) until D3 is written — same pattern as the public API.
`POST /brokers/{id}/connect` never returns a redirect while the gate is shut and never stores a
token. That is Track C in `docs/smallcase/02` and Phase 4 in `docs/05`.

**NOT done:** live redirects, per-user token encryption, holdings sync from any broker, official
logo licences, HDFC/Kotak/ICICI app registration. Queued as NEEDS-MAULIK item 13.
