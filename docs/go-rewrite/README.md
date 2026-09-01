# Baskfy → Go: the rewrite pack

**What this is.** The instruction pack for rewriting Baskfy's Python backend in **Go**, run by
**eight Claude Code terminals working as separate developers in parallel**, integrated by
Maulik (+ the Cowork session acting as CI/reviewer). Same convention as `MERGE-PROMPTS.md`
and `docs/smallcase/`: ordered lanes, acceptance criteria, a live status page, decisions
recorded rather than asked.

**Read order for every agent:** root `CLAUDE.md` (safety rails and the two laws apply verbatim —
they are *stricter* here, see §Non-negotiables) → this file → `01-architecture.md` →
`02-lanes.md` (your lane only, in full) → `03-parity-and-gates.md` → `04-protocol.md` →
your prompt in `prompts/`. Then start.

## The decision (30 Aug 2026, Maulik)

| | |
|---|---|
| Language | **Go** (1.23+). One module, one repo directory `go/`, three binaries + one CLI. |
| Scope | **Backend only:** `packages/core`, `packages/providers`, `packages/execution`, `services/api`, `services/worker`, and the desk console (`kite-momentum-rebalancer/app`). |
| Untouched | `apps/web` (Next.js, 80k lines TS) — held to the **existing `openapi.json` contract** so it runs unchanged against the Go API. **Postgres schema unchanged** (68 app tables + 21 desk tables) — the database is the second contract. `frozen/strangle/` stays frozen. |
| Method | **Strangler, not big-bang.** Go services stand up beside the Python ones on the same Postgres + Redis; Caddy moves route groups to Go as each passes its gate; Python stays live as the fallback until the last group moves. Nothing tonight turns Python off. |
| Execution | The desk's **Python `/execute` remains the only thing that places an order** until the Go desk has passed two shadow Fridays in `DRY_RUN=true` with byte-parity plans. That cutover is *not* in tonight's scope and no agent may schedule it. |

## Why Go (recorded so nobody relitigates it mid-sprint)

The backend is I/O-bound services (API, workers, Kite/NSE clients, Postgres) plus numerics that
are loops over daily bars, not linear algebra. Agents write correct Go 2–3× faster than Rust,
`go build` is seconds not minutes, a single static binary suits the AWS Mumbai box
(`docs/08`), and `pgx`/`sqlc`/`gonum`/`decimal`/`gokiteconnect` cover every dependency in the
locked stack. Rust's one real advantage — `polars-rs` for a near-1:1 port of `packages/core` —
is outweighed by compile times and borrow-checker churn in an agent loop. Rejected
alternatives and reversal path: `DECISIONS-GO.md` §G0.1.

## Size of the thing (measured 30 Aug 2026, non-test lines)

| Python source | lines | files | Go owner |
|---|---:|---:|---|
| `packages/core` | 27,087 | 79 | L1 · L2 · L3 |
| `packages/providers` | 4,757 | 17 | L4 |
| `packages/execution` | 719 | 9 | L4 |
| `services/api` | 38,997 | 118 | L5 · L6 · L7 |
| `services/worker` | 12,771 | 54 | L8 |
| desk `app/` + `scripts/` | 16,004 | 67 | L4 (wave 2) · L8 (scripts) |
| **total to port** | **~100k** | | |
| tests (both trees) | 71,382 | 236 files | ported as goldens, not line-by-line |
| API contract | 137 paths / 159 operations | `openapi.json` | generated, not written |
| schedules | 18 Celery Beat entries | | L8 |

## What "within hours" honestly means

Eight agents × ~7 hours ≈ 12.5k Python lines per agent. That is achievable **for compiling,
golden-tested Go of each lane's *must* scope**, not for a byte-for-byte port of all 100k lines
with every edge case. So the pack defines, per lane, **must / should / could** (`02-lanes.md`),
and four gates (`03-parity-and-gates.md`) such that **whatever is green at the end of the
evening is usable on its own** and whatever is not carries over in `STATUS.md` to the next
evening with the same prompts (every prompt is resumable). The order of value is:

1. `internal/core/signals` at corpus parity (L1) — the product's answer key.
2. providers + execution gateway with DRY_RUN tests (L4) — the safety boundary.
3. The API routes the web app actually calls — `portfolios`, `screens`, `meta`, `backtests`,
   `admin`, `cb`, `alerts`, `brokers`, `auth` (L5–L7, in that order of web usage).
4. The nightly pipeline + 18 schedules (L8).
5. Backtest/regime/ledgers (L2), curated Track-B internals (L3 — Track B flags are `false`
   today, so this is the lane to fold first if terminals are short).
6. The desk console in Go (L4 wave 2–3), DRY_RUN only.

## Timeline (IST, T = the minute every terminal is open)

