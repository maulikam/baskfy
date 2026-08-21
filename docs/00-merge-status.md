# 00 — Merge status

The live status page for the Baskfy merge run (`MERGE-PROMPTS.md`). Updated at the end of every
module. **Loud about what is NOT done** — both source repos keep honest open-items lists, and
that culture continues here.

**Run started:** 22 Aug 2026 · **Current module:** M2 · **State:** running

---

## Module ledger

| # | Module | State | Date | Note |
|---|---|---|---|---|
| M0 | Preflight, baselines, safety copy | ✅ done | 22 Aug 2026 | Baselines + verified safety copy recorded. Blocker resolved: decile's overnight work committed as `ea5dd0f` (M0.8) |
| M1 | Umbrella repo (subtree) | ✅ done | 22 Aug 2026 | One repo at the root; 117 commits; both histories reachable from HEAD. `--follow` does not prove it — see M1.1 for the commands that do |
| M2 | The rename | 🔄 running | 22 Aug 2026 | |
| M3 | Working agreement + status page | — | | |
| M4 | One env schema | — | | |
| M5 | One CI workflow | — | | |
| M6 | Freeze the strangle lab | — | | |
| M7 | Local stack up | — | | |
| M8 | NSE endpoints verified | — | | |
| M9 | Kite creds + backfill | — | | **HUMAN GATE** |
| M10 | Calendar + corporate actions | — | | |
| M11 | 271-row parity test | — | | **RED GATE** |
| M12 | Desk parity | — | | **RED GATE + HUMAN REVIEW** |
| M13 | MomentumScan — CSV cord cut | — | | |
| M14 | Breadth + shadow harness | — | | |
| M15 | Desk brains → core | — | | |
| M16 | Execution package + broker split | — | | |
| M17 | Rank buffer demoted | — | | |
| M18 | Database migration | — | | **HUMAN GATE** |
| M19 | Schedules → Celery | — | | |
| M20 | Observability + runbook 6 | — | | |
| M21 | Verification + handover | — | | |

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
