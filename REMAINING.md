# What is left — the honest remaining list (12 Sep 2026)

One page, written because the laptop is out of memory and the run needs to be sequenced around
that rather than pushed through it. Branch `developer`, HEAD `43b34dd`.

This file is a **snapshot**, not a status page. `docs/00-merge-status.md` and `docs/twt/STATUS.md`
stay authoritative for their own runs; `NEEDS-MAULIK.md` stays authoritative for what only Maulik
can supply. Where this file and one of those disagree, they win and this file is the stale half.

---

## 0. The urgent thing, and it is not a feature

**Five green TWT modules are uncommitted.** TW4, TW5, TW6, TW7 and TW9 exist only in the working
tree. `gates/twt-4.md` through `gates/twt-9.md` are checked with evidence, the suites are green,
and none of it is in a commit.

| | |
|---|---|
| Uncommitted files | 60 — 30 modified, 30 untracked |
| Committed TW modules | TW0 (`5056aa1`), TW1 (`9637e86`), TW2a (`b299c35`), TW3 (`a2bf343`), TW8 (`104d93b`), TW10a runbook (`511e31f`) |
| **Uncommitted TW modules** | **TW4, TW5, TW6, TW7, TW9** |

Verified work that is not committed is work that can be lost to a crash, and a machine under
memory pressure is a machine that crashes. **This is step 1 of the plan below.** Five commits, one
per module, in the repo's `M<N>: green — <one line>` habit.

---

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

### The three heavy things in this repo. Never run two at once.

- **Docker + Postgres on 5433** — needed only by the `db`-marked tests. Note the trap recorded in
  `docs/twt/STATUS.md`: with the daemon down, a db-marked suite turns into a silent green *skip*.
- **The web suite** — 2,900+ vitest tests, plus Playwright's Chromium for the e2e specs.
- **`uv run mypy --strict`** — walks 644 source files.

---

## 2. TWT — nine of eleven units green, one module left

`docs/twt/STATUS.md` is the full ledger. What remains:

### TW10, the safety half — 3 gates unmet (`gates/twt-10.md`)

| Gate | What it needs | Cost |
|---|---|---|
| G1 | A property test over **every route and every task** proving that with `BASKFY_TWT_EXECUTION_ENABLED=false`, no TWT path reaches `OrderGateway.place` or `place_gtt_stop` with `DRY_RUN=false`. Not a spot check. `packages/core/tests/test_twt_safety_properties.py` | Python only |
| G2 | A grep proving no `BASKFY_TWT_AUTO`-anything exists anywhere. Non-negotiable 1's named exception stays the swing sleeve's alone | Seconds |
| G6 | **`tools/twt/drill.py` does not exist yet.** The full DRY_RUN drill — evening plan, morning plan, confirm every line, a fill, a GTT, a ratchet, the sweep — reporting `0 orders reached a broker`. This is the sentence `docs/twt/FIRST-LIVE-MORNING.md` tells Maulik to look for the night before he goes live | Python only |

G3, G4, G5, G7 are already green with evidence. G8 is currently blocked by its own staleness: it
reads `gates/twt-9.md 0/8 pending:8` and `gates/twt-root.md 7/14 pending:7`, which will clear once
the ledger below is filled.

### The root ledger — 7 rows unfilled (`gates/twt-root.md`)

R6, R7, R9, R10 record TW6/TW7/TW9/TW10's results. R11 asserts no live order, no flag flip, no
capital set. R12 asserts both trees still green. **R13 is `TW-FINAL-REPORT.md` at the repo root**,
in the style of `SW-FINAL-REPORT.md`, and it has not been written.

### What TWT will still NOT have when TW10 is green

Carried forward from `docs/twt/STATUS.md` so it is not quietly lost:

- **The ratchet has never executed anywhere.** No paper phase was chosen (`02` §3). It will first
  ratchet with real money behind it.
- **`tw_config.sleeve_capital_inr` is seeded at 0** and this run never sets it.
- **TW9's backtest drifts and is FLAGGED** — 22.17 % CAGR at −26.47 % on 169 trades against `01`
  §6's 20.92 / −24.7 / 164. DECISIONS-TW TW9.3 names the ₹5 crore liquidity floor as most of it.
- The plant's bars are thinner than Chartink's on some days. Expected, not fixable here.

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
