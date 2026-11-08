# Integrating the multibagger ranker into Baskfy — proposal (8 Nov 2026, ⚠ UNREVIEWED)

`STRATEGY.md` is the spec. This note says where the code goes, what it touches, and what it must
not touch. It follows the shape `docs/vbt/` gave VBT-1; nothing below is built yet.

## What it is, in Baskfy's terms

A **fourth sleeve, read-only**: `MB` (multibagger). A nightly post-publish step scores every
universe name with a saved LightGBM model and writes `mb_score_daily`; the web app gets a `/discover`
-style page (ranked list, investable flag, the features that drove each score); the desk gets the
list as a **watchlist**, not a plan. No plan builder, no gateway path, no flag, no auto-execute —
the research is a 12-month holding strategy and the desk's `/analyze → /execute confirm=true` is
how a human would buy a tranche by hand if he chose to. Track C for this run: anything that places or
schedules an order from an MB score.

## Where the code lives (law 1)

| Piece | Location | Notes |
|---|---|---|
| labels, features, cross-sectional ranks, tranche book | `packages/core/src/baskfy_core/mb/` ← `portfolioml/{labels,features,tranches}.py` unchanged | pure: frames in, frames out; `test_mb_purity.py` copies `test_vbt_purity.py` |
| model fit / predict | `baskfy_core/mb/model.py` | takes a `lightgbm.Booster` **object** in; never reads a path. Loading the file is the service's job |
| data loading, Postgres, model files on disk | `services/mb/` (Python worker task) | reads `ohlcv_daily`, `instrument`, `index_snapshot_daily`; writes `mb_score_daily`, `mb_model` |
| nightly task | `mb-score` after `publish` (post-publish, cannot fail the night, like VBT's) | ~30 s for features, ~1 s to score |
| retrain task | `mb-train`, Beat: first Saturday of each quarter | walk-forward refit on every completed 12-month label; model file versioned, previous ranking diffed before publish |

**New dependency: `lightgbm`** — outside `docs/02-tech-stack-adr.md`'s locked stack (house rule 1).
Why: tabular ranker, 300 trees, trains in a minute on the Mac; the alternatives in the stack (none)
do not do this. Scoring needs only the model file and the feature frame. Record in
`docs/DECISIONS-MERGE.md` before it enters `packages/core`.

**Rust rewrite:** `baskfy-core` is the port target and LightGBM has no first-class Rust. Options,
cheapest first: (a) keep `mb-score` as a Python worker task — the plan already keeps Python workers
for what is not ported; (b) export the booster to its text format and score with a small tree walker
in Rust (LightGBM's model file is a plain tree dump; ~200 lines); (c) ONNX. Pick (a) now, (b) when
the Python worker is retired.

## Schema (`db/migrations/00xx_mb.sql`)

```sql
CREATE TABLE mb_model (id serial PRIMARY KEY, trained_on date NOT NULL, label text NOT NULL,   -- 'touch_2x_250'
    features jsonb NOT NULL, params jsonb NOT NULL, path text NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE mb_score_daily (date date NOT NULL, instrument_id int NOT NULL REFERENCES instrument(id),
    model_id int NOT NULL REFERENCES mb_model(id), score numeric(10,6) NOT NULL, rank int NOT NULL,
    universe_n int NOT NULL, investable boolean NOT NULL, top_features jsonb, PRIMARY KEY (date, instrument_id));
```
`date` is the **last completed session** (data-date-clocks skill); prices on the page overlay live
quotes only where a Kite session exists. Round at write time (house rule 8). Idempotent: re-running
a night rewrites identical rows (rule 7).

## Pages

- Web `/mb` (read-only): the day's top 50 with rank, score, investable, 12-month return, distance
  from ATH, turnover, sessions listed; the backtest from `STRATEGY.md` §4 as the page's "what this
  is" panel; the disclaimers as components (rule 9).
- Desk `/mb`: the same list as a watchlist with a "send to /analyze" link that pre-fills a
  *manual* plan. Nothing more.

## Gates before real money (the sleeve's §3, mirroring `docs/vbt/02`)

1. Fundamentals and sector in the feature set (`STRATEGY.md` §6.1–6.2), re-run, numbers no worse.
2. A paper book: 12 monthly tranches of the live ranking, journaled, compared monthly to the backtest's
   expectation (hit rate 25–30 %, median 12m +15 %). Twelve months minimum before any flag exists.
3. D3 posture: a published ranking of named stocks is **advice-shaped**; counsel C1–C3 decides whether
   the public page may show it or only the operator's.

## What this pass did not do

- No code in `packages/core`, no migration, no task, no page. The research pack is the deliverable.
- No commit. The repo had staged RF-refactor changes when the folder was connected; nothing was
  touched outside `machinelearning/python/research/portfolio-ml/`.
- `run_research.py` / `portfolioml/book.py` (the swing book) ran end to end only in `--quick` mode; §2's
  verdict rests on `explore_barriers.py`, which is the same model and splits without the book.