| When | What | Gate |
|---|---|---|
| T+0:00 → 0:45 | **Wave 0.** L0 builds the foundation (`go/` skeleton, `internal/domain`, config, db+sqlc baseline from a live schema dump, obs, testkit, CI). L1–L8 do *not* idle: they read their Python scope, write `docs/go-rewrite/status/L<n>.md` (function inventory), and **dump goldens with Python** into `go/testdata/golden/L<n>/`. | **G0** at T+0:45: `go build ./...` and `go test ./internal/domain/...` green on `go/main`; every lane rebased. |
| T+0:45 → 2:30 | **Wave 1.** Port the *must* scope against goldens. | **G1** at T+2:30: each lane's must-scope compiles, its golden tests pass, the lane is merged into `go/main`. |
| T+2:30 → 4:30 | **Wave 2.** *Should* scope; API lanes wire to core; L8 runs the pipeline end-to-end; L4 starts the desk console. | **G2** at T+4:30: `cmd/api` serves the web app's must-routes on :8001 and the Playwright subset (`screens`, `portfolios`, `market`, `nav`) is green against it; `cmd/worker` completes `pipeline.nightly` in DRY_RUN against a DB copy. |
| T+4:30 → 6:00 | **Wave 3.** *Could* scope; desk DRY_RUN parity; docs; status. | **G3** at T+6:00: `scripts/friday_drill.py`-equivalent produces the same plan from the Go desk as from Python (DRY_RUN); `docs/go-rewrite/STATUS.md` is honest and complete. |
| T+6:00 → 7:00 | Stabilize, final merges to `go/main`, one merge of `go/main` into `developer` (adds `go/`, `tools/parity`, `tools/rewrite`; changes **nothing** under the Python trees). | **G4 is not tonight:** two shadow Fridays, Caddy cutover by route group, Python retirement. |

## How to start (Maulik)

```bash
cd ~/Documents/projects/baskfy
git status                        # must be clean — commit or stash first
brew install go sqlc golangci-lint   # once; go ≥ 1.23
go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest
tools/rewrite/setup-worktrees.sh  # creates branch go/main + worktrees ../baskfy-wt/L0..L8
```

Then open nine terminals, one per lane:

```bash
cd ~/Documents/projects/baskfy-wt/L3 && claude      # (L0..L8)
```

and paste the matching `docs/go-rewrite/prompts/L<n>.md` **in full** as the first message.
L0 is *your* terminal. Every ~90 minutes (the gates) run `tools/rewrite/merge-lane.sh L<n>`
for each lane that reports green in `STATUS.md`; the Cowork session can run the same
build/test checks on the merged `go/main` and report.

If you have only six terminals: run L0, L1, L4, L5, L6, L8 tonight; L2, L3, L7 are the
lanes whose Python stays live longest anyway (see `02-lanes.md` §Folding).

## Non-negotiables for this run (added to CLAUDE.md's, not replacing them)

1. **The Python trees are read-only source of truth.** No agent edits, reformats, or deletes a
   line under `decile-blueprint/` or `kite-momentum-rebalancer/`. The only additions allowed
   are golden dumpers under `tools/parity/` (which *import* Python, never modify it).
2. **`DRY_RUN=true` everywhere, `BASKFY_ENVIRONMENT=development`, never live credentials.** A Go
   binary that can reach Kite's order endpoint does not exist tonight: `internal/execution`
   compiles against a `Broker` interface whose only concrete implementation is the dry-run
   adapter; the live Kite adapter is a stub that returns `ErrLiveOrdersDisabled` and a test
   asserts it.
3. **The database is a contract.** No schema changes, no new tables, no Go migrations tonight.
   Alembic remains the migration tool until the last Python service is retired.
4. **Contracts are generated, not typed.** API types come from `openapi.json` via
   `oapi-codegen`; SQL types come from the schema dump via `sqlc`. If the generator output looks
   wrong, the *spec* is wrong — record it in `DECISIONS-GO.md`, don't hand-edit generated code.
5. **Tests assert the spec.** Golden files are dumped from Python *by the Python code as it is
   today*; where today's Python is known-wrong (the look-ahead in `apply_adjustments`,
   `DECISIONS.md` §21.9), the golden carries the known-wrong value **and a `known_bug` marker**
   so the Go port reproduces it for parity now and can fix it later on purpose.
6. **Everything under CLAUDE.md "Safety rails" and "Autonomy charter" stands.** Decide, record
   in `DECISIONS-GO.md` tagged `⚠ UNREVIEWED`, continue. Things only Maulik can supply go in
   `NEEDS-MAULIK.md` under a `## Go rewrite` heading.

## Files in this pack

| File | Purpose |
|---|---|
| `01-architecture.md` | Go layout, Python→Go module map, locked stack, the two laws in Go form, conventions |
| `02-lanes.md` | The nine lanes: ownership, must/should/could, dependencies, folding table |
| `03-parity-and-gates.md` | The golden harness, tolerances, the four gates, definition of done, cutover rules |
| `04-protocol.md` | Worktrees, branches, ownership boundaries, `REQUESTS.md`, commit convention, status cadence |
| `STATUS.md` | Live board — every lane updates its row every 30 minutes |
| `REQUESTS.md` | Cross-lane asks (a shared type, a query, an interface) — L0 answers within 15 minutes |
| `DECISIONS-GO.md` | Judgement calls, numbered `G<lane>.<n>`, tagged `⚠ UNREVIEWED` |
| `prompts/L0.md … L8.md` | Paste-in kickoff prompts, one per terminal, resumable |
| `../../tools/rewrite/` | `setup-worktrees.sh`, `merge-lane.sh` |
| `../../tools/parity/` | `golden.py` — canonical JSON dumper the lanes use for goldens |
