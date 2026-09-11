# What is left — the honest remaining list (12 Sep 2026)

One page, written because the laptop is out of memory and the run needs to be sequenced around
that rather than pushed through it. Branch `developer`, HEAD `43b34dd`.

This file is a **snapshot**, not a status page. `docs/00-merge-status.md` and `docs/twt/STATUS.md`
stay authoritative for their own runs; `NEEDS-MAULIK.md` stays authoritative for what only Maulik
can supply. Where this file and one of those disagree, they win and this file is the stale half.



## 1. The laptop's memory — diagnosed and largely fixed (12 Sep 2026)

### What it was

| | |
|---|---|
| RAM | 16 GB |
| Free space on `/` | **37 GB** |
| `Docker.raw` (Docker Desktop's VM disk) | **130 GB used**, 460 GB apparent |
| Docker images | 240 images, 99.26 GB |
| Docker build cache | 32.98 GB, **zero of it active** |
| Containers auto-starting with Docker | **17**, all on `unless-stopped` |

**Two causes, and the second one was the surprise.**

1. **214 of the 240 images were per-commit ECR build tags** — 86 `baskfy-web`, 86 `baskfy-py`,
   42 `baskfy-desk`, one per commit, never cleaned up. Plus 33 GB of build cache with nothing
   active in it.
2. **Seventeen containers were auto-starting on every Docker launch** and none was needed for
   this work: eight from a local `baskfy-staging` replica of the box, nine from a **Supabase stack
   belonging to a different project**. They all carried `unless-stopped`, so stopping them by hand
   never held. This, not the disk, was the RAM drain.

Found on the way: `baskfy-staging-caddy-1` had been **crash-looping every 60 seconds** on a
malformed Caddyfile — *"server block without any key is global configuration, and if used, it must
be first."* It restarted forever and nothing reported it.

### What was done

| Action | Result |
|---|---|
| `docker builder prune -af` | 32.98 GB → **0** |
| Removed 205 stale per-commit tags, keeping the newest 3 per repo | 99.26 GB → **20.72 GB** |
| Removed 6 stale stopped containers (the `decile-*` stack, `supabase_edge_runtime_app`) | — |
| Removed 7 dangling volumes, then `decile_decile-pgdata` on Maulik's call | 7.35 GB → **5.96 GB** |
| Set **every** container's restart policy to `no`, then stopped all 17 | **0 containers running** |
| Removed the 9 Supabase containers and all 16 Supabase image tags (Maulik's call) | 20.72 GB → **11.66 GB** |

**Result: host free space 37 GiB → 146 GiB. `Docker.raw` 130 GB → 20 GB**, which it reclaimed on
its own; no manual shrink was needed.

**Still outstanding, and it needs a human click.** Docker Desktop reserves **5.00 GB of RAM at
0.00 % CPU with zero containers running** — nearly a third of a 16 GB machine held by an idle VM.
Settings → Resources → Memory **4 GB**, CPUs **2**, Apply and Restart. Quit Docker Desktop
entirely when no database test is running.

### What was deliberately NOT deleted

- **`baskfy_baskfy-pgdata`, 5.76 GB — the plant.** 3.5M bars, 2017→2026. Verified intact after
  every step. Volume pruning was done by name, never with `docker volume prune`, precisely so this
  could not be swept up.
- **`supabase_db_app` and `supabase_storage_app` volumes.** The Supabase *images and containers*
  were deleted; its **data was not**. `supabase start` recreates the containers against these
  volumes after re-pulling. Verified present after the removal.
- `baskfy-staging_*` and `creatorboard-*` volumes — other stacks' data, stopped but not destroyed.
- The `latest`, `dd9cc73` and one further tag per ECR repo. `dd9cc73` is **what the box is running
  today** (§3), so it stays local. Everything deleted is re-pullable from ECR.

### The standing rules this leaves behind

- **Nothing auto-starts any more.** Bring a stack up deliberately: `make up` for the dev Postgres,
  `docker compose up` for the rest. Quit Docker Desktop entirely when no container is needed.
- **Prune the commit tags periodically.** They accumulate one per deploy and this is what 130 GB
  looks like after a few months.
- **Fix the staging Caddyfile** before that stack is brought up again, or it resumes the loop.

### The process side, added 12 Sep 2026 — the disk was only half of it

The clean-up above was about Docker's disk and its idle VM. A second screenshot the same night
showed the other half: **three vitest pools alive at once and about fifteen python processes from
the API server and the Playwright harness**, none of them waited on by anybody, with 15.42 GB used
and 18.57 GB of swap.

| | |
|---|---|
| `tools/reclaim-ram.sh` | lists the repo's orphaned test and dev processes; `--kill` reaps them. It matches a process only when **lsof says its working directory is inside this repo**, so another project's vitest is never touched |
| Both `vitest.config.ts` files | fork pool capped at **4** (`VITEST_MAX_FORKS` overrides). Vitest defaults to one fork per core, which on this 10-core machine is ten jsdom processes |

Measured on the web suite, 166 files / 2,978 tests: **2,511 MB peak at ten forks, 1,216 MB at
four**, 25 s against 31 s. Half the memory for six seconds.

Still outside the repo and worth doing: **Adobe Creative Cloud runs ~700 MB across four helpers
from login onward.** Removing it from login items gives back about what a whole vitest pool costs.

### The three heavy things in this repo. Never run two at once.

- **Docker + Postgres on 5433** — needed only by the `db`-marked tests. Note the trap recorded in
  `docs/twt/STATUS.md`: with the daemon down, a db-marked suite turns into a silent green *skip*.
- **The web suite** — 2,900+ vitest tests, plus Playwright's Chromium for the e2e specs.
- **`uv run mypy --strict`** — walks 644 source files.

---

## 2. TWT — code complete, and the only thing left is Maulik's hand

**Closed 12 Sep 2026 — and this paragraph was wrong when it was written.** It claimed
`gates/twt-root.md` R0–R13 was "filled with evidence". **Seven of the fourteen rows were unchecked
with `EVIDENCE: pending`**: R6, R7, R9, R10, R11, R12 and R13. The previous session *repaired* those
seven CHECK lines — correctly, and the repairs are the reason they pass — and then never executed
them, and wrote this summary as though it had. `gates/twt-10.md` was 7/8 with evidence, not 8/8:
G8's own evidence line read `pending`, and a checked box whose evidence reads pending is unmet.

The numbers this section already quoted were all accurate — TW6 11/11, TW7 6/6, TW9 8/8, TW10 7/7,
the runbook 8/8, the desk suite 1,984 passed and 17 skipped in 71.83 s. What was missing was not the
work. It was the recording, and a summary written from memory instead of from the ledger it was
describing.

**Running the seven found seven more of the same bug, and then nineteen more underneath.** R0, R1,
R2, R3, R4, R5 and R8 carried the *identical* impossible EXPECT and were all marked green — the file
diagnosed the bug in its own preamble and left seven live instances of it. Re-running all eleven
rows through `tools/gates/rerun.py` then surfaced **nineteen failing child checks**: anchors missing
the `m` flag, EXPECTs spanning lines with `.*`, a multi-line `python -c` that died on a syntax error
before testing anything, grep windows that had drifted as their commands' output grew, a float
filter reading the study's Sharpe ratio as money, and a seam gate still demanding `NOT READY` after
TW2 closed the seam. Eighteen are repaired with a dated reason beside each.

**Two were not check bugs, and they are why this was worth doing:**

1. **`docs/twt/06`'s TW6a had no `**Goal:**` line** — twelve module headings, twelve ACs, eleven
   goals — against `06`'s own convention that every module carries both. Written.
2. **`make lint` was red across the entire repository**, and had been. See §4: two errors in the UI
   tree's uncommitted test files, failing every lint gate in the repo including three TWT gates that
   had been recorded green before the breakage landed.

**Measured after all of it, 12 Sep 2026:** `gates/twt-root.md` **13/13 checks re-run, 0 failed**
(R12 skipped and separately green); the whole pack **15 files, 151 gates, 0 incomplete**; both
suites green at **7,651 passed / 6 skipped** and **1,984 passed / 17 skipped**.

All eleven units are green, and `TW-FINAL-REPORT.md` is written. `docs/twt/STATUS.md` is the full
ledger.

**This section used to say three TW10 gates were unmet and that `tools/twt/drill.py` did not
exist.** Both had been done before this page was next read; the page was the stale half. That is
the second time in this run that a document outlived the fact it described, and the root
`CLAUDE.md` rule applies — find which is the later fact, fix the stale half, say which it was.

### What closed it

| | Measured 12 Sep 2026 |
|---|---|
| `gates/twt-10.md` | **8/8 with evidence** — 21 safety-property tests, and under mutation (the flag flipped true) 7 of the 21 go red. *Was 7/8 until 12 Sep: G8 asserts the whole ledger, so it could not be filled until the root's seven rows were* |
| `tools/twt/drill.py` | a whole session in DRY_RUN: ratchet **80.00 → 104.00**, sweep re-armed 1 and left 0 naked, **`0 orders reached a broker`** |
| The four module rows re-run | TW6 **11/11**, TW7 **6/6**, TW9 **8/8**, TW10 **7/7**, runbook **8/8**, zero failures |
| Desk suite | 1,984 passed, 17 skipped, 72 s |
| R11 | `flag_true=0 live_orders=0 capital_seed=1` |

### What the ledger itself turned out to be

**Eight more CHECK lines could never have passed** — four in `gates/twt-10.md` and the four module
rows of `gates/twt-root.md`, which expected a string (`0 unchecked`) the unlazy checker never
prints. Two repairs are worth carrying forward because they are about the tooling rather than this
run:

* **`gate-check.mjs` drops a lone file argument** and falls back to every `gates/*.md` in the tree;
  since `gates/twt-root.md` invokes the checker, it recurses without bound. Use
  `--status <file>` or `--timeout N <file>`. The bug is in the skill, outside this repo.
* **A parent row that re-verifies a child must re-execute the child's checks.** The checker only
  re-runs gates it already believes unmet, so against a finished file it reports the *file* is
  complete, which says nothing about the module still being green. `tools/gates/rerun.py` does the
  re-execution; `tools/gates/ledger.py` reports which files are incomplete and names any
  self-referential gate it had to exclude.

### What re-reading the runbook turned up

`docs/twt/02` §3 makes `FIRST-LIVE-MORNING.md` a **condition**, so it was re-read command by
command rather than taken as green because its gate file was.

- **Two commands could never have run.** The DRY_RUN drill (§2.1) and the standalone sweep (§9.2)
  both invoked `uv run` from the **repo root**, where there is no `pyproject.toml`, so both died
  on `ModuleNotFoundError: No module named 'sqlalchemy'`. The drill is the single command a person
  runs cold, at night, alone, and `0 orders reached a broker` is the sentence he is told to look
  for. Both corrected; `make twt`'s stale `[NOT YET REAL]` marker cleared.
- **The sleeve could not be funded** — no `/api/v1/twt/config` route, no `me/twt` page, no
  `--capital` flag on `seed twt` though `seed swing` has had one since SW13. §2.2's three markers
  were always honest; nothing had added them up. **NEEDS-MAULIK T3 and DECISIONS-TW TW10.3**, and
  deliberately not built in that run.
  ✅ **The smallest of the three was built on 12 Sep (TW11)** once Maulik had seen the choice, which
  is the condition TW10.3 named for its own reversal: `set_twt_sleeve` beside `set_swing_sleeve`,
  `--capital` widened to `swing|twt`, writing through the same audited `apply_patch`. The route and
  the web form remain unbuilt and are not blocking.

### What TWT still does NOT have

- **The ratchet has never executed outside the drill**, which is a simulation against a throwaway
  database. No paper phase was chosen (`02` §3). It will first ratchet with real money behind it.
- 🔴 **A gate in this ledger empties the local development database, and re-running the ledger on
  12 Sep 2026 did exactly that.** `gates/twt-3.md` G2 proved the migration round-trips by running
  `make migrate && make downgrade && make migrate` with no `BASKFY_DATABASE_URL` set — and that
  variable defaults to `localhost:5433/baskfy`, the database `DESK_DATABASE_URL` and
  `SCREENER_DATABASE_URL` also point at. `make downgrade` is `alembic downgrade **base**`, not
  `-1`. **Every table in the developer's own database is dropped and recreated empty each time the
  gate runs.**
  **What was lost is small but it was real, and it is not recoverable from here.** Before the run
  that database held a `tw_config` row reading **₹25,00,000**, `updated_by = test`, written
  11 Sep 15:05 UTC with an **empty** `tw_config_audit` — a capital written around the audit path.
  That row is gone. The database is now migrated and empty; `make seed` restores the reference and
  fixture data, and nothing restores anything that was not seeded. At 64 MB it was close to an
  empty schema before the wipe (the plant's bars live in `baskfy_preseed` and `baskfy_tw9_plant`,
  608–644 MB, untouched), so the likely loss is that one row — but **I did not record the contents
  first and so cannot prove that**, and that uncertainty is the honest answer rather than a
  reassurance.
  **Repaired.** G2 now round-trips a throwaway `baskfy_migrate_check` database, the way
  `gates/twt-10.md` G6's drill already did, and asserts the end state (`version=0041_twt
  tw_tables=13`) instead of grepping for a word that a single successful upgrade would also print.
  Re-running the gate afterwards left the dev database's 112 tables in place. DECISIONS-TW TW11.4.
  **The `tw_config` finding this bullet used to carry still matters for the box**, where the same
  question is unanswerable from here: a capital with no matching `tw_config_audit` row means
  somebody wrote it around the audited path. `docs/twt/FIRST-LIVE-MORNING.md` §3.5 now asks for the
  audit row alongside the number for exactly that reason.
  **What R11 proves, and what it does not.** Its check greps the *seeder's source* for
  `sleeve_capital_inr=Decimal("0")`. It has never looked at a database. "The run never set the
  capital" is true and is what R11 asserts; "no database has a capital" is a different claim and no
  gate in this run makes it.
- **`BASKFY_TWT_EXECUTION_ENABLED` is false everywhere.** **T2 — Maulik's hand only.**
- ✅ **The evening and the morning are scheduled now** (TW11, 12 Sep 2026): `twt-evening` at 21:20
  and `twt-morning` at 09:05, Mon–Fri. Neither places an order — `tasks/twt_evening.py` says so in
  its first line, every line is `PROPOSED` until a person confirms it on the desk.
  **The sentence this bullet used to carry was half wrong** and is worth keeping as a correction.
  It said *"No Beat entry, no TWT entry in the desk's clock"*, and `twt-detect` has been in
  `BEAT_SCHEDULE` at 21:00 since TW4. Two of the three were missing; the summary rounded that up to
  all three, which is how a gap gets fixed twice or not at all.
  **The 15:15 sweep is still a person's command, deliberately and permanently.** It re-arms GTT
  stops, so a Beat entry for it would be the desk placing orders on a timer, and non-negotiable #1
  allows exactly one named auto-execute exception — the swing sleeve's. It stays
  `POST /twt/sweep`. `test_the_sweep_is_not_on_a_timer` enforces it. DECISIONS-TW TW11.2.
- **TW9's backtest drifts and is FLAGGED** — 22.17 % CAGR at −26.47 % on 169 trades against `01`
  §6's 20.92 / −24.7 / 164. DECISIONS-TW TW9.3 names the ₹5 crore liquidity floor as most of it.
- The plant's bars are thinner than Chartink's on some days. Expected, not fixable here.
- **No deploy.** The box is Maulik's.

---

## 3. Everything waiting on one deploy

**The box is serving `dd9cc73`**, which predates both the Portfolio Command Center and the
corporate-action fix. HEAD is `43b34dd`. Four separate gate sets are blocked on this single
deploy, and **none of them is coding work**:

| Blocked | Where | What the box is doing wrong today |
|---|---|---|
| PC1 G18 + `gates/pc-integration.md` I12 | `GATES.md`, `NEEDS-MAULIK.md` § PC | Serving the **old** Portfolios screen |
| `gates/ca-truncation.md` G8 | `NEEDS-MAULIK.md` § CA | Still fetching **20 corporate-action rows** a night |
| `gates/backfill-compression.md` G3, G4, G7 | — | The post-run duplicate assertion and the box's own duplicate-pair check have never run |
| Risk-ceiling lock (M4) | `NEEDS-MAULIK.md` Open item 1 | Repo and box simply differ. **Deploy outside market hours** |

**Why it has not happened.** The Docker daemon has not been running on this machine, so the web
image cannot be built or pushed to ECR. And that box is the **live auto-execute host**, so
deploying it is Maulik's call in any case, not an agent's.

Everything these gates would prove about the *code* is already proved against the working tree by
PC1 G3–G16. What is unproved is only that the box has caught up.

---

## 4. The UI polish tree — 5 gate files pending

⚠️ **First: this tree had the whole repository's lint gate red, and nothing had noticed.**
Found 12 Sep 2026 while re-running the TWT ledger. `make lint` exited 1 on two errors in this
tree's **uncommitted** work:

| Where | Error |
|---|---|
| `filter-chip-bar.test.tsx:163` | `TS2322` — `renderBar`'s `index?: string` widened past `definition.index`, which is a slug union |
| `default-view.test.tsx:321` | `@typescript-eslint/require-await` — an `async` test with no `await` in it |

Both were in the 87 lines this tree added to `filter-chip-bar.test.tsx` and its sibling. **They
were failing every `make lint` gate in the repository**, including three TWT gates (`twt-1` G12,
`twt-3` G10, `twt-8` G9) that had been recorded green before the breakage landed.

Fixed here, minimally and type-only: the first now reads
`index?: ReturnType<typeof defaultDefinition>["index"]`, the second drops a needless `async`.
No behaviour changed and no assertion was touched. `make lint` exits 0 again; the one remaining
`react-hooks/incompatible-library` **warning** on `data-table.tsx` is pre-existing and is a warning,
not an error. **Said plainly because it is this tree's code and not TWT's:** if the session that
owns these files has a different intention for either line, it should overrule this.


Needs a dev server and Playwright, which is the second-heaviest thing you can run here. Do it in
its own session with the Python toolchain idle.

- `gates/node-7.1.md` — 0/3. Leaves 7.1.1–7.1.3 green, `cell-encodings.tsx` holds both edits, and
  a **live render** showing the top row's bar visibly longer than row 2's.
- `gates/node-7.2.md` — 0/2. Leaves 7.2.1–7.2.2 green, and no element anywhere renders a raw slug.
- `gates/node-7.3.md` — 0/2. Leaves 7.3.1–7.3.2 green, and FLIP coexists with the table-first
  default on first interaction.
- `gates/leaf-7.4.1-mobile-perf.md` — 0/3. Bounded node count for a 271-row result, scrolling
  still reaches the last row, and each card keeps its rank badge, score bar and return chip.
- `gates/leaf-7.5.1-verify.md` — 5/6.

Also open in the same neighbourhood: `gates/leaf-7.1.1-scorebar.md` (8/9),
`gates/leaf-7.2.1-chip-labels.md` (3/4), `gates/leaf-7.2.2-tooltips.md` (4/5),
`gates/leaf-7.3.1-default-view.md` (4/6), `gates/leaf-7.3.2-motion.md` (5/6).

---

## 5. Parked, and each one needs a decision rather than an engineer

- **`gates/trending-root.md` — all 16 gates ABANDONED.** Reason recorded in the file:
  *"curated_trending.py is owned by a concurrent session; its module defines 9 lists under
  different keys."* This needs a call on who owns that module before a line is written.
- **`gates/tree7-leaf-7.4-costs.md`, `-7.5-notify.md`, `-7.6-return-caveat.md` — 0/2 each,
  deferred.** The file's own words: *"fee math exists; page not built. Resume after
  mark-as-invested (#15)."*
- **`gates/sb7-cash-sleeve.md` — 2/4.** API accepting an explicit `cash_pct` on a from-screen
  save, and the core parity test listing `ZERO_CASH_PCT`.
- Smaller residue, listed so nobody rediscovers it: `gates/catalogue-content.md` (13/15, 2
  abandoned), `gates/marketing-flow-refresh.md` (6/8, 2 abandoned), `gates/screen-leaf-1.3.1.md`
  and `-1.3.2.md` (4/5 each), `gates/archive-m85-login-freshness.md` (6/7),
  `gates/desk-retire-1.2.3.md` (4/6), `gates/tree5-existential.md` (4/5).

---

## 6. The plan, shaped around 16 GB

**Rule for every session below: one heavy tool at a time.** Docker, or the web suite, or mypy.
Never two.

### Step 1 — Commit the TWT work. Do this first.
Five commits, one per module, TW4 → TW5 → TW6 → TW7 → TW9. No Docker, no browser, no test run
beyond what each module's own gate file already recorded. **Cost: near zero memory. Value: the run
stops being one crash away from losing five green modules.**

### Step 2 — Reclaim the disk. ✅ DONE 12 Sep 2026
See §1. 100 GB returned to the host, nothing auto-starts any more, the plant survived. The one
thing left here is optional: cap the Docker VM at **4 GB RAM / 2 CPUs** in Settings → Resources,
which needs a human click. With zero containers running it is no longer urgent.

### Step 3 — Finish TW10's safety half. Python only.
Write `tools/twt/drill.py` and `test_twt_safety_properties.py`, close G1/G2/G6, fill the seven
root-ledger rows, write `TW-FINAL-REPORT.md`. The TWT run ends here. **No flag is flipped and no
capital is set** — that is R11, and it is a gate, not an oversight.

### Step 4 — The deploy, in a session of its own.
Docker is the only heavy process running. Closes PC1 G18, the corporate-action truncation gate,
the backfill compression checks and the risk-ceiling lock in one go. **Outside market hours**, and
Maulik's call to start it.

### Step 5 — The UI tree, last.
One browser-only session. Dev server plus Playwright, Python toolchain idle.

### Then, and only then, the parked items
Trending needs its ownership question answered. The cost/notify/caveat leaves need
mark-as-invested (#15) to land first.

---

## What this file deliberately does not do

It does not re-open a decision. The seven non-negotiables, the two laws and the nine house rules
in `CLAUDE.md` govern every step above. In particular: **step 4 deploys to the live auto-execute
host and is not autonomous**, and **step 3 ends with the TWT flag still false**.
