# MERGE-PROMPTS — the Baskfy execution script

One file, executed top to bottom by Claude Code, that takes this folder from "two repos and a
docs directory" to **one merged, tested, runnable single-user Baskfy** (Phases 0–3 of
`docs/05-merge-plan.md`). It is written in the same convention both codebases were built with —
ordered modules, each with acceptance criteria — because that convention is why they work.

## How to run this (for Maulik)

Open a terminal:

```
cd ~/Documents/projects/baskfy
claude
```

Paste this one message and let it run:

> Read CLAUDE.md, then docs/README.md, then MERGE-PROMPTS.md in full. Execute the modules
> M0→M21 strictly in order. One commit per module, message "M<N>: green — <summary>". Do not
> proceed past unmet acceptance criteria. Stop and ask me only at the points marked HUMAN GATE,
> and at any red gate. Keep docs/00-merge-status.md updated as you go. Begin with M0.

To resume in a later session, paste:

> Read CLAUDE.md, MERGE-PROMPTS.md and docs/00-merge-status.md, then continue from the first
> module that is not marked done.

Expect three pauses that need you: **M9** (Kite credentials + the daily login token), **M12**
(the parity gate — you review the delta tables), **M18** (go/no-go on the real database
cutover). Everything else should run without you. Budget: M0–M8 is a long evening of agent
work; M9–M12 depends on the backfill and what parity finds; M13–M21 is the structural week(s).

## Rules (binding for the whole run)

1. **Order is strict.** A module starts only when the previous one's acceptance criteria are met
   and committed.
2. **The desk keeps trading.** `kite-momentum-rebalancer` tests are green at the end of every
   module. If a Friday arrives mid-run, the desk must be usable.
3. **`DRY_RUN=true` everywhere an agent sets up anything.** No live orders, ever, from this
   script.
4. **Anything touching `data/portfolio.db` follows CLAUDE.md's backup rail first.**
5. **Never weaken, skip, or fudge a test to get to green.** A criterion that seems wrong →
   `docs/DECISIONS-MERGE.md`, stop, ask.
6. **HUMAN GATE protocol:** stop, print exactly what is needed and why, wait. Do not work ahead
   past a gate.
7. **RED GATE protocol (M11/M12):** if parity fails with unexplained deltas, stop the run
   entirely. Write up every delta. Do not open M13.
8. The strangle subsystem is out of scope beyond M6's freeze. `docs/` 01–08 are the record —
   extend, don't rewrite.
9. Secrets: never echoed, never committed. `data/`, `.env` stay untracked.
10. Every module ends by updating `docs/00-merge-status.md` (one line per module: done/blocked +
    date + anything a future session must know).

## Map to the plan

| Modules | Plan phase |
|---|---|
| M0–M6 | P0 Foundation |
| M7–M12 | P1 Prove the numbers (M12 = P1.7, **the** gate) |
| M13–M14 | P2 Cut the CSV cord (shadow weeks are calendar time — tooling ships here) |
| M15–M20 | P3 One codebase |
| M21 | Final verification + handover |
| — | P4+ is deliberately absent (SEBI gate; see CLAUDE.md) |

---

## M0 — Preflight, baselines, and the safety copy

**Goal:** know the ground truth before touching anything.

1. Verify tools: `git`, `python3.12` (decile pins `>=3.12,<3.13`), `uv`, `node` + `pnpm`
   (decile web), `npm` (desk's Tailwind build), `docker` + `docker compose`, `sqlite3`, `rsync`.
   Report versions; stop if one is missing and tell Maulik what to install.
2. Both sub-repos: `git status` clean, record `git rev-parse HEAD` of each in the status page.
3. Install deps: `decile-blueprint`: `pnpm install` and `uv sync`; desk: create `.venv`,
   `pip install -r requirements.txt`, and `npm ci` if its package.json build needs it.
4. **Safety copy (before anything else):** in the desk, run `python -m scripts.backup` and
   confirm it reports `ok`; then copy the entire `kite-momentum-rebalancer/data/` directory to
   `~/baskfy-safety/<today>/data/` (outside this folder). Record both in the status page.
5. Baselines: run the desk suite (`pytest`) from its directory; run decile's test targets (read
   its `Makefile` first — bring up `make up` services if its suites need Postgres/Redis; skip
   network-marked suites). Record pass/fail counts verbatim.
6. Create `docs/00-merge-status.md` (module table, baseline numbers, safety-copy path) and
   `docs/DECISIONS-MERGE.md` (empty, with the numbering convention noted).

**Acceptance:** all tools present; both baselines recorded; backup verified `ok`; safety copy
exists outside the repo; the two new docs exist.

