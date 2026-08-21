# 01 — Decile Blueprint (Product A)

**What it is:** an India-equities momentum screener, built as a specification first and a
codebase second. It is a deliberate, numerically-verified clone of **momoindiascreener.in**,
extended with billing, auth, backtesting and a public API.

**What it is not, yet:** live. It has never ingested a real bar.

---

## 1. Provenance — how it was built

This is unusual and it matters for the merge. The repo began as a **specification bundle**, not
as code:

- `docs/01` … `docs/14` (5,596 lines) reverse-engineer the reference product page by page,
  filter by filter, and derive the factor formulas — then verify them numerically against a real
  **271-row CSV export** taken from the live product on 18 Aug 2026
  (`fixtures/reference-screen-export-2026-08-18.csv`).
- `PROMPTS.md` (44 KB) is 22 ordered build prompts, one module each, every one with stated
  inputs, deliverables and acceptance criteria.
- The code was then produced by running those prompts. Git history is literally
  `module 13: green`, `module 14: green`, … through `module 20: green` — 22 commits.
- `docs/DECISIONS.md` (1,787 lines) logs every judgement call made under spec ambiguity,
  numbered by prompt.

The consequence: **the docs are the source of truth and the code knows it.** `CLAUDE.md` opens
with "If your instinct conflicts with it, say so out loud rather than deviating quietly," and
every deviation that was taken is written into a numbered `*a-implementation-notes.md` file.
This is the most valuable thing in the repo and the merge must not break it.

## 2. Scale

| Area | Files | Lines |
|---|---:|---:|
| `packages/core` (pure domain — no I/O) | 73 | 20,502 |
| `packages/providers` (Kite / NSE / composite / fixtures) | 29 | 6,550 |
| `packages/api-client` (Zod contracts, generated TS) | 7 | 15,563 |
| `services/api` (FastAPI) | 111 | 34,732 |
| `services/worker` (Celery + Beat) | 50 | 11,444 |
| `apps/web` (Next.js 15) | 280 | 28,661 |
| **Total** | **550** | **~117,000** |

Tests: 29 core, 45 API, 13 worker, 12 provider suites, plus 18 Playwright specs.

## 3. Stack (locked, `docs/02`)

Next.js 15 / React 19 / TS 5.6 / Tailwind v4 / shadcn · TanStack Table+Query · visx · Auth.js v5 ·
FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic (Python 3.12) · **Polars** for numerics ·
**PostgreSQL 16 + TimescaleDB** · Redis 7 · Celery + Beat · Cloudflare R2 · Razorpay · Resend ·
pytest+hypothesis / Vitest / Playwright.

## 4. What it does

### Data plant
A ten-step nightly Celery chain (19:30 IST): refresh instruments → fetch Kite daily bars →
fetch NSE corporate actions → apply adjustments → refresh point-in-time index membership →
refresh ~145 index snapshots → compute factors → compute market health → run 8 quality-gate
assertions → publish a `data_version`.

Storage contract is strict: `close` is adjusted, `close_raw` is the exchange print, factors read
`close`, display reads `close_raw`. Money is `numeric`, never float. **Rounded at write time**, so
API, UI and CSV export cannot disagree.

### Factor engine (`packages/core/src/decile_core/factors.py`, 561 lines)
64 ranking factors across families: absolute return, Sharpe return, RSI, volatility, beta,
circuits, positive-days-%, away-from-high, moving averages, marketcap, median volume, P/E.
Each over 1M / 3M / 6M / 9M / 1Y calendar-offset windows. Plus a Wasserstein BULL/BEAR/NEUTRAL
regime label per instrument, and a PROS/CONS rule engine.

### Screener (`screener.py`, 791 lines)
Universe (14 NSE indices, PIT membership) → decile / top-N bucket → a long filter pipeline
(MA, away-from-high, positive days, circuits, marketcap, P/E, series, top-beta/top-volatility
exclusions, CMP range, three custom filter slots) → rank by 1–3 blended factors → sortable,
exportable table. Saveable as a reusable "screen", re-runnable on any historical date.

### Product surfaces (39 Next.js routes)
Screens list/editor/columns · instrument factsheet · dashboard · market health · listings ·
portfolios + rebalance tracker · backtests · pricing/invoices · API keys · alerts · admin ·
auth (5 pages) · marketing + 4 legal pages.

### Commercial machinery
Auth.js v5 + Argon2id + OTP + refresh-token families · Razorpay checkout + webhooks ·
GST invoices with a hand-written PDF writer · entitlement service · public read API
(built, **and deliberately bolted shut**) · API keys · screen-diff alert emails · webhooks ·
OpenTelemetry + Sentry + Prometheus + Grafana + 5 runbooks · backup/restore drill in CI.

### Backtest (`backtest.py`, 1,721 lines)
Point-in-time engine with a `PointInTimeReader` look-ahead guard, metrics, fragility analysis,
SSE progress, R2 artefacts, signed export links.

## 5. Honest status — what has never actually run

Taken verbatim from its own `CLAUDE.md` "Open items carried forward". This list is the single
most useful artefact in the repo and it should survive the merge unedited.

| Never happened | Consequence |
|---|---|
| **No real backfill.** `ohlcv_daily` holds no history | Every backtest a user could queue fails |
| **The decisive 271-row parity test has never run green** — it *skips*, needing 248 days of adjusted closes | The factor engine is verified only against what a single-date snapshot can prove |
| **NSE URL shapes and column names are unverified** — written from documented layouts, suite is network-blocked | First production backfill is where they get found |
| Calendar is ~9 lunar holidays/year short → windows resolve 22/67/127/191/256, not 22/64/121/185/247 | Fixed by running `reconcile_calendar` over a real backfill, not by code |
| **The API → broker → worker wire has never run.** No test starts a Celery worker | Backtest enqueue path is two tested halves and no whole |
| **No Razorpay webhook, alert email or outbound webhook has ever been delivered** | Money and notifications are documented-format guesses |
| **No runbook has been executed against staging.** There is no staging | Each carries a `Verified against: NOT YET` line, asserted by a test |
| **No lawyer has read the four legal drafts.** No grievance officer, no DPO | Not launch-ready; `DRAFT-NOTICE.md` lists 8 decisions needed before the first charge |
| The public API is **off**, held shut by a source constant a test protects | Flipping it is a commit, not a config change |
| Webhook sender has **no SSRF protection** | Contained only by the feature being unreleased |
| `docs/05` §8's skip-month definition is **REFUTED, not resolved** — ours implies 521.31% for CUPID against a published 608.37% | A real formula disagreement, unsettleable from the committed export |
| The ten-journey Playwright suite has never been executed | Type-checks only |

**Read that table as: Decile is an extremely well-built machine that has never been switched
on.** Everything downstream of "it produces correct numbers from real data" is currently
unproven, and that is exactly the risk the merge with the desk retires — because the desk has
real data, a real account, and a real answer key.
