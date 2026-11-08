# DECISIONS-PM — judgement calls, numbered by module

Pre-taken with the pack (9 Nov 2026, ⚠ UNREVIEWED):

- **PACK.1** Label = *touches 2× within 250 sessions* (`touch`), not `held`, not graded relevance, not
  24 months, not a triple barrier (STRATEGY §2–3). Reverse: rebuild with another `--mb`/`--target`.
- **PACK.2** Model = LightGBM lambdarank, one query per session, 31 leaves, 300 fixed rounds; early
  stopping on a time tail was too noisy. Reverse: `ModelConfig`.
- **PACK.3** Book = monthly tranches, top-20, 250-session hold, 25 bps a side, investable floor
  ≥ ₹50 and ≥ ₹5 cr/day. The index gate is **off** (cost 11 CAGR points for 3 of drawdown).
- **PACK.4** The sleeve is read-only, no flag exists. Buying a tranche is a human's `/analyze`.
- **PACK.5** The pack lives under a new top-level `machinelearning/` (Maulik, 9 Nov 2026), not
  `legacy/python/research/`, because it is not legacy and will graduate into `baskfy_core.mb`.
- **PM0.1 (pending)** `lightgbm` enters the stack — outside `docs/02-tech-stack-adr.md`. Research-only
  until PM6; record in `docs/DECISIONS-MERGE.md` when it reaches `packages/core`.
- **PM3.1 (pending)** Sector treated as static (today's `industryInfo` applied to history) — a small,
  known leak accepted until a dated source exists; flagged in every table it feeds.
- **PM6.1 (pending)** Scoring stays a Python worker task; Rust port of a LightGBM tree walker deferred.
- **PM0.2** ⚠ UNREVIEWED Export reads index closes from `index_bar_daily` (2011→, keyed by `index_master.id`, not `index_def.id`), not `index_snapshot_daily` (2017→): bars now start 2011, so snapshots would null market features for 2011–17. Closes match the old file to 0.05 %. Mom50 stays on snapshots (unused beyond a dropped column). Container default `baskfy-local-postgres-1`, override `BASKFY_PG_CONTAINER`.
- **PM0.3** Env: `uv venv --python 3.12` (system default 3.15); LightGBM needs `brew install libomp`.