## M1 — The umbrella repo (P0.2)

**Goal:** one git repo at this root, both histories preserved.

1. Write a minimal root `.gitignore`: `.DS_Store`, `_to_delete/`, `*.tar.gz`,
   `baskfy-safety/` (each sub-tree keeps governing its own ignores).
2. `git init` at the root; first commit: `CLAUDE.md`, `MERGE-PROMPTS.md`, `docs/`,
   `.gitignore`.
3. Preserve histories via subtree. Recommended recipe (verify branch names with
   `git -C <dir> branch --show-current` first):
   - `mkdir ../_baskfy_subtree_tmp && mv decile-blueprint ../_baskfy_subtree_tmp/`
   - `git subtree add --prefix=decile-blueprint ../_baskfy_subtree_tmp/decile-blueprint <branch>`
   - repeat for `kite-momentum-rebalancer`.
4. **Restore untracked files** (subtree brings only tracked content): rsync back from the tmp
   copies with `--ignore-existing` — this returns the desk's `data/` (portfolio.db!), `.env`,
   and any venvs/node_modules. Verify `data/portfolio.db` and `.env` are back and that
   `git status` does NOT list them (the sub-tree `.gitignore`s must cover them; fix if not).
5. Keep `../_baskfy_subtree_tmp/` untouched until M21 — it is the rollback.

**Acceptance:** `git log --follow -- kite-momentum-rebalancer/app/scoring.py` reaches pre-merge
commits, and a decile file does the same; desk suite green from the new tree; `git status`
clean with `data/` and `.env` untracked.

## M2 — The rename, as one commit containing nothing else (P0.3)

**Goal:** the namespaces become Baskfy's. Directory names of the two sub-trees stay.

1. `decile_core`→`baskfy_core`, `decile_api`→`baskfy_api`, `decile_worker`→`baskfy_worker`,
   `decile_providers`→`baskfy_providers` — package dirs, imports, pyproject names/entry points,
   Alembic env, Celery task names (`decile.ingest.*`→`baskfy.ingest.*` etc.).
2. TS: `@decile/*`→`@baskfy/*` in package.json files and imports.
3. Env prefix `DECILE_`→`BASKFY_` (settings classes, compose, `.env.example`s, Makefile, CI);
   compose project/container names and database name `decile`→`baskfy`.
4. Exclusions: `docs/` (both trees' historical docs stay verbatim), committed fixtures'
   contents, and the sub-tree directory names.
5. Run every linter/type-checker/test suite that M0's baseline ran.

**Acceptance:** zero occurrences of `decile_`, `DECILE_`, or `@decile/` outside `docs/`
(`grep -rIl` proves it); suites match or beat the M0 baselines; **exactly one commit**.

## M3 — Working agreement + status page formalized (P0.4, P0.8)

**Goal:** one governing CLAUDE.md, honestly cross-linked.

1. Compare root `CLAUDE.md` against both sub-tree CLAUDE.mds; fold in anything material it
   lacks (it already carries the two laws and the seven non-negotiables — verify verbatim).
2. Add a one-line header to each sub-tree CLAUDE.md: "Part of Baskfy — the root CLAUDE.md
   governs; this file remains authoritative for this tree's internals."
3. Make `docs/00-merge-status.md` the first link in `docs/README.md`.

**Acceptance:** a new contributor reading root CLAUDE.md alone would not break either product;
status page linked; both sub-files marked.

## M4 — One env schema, user knobs split from system knobs (P0.5)

**Goal:** a single documented `.env.example` at the root.

1. Merge both `.env.example`s into a root `.env.example` under `BASKFY_*` for the pipeline side;
   document the desk's existing unprefixed vars in a clearly marked desk section (renaming the
   live desk's env vars waits for M15+ so the deployed box is never broken by a pull).
2. Mark every var `# user-editable` or `# system-only` per docs/03 §3f.
3. Add a test asserting no system-only risk constant (`RISK_*`, rate caps, kill switch) is
   reachable from any user-facing settings route in either app.

**Acceptance:** root `.env.example` complete; the reachability test exists and passes.

## M5 — One CI workflow (P0.6)

**Goal:** a PR touching either tree gets one green check.

1. `.github/workflows/ci.yml`: decile's existing jobs (lint, type, pytest suites, TS checks)
   plus a desk job (`pip install -r requirements.txt && pytest`).
2. Run each workflow step locally exactly as CI would, and record the results (no pushing
   required to prove it).

**Acceptance:** every CI step passes locally; workflow file committed.

## M6 — Freeze the strangle lab (P0.7)

**Goal:** the options subsystem out of every gate, breaking nothing.

1. Map the import graph first: what in `app/` (main.py, strategies/__init__, analytics)
   imports `app/strategies/strangle/*` or `app/strategies/options*.py`, and how the
   `OPTIONS_ENABLED` gate guards it.
2. Make those imports lazy/optional behind the existing gate, then `git mv` the strangle package
   and options strategy modules to `frozen/strangle/`, tests included.
3. Exclude `frozen/` from pytest collection, linters, type-checkers, coverage.
4. Do NOT touch the live box's systemd collectors — repo only. Note in the status page that the
   box still runs the pre-freeze layout until its next deliberate deploy.

**Acceptance:** desk boots and its suite is green with `OPTIONS_ENABLED=false`; `frozen/` is
outside every gate; collectors acknowledged as ops.

## M7 — The local stack, up (pre-P1)

**Goal:** the pipeline's services run end to end on this machine, empty.

1. `decile-blueprint`: `make up` (Postgres/Timescale, Redis, mailpit per
   `infra/docker/compose.yml`), then migrations (`alembic upgrade head` via its Make target),
   then `make seed`.
2. Boot the API and a worker (`make api`, `make worker`) and run whatever doctor/integrity
   commands the Makefile exposes.

**Acceptance:** migrations apply cleanly; API healthy; worker consumes; integrity checks pass at
"no data yet" grade; how-to recorded in the status page.

## M8 — NSE endpoints against the real network (P1.1)

**Goal:** every NSE provider serves real bytes; the guesses become verified facts.

1. Run each NSE provider (constituents, index snapshots, corporate actions, bhavcopy, listings)
   against the live site through the existing provider classes — headers, cookie priming,
   URL shapes, column names.
2. Fix what reality disagrees with; archive every raw fetch through the archive path
   (`LocalRawArchive` is fine until AWS).
3. Commit a dated verification note per endpoint (URL, date verified, quirks) —
   `docs/DECISIONS-MERGE.md` or a `reconciliation/` note, matching repo culture.

**Acceptance:** all NSE providers return and parse real data; raw files archived; each URL has a
dated verified note. NSE quirks (rate limiting, cookie dances) documented, not worked around
with scraping hacks.

## M9 — Kite credentials and the first real backfill (P1.2) — HUMAN GATE

**Goal:** `ohlcv_daily` holds 2011→today for the equity universe.

1. **HUMAN GATE:** ask Maulik for the Kite Connect app credentials to use for pipeline data
   (reusing the desk's app from its `.env` is acceptable for Phase 1 — note in
   DECISIONS-MERGE that system-vs-user credential split happens before any multi-tenant work),
   and have him complete the daily login to mint an access token. Never echo the values.
2. Refresh instruments; run the backfill `FROM=2011-01-01` through the existing resumable,
   chunked (≤2,000-day), rate-limited path. Expect roughly an evening (docs/07 §4b); it must
   survive interruption and resume via `ingest_cursor`.
3. Then the rest of the chain's data prerequisites: point-in-time index membership, index
   snapshots, corporate actions.

**Acceptance:** `ohlcv_daily` populated across the universe; `ingest_cursor` shows a clean
finish; raw-file archive holds the fetches; token never logged.

## M10 — Calendar and corporate actions over real history (P1.3, P1.4)

1. Run `reconcile_calendar` over the backfill; re-assert the factor windows resolve
   **22/64/121/185/247** (not the seeded calendar's 22/67/127/191/256).
2. Run corporate-action adjustment across full history; CUPID's documented actions reproduce;
   rights issues report `INSUFFICIENT_DATA` rather than guessing.

**Acceptance:** window-length assertions green against real data; `adj_factor` populated; CUPID
reproduces; the calendar fix is data, not code edits.

## M11 — The 271-row parity test (P1.5, P1.6) — RED GATE

1. Set the parity-bars env (`BASKFY_PARITY_BARS` after M2) and un-skip
   `test_reference_parity.py`. All 93 numeric columns of all 271 rows against
   `fixtures/reference-screen-export-2026-08-18.csv`.
2. Any non-green column gets a written explanation in DECISIONS-MERGE **before** proceeding —
   or stops the run if unexplainable.
3. Settle docs/05 §8's skip-month formula: candidate B against real bars vs the published
   608.37% for CUPID. Either it reproduces, or the factor is marked UNVERIFIED and excluded
   from every default screen (with a test asserting the exclusion).

**Acceptance:** the parity test **runs and is green** (or every deviation is explained in
writing); the skip-month question has a committed answer.

## M12 — Desk parity: the merge's real acceptance test (P1.7–P1.10) — RED GATE + HUMAN REVIEW

**This is the gate the entire merge stands on (docs/README, docs/03 §5c).**

1. For **every** scan CSV in `kite-momentum-rebalancer/data/uploads/` (they are read-only):
   generate the desk's 29 required columns from the merged engine for the same date and
   universe, run **both** files through the desk's **unmodified** `app/scoring.py`, and print
   the top-25 rank delta table.
2. Repeat across all historical scans held, not just the latest (P1.8).
3. Reconcile pipeline breadth (`market_health_daily`) against the desk's `breadth_readings` row
   for the date both cover; agreement or a written reason (P1.9).
4. Commit `reconciliation/DESK-PARITY.md` in the style of the existing `REPORT.md`: date, data
   version, every delta and its explanation (P1.10).
5. **HUMAN GATE:** show Maulik the delta tables and DESK-PARITY.md. He opens M13, not the agent.

**Acceptance:** empty top-25 delta tables across all dates (or every moved name explained and
accepted by Maulik). **If not: full stop.** The fallback posture is docs/05's: "Decile is a
second opinion, not the source" — a different, smaller plan.

## M13 — MomentumScan: the CSV cord is cut (P2.2, P2.3, P2.4)

1. Build the `MomentumScan` internal contract: `(date, universe) →` the desk's columns with the
   export's exact names and precision. A fixture test asserts byte-identity with a CSV the desk
   would have accepted.
2. Add the "generate scan" path to the desk's `/analyze` beside upload — **upload keeps
   working**. An end-to-end test asserts both paths produce the same plan for the same date.
3. Record `screen_run_id` (definition + `as_of` + `data_version`) on every plan; an old plan can
   re-resolve its exact inputs.

**Acceptance:** fixture byte-identity test green; both `/analyze` paths agree end to end;
`screen_run_id` present on new plans and resolvable.

## M14 — Breadth wiring and the shadow-mode harness (P2.7, P2.5 tooling)

1. Point the desk's cash-band breadth (`_cash_pct`) at `market_health_daily`; the one-row
   `breadth_readings` table stops being written (kept for history).
2. Build the shadow-mode harness: one command that, for a given Friday, produces the plan from
   the uploaded CSV and from the generated scan, diffs at order level, and appends the result to
   a log the ops page can show.
3. Write `docs/SHADOW-MODE.md`: the four-consecutive-green-Fridays protocol (P2.5) and the flag
   flip that makes generated the default afterwards (P2.6). The four weeks are calendar time —
   Maulik runs them; the tooling and the flag ship now.

**Acceptance:** breadth reads the pipeline; the harness produces an order-level diff on the
corpus; the protocol doc exists; the default-flip is one flagged config change, ready but off.

## M15 — The desk's brains move to core (P3.1–P3.4)

1. `app/scoring.py` → `packages/core/.../score.py` unchanged (only the `__main__` block moves);
   byte-identical outputs over the M12 corpus, asserted.
2. `app/rebalance.py` → core `basket.py`, made pure: the `data/sectors.csv` read lifts to a
   caller-supplied mapping. Same plans on the corpus, asserted.
3. `app/costs.py` → core; `app/core/regime.py` + `regime_alloc.py` → core `exposure/`; regime
   replay byte-identical to the persisted `regime_evaluations` rows.
4. Rename decile's instrument-level `regime.py` → `instrument_regime.py`; add the test that
   forbids any module importing both regime names (docs/03 §3c).
5. Extend core's purity enforcement over every moved module — the real mechanisms are
   `packages/core/tests/test_no_escape_hatches.py` plus the suite-wide socket block in
   `network_guard.py` (docs/05 calls this "test_no_io_in_core"; use what exists). The desk
   imports from core; its suite stays green.

**Acceptance:** corpus outputs byte-identical pre/post move; no-I/O test covers the moved
modules; collision test in place; desk green.

## M16 — The execution package and the broker split (P3.5–P3.7)

1. Create `packages/execution/` from the desk's `core/{gateway,guards,risk,ratelimit}.py`.
   Each of the seven non-negotiables gets a named test that fails if it is "improved" away.
2. One broker port, two faces: `MarketData` (system credentials — historical/quotes/instruments)
   and `Trading` (the account's credentials — holdings, margins, orders, GTT). Tests assert a
   trading token cannot reach an ingestion call and the system token cannot reach
   `place_order`.
3. Move the desk's token storage to the encrypted path (decile's `providers/tokens.py`
   pattern); the plaintext `data/.kite_token.json` write path is removed.

**Acceptance:** seven named non-negotiable tests green; both cross-face tests green; no
plaintext token write remains (grep proves it); desk still authenticates.

## M17 — Rank buffer demoted, portfolios adapted (P3.8)

1. Decile's `rebalance.py` → `rank_buffer.py`, re-expressed as one input to core `basket.py`
   (the desk already embodies it as `RETENTION_BUFFER` + `REPLACEMENT_EDGE`).
2. Decile's `/portfolios` surface keeps working through a thin adapter.
3. Run the web portfolio checks: type-level always; execute the portfolio Playwright specs if
   the local environment allows (record honestly if not — they have never been executed).

**Acceptance:** one basket-construction code path; adapter keeps `/portfolios` working;
Playwright attempt recorded either way.

## M18 — The database migration, built and drilled (P3.9) — HUMAN GATE for the real run

1. Write the one-way SQLite → Postgres migration: per-table row counts + checksums asserted,
   NAV series recomputed and compared to the SQLite-derived series, restore drill proving the
   dump reopens.
2. **Run it against a COPY** of `data/portfolio.db` into the local Postgres. All assertions
   green. The desk's read paths run against the migrated copy in a test configuration
   (the 13 Jinja pages render — P3.11's first half).
3. **HUMAN GATE:** present the assertion report to Maulik. The real cutover (desk configured to
   Postgres as primary, SQLite archived forever) happens only on his explicit go, on a
   non-trading evening, with the M0 safety copy refreshed first.

**Acceptance:** migration script + assertions green on the copy; drill documented; real cutover
awaiting/complete per Maulik's word; SQLite file archived (never deleted) either way.

## M19 — Schedules to Celery, desk on the merged backend (P3.10, P3.11)

1. Re-express `scripts/daily.py` and `scripts/autorun.py` as Celery tasks on Beat (IST), keeping
   idempotency: a re-run changes nothing, asserted by test. The desk's own convention holds —
   one failure never stops the rest.
2. The systemd timers on the live box stay until five green Beat runs are recorded (that
   retirement is a box operation, documented in the status page, not performed here).
3. Point the Jinja desk at the merged backend (post-M18 database) in the local configuration;
   all 13 pages work.

**Acceptance:** Beat schedules exist with idempotency tests; the 13 Jinja pages green against
Postgres locally; timer-retirement protocol written down.

## M20 — Observability over the order path, and runbook six (P3.12, P3.13)

1. Extend OTel spans, Sentry capture, and Prometheus metrics over plan → execute → GTT —
   the paths that today have `journalctl` and nothing else.
2. Write runbook #6, "a rebalance half-executed", in the style of the existing five, carrying
   its honest `Verified against: NOT YET` line (staging comes with AWS Phase B).

**Acceptance:** the order path is traceable end to end in local runs; runbook committed; the
alert rules that exist evaluate over the new metrics.

## M21 — Full verification and handover

1. Everything, once, from clean: linters, type checks, both Python suites, TS/unit suites, the
   CI workflow steps, the M12 corpus re-run (still byte-identical through the moved modules),
   integrity checks against the populated database.
2. Write **`RUN-AND-TEST.md`** at the root — the document this whole script exists to earn:
   - bring the stack up from nothing (compose, migrations, seed, api, worker, beat, web dev);
   - the nightly chain: how to fire it manually and what "published" looks like;
   - **the Friday drill in DRY_RUN:** generate scan → `/analyze` → review plan → `/execute`
     with `confirm=true` under `DRY_RUN=true` → stops preview — the full loop with zero orders;
   - where everything is observed (ops pages, metrics, logs);
   - what is deliberately NOT running (Phase 4+, public API, strangle) and why.
3. Update `docs/00-merge-status.md` to its final state; tag `v0.1.0-merged`.
4. Only now may `../_baskfy_subtree_tmp/` be deleted — ask Maulik first.

**Acceptance:** one command sequence in RUN-AND-TEST.md takes a fresh checkout to a running,
testable Baskfy; every suite green; status page final; tag pushed nowhere (local tag — remotes
are Maulik's call).

---

## After M21 (not in this script)

- **Shadow Fridays** (docs/SHADOW-MODE.md) — four consecutive green weeks, then flip the
  default. Operator-paced.
- **AWS Phase A** (`docs/08-aws-architecture.md` §3) — the ₹4,400/month Mumbai box. A separate,
  shorter agent run with Maulik's AWS account at hand.
- **The human tracks** (CLAUDE.md): D3 counsel + Zerodha conversation, TM filing, defensive
  domains, D10 data-licensing position. None of them block anything before Phase 4.
