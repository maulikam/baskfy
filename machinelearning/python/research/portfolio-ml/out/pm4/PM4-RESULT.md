# PM4 — result (11 Nov 2026, native Mac run, export of 11 Nov 03:23 IST)

**Verdict: FAIL for fundamentals. The price-only model is the sleeve's model (06 PM4 fallback, Maulik's PM6.0),
but it does not pass the gate either on this run.**

- AC1 (gate on model "all", 2024 / 2025 / pooled, covered rows): **FAIL** all three windows.
- AC2 (no year's AUC down > 0.02 vs price-only): **FAIL** — 2023 −0.027 (covered), −0.024 (full).
- AC3 (PM1–PM3 features in the top 12): PASS — 6 of 12 (log_mcap #1, mcap_decile #2, ebitda_margin_ttm #3,
  pat_margin_ttm #5, rev_accel #7, rev_q_yoy #9). Used, not generalising.
- Blank check: gap > 10 points (dead names blanker) → judged on common-coverage rows (PM4.4). Folds 2022–2025.

## Judged run (covered rows, 2022–2025, dead names, terminal b, full marks)

| Model | AUC | Top-20 hit (lift) | Halved | Book CAGR / max DD / Sharpe | Gate 2024 / 2025 / pooled |
|---|---|---|---|---|---|
| controls only | 0.629 | 18.9 % (1.60×) | 3.2 % | 14.0 % / −34.4 % / 0.70 | F / F / F |
| controls + fundamentals | 0.611 | 19.9 % (1.68×) | 1.7 % | 18.1 % / −33.3 % / 0.88 | F / F / F |
| controls + technicals (price-only) | 0.649 | 22.1 % (1.87×) | 4.3 % | 19.2 % / −33.0 % / 0.92 | F / P / F |
| all | 0.630 | 20.4 % (1.72×) | 2.2 % | 17.3 % / −36.0 % / 0.82 | F / F / F |
| all, sector cap 3 | — | — | — | 16.2 % / −33.8 % / 0.78 | — |

AUC all − price-only by year: 2022 +0.008, 2023 −0.027, 2024 −0.006, 2025 +0.021.
Price-only on the gate: 2024 top-20 −0.8 % vs universe +0.8 % (hit 16.3 %); 2025 +5.3 % vs +4.1 % (hit 10.0 % vs 5.3 %);
pooled +2.1 % vs +2.3 %.

## Full universe (reference, 2020–2025)

| Model | AUC | Top-20 hit (lift) | Book CAGR / max DD / Sharpe | Gate 2024 / 2025 / pooled |
|---|---|---|---|---|
| controls only | 0.621 | 25.5 % (1.75×) | 25.4 % / −33.7 % / 1.07 | P / F / F |
| controls + fundamentals | 0.639 | 25.3 % (1.73×) | 24.0 % / −35.0 % / 1.03 | F / F / F |
| controls + technicals (price-only) | 0.643 | 30.1 % (2.06×) | 32.0 % / −33.5 % / 1.31 | P / F / F |
| all | 0.642 | 29.1 % (1.99×) | 30.2 % / −35.7 % / 1.26 | F / F / F |

AUC all − price-only by year: +0.001, −0.022, −0.003, −0.024, +0.003, +0.022. Books here start 2020 (bull window).
Price-only 2025 on the full universe: top-20 −3.8 % vs +4.0 %, hit 3.6 % vs 5.3 % — the known 2025 failure.

## Native vs the cloud run (PM4.8, PM4.10)

The first run was in the agent's cloud workspace on what was meant to be the same export. The native run here
differs: about 100 more labelled training rows per fold (2022 positives 8,722 vs 8,687) and different picks. A
fresh rebuild of the dataset from the export reproduces the native tables exactly, so the native tables stand
and the committed CSVs are native. Verdicts are unchanged (AC1 FAIL, AC2 FAIL, AC3 6). What changed: the cloud
run had the price-only arm passing all three windows on covered rows; natively it fails 2024 and pooled. On
the full universe it fails 2025 and pooled in both runs.

## The covered-rows 2025 effect (isolate_filter.py, cloud run)

Restricting the pool to filers does not explain the covered-mode 2025 pass: full-trained price-only scores
with picks limited to filers hit 4.4 % vs 5.1 % base in 2025, like the full pool (96 % of investable rows are
filers). The difference is the 2019→ training set. One-year noise; not adopted.

## Reading

Fundamentals as built (TTM growth, margins, ROCE, D/E, CFO/PAT from 2021–22; shareholding from Sep 2021;
market cap; is_bank) add no out-of-sample ranking value over price and volume on 2022–2025. Four test years
are a first reading; the shareholding and balance-sheet layers have almost no training history yet.

The price-only model's honest standing: 2014→ book of record ~17 % / −66 % (STRATEGY §4); 2020→ books ~32 % /
−34 % are the bull window; no edge in 2025 on the full universe, and it fails the 2024 window on covered rows.
The sleeve proceeds read-only (PM6.0); the paper book (PM8) is the test, and the shadow lines (63-day trigger at
1/20 and 1/16; Smallcap-250 100-DMA exit) carry unchanged.

## Notes

- Memory-only changes made for the cloud run (`features.cross_sectional` in place, `build_dataset.py` chunk
  spill and sunk join, `run_pm4.blank_rates` grouped), and the `portfolioml/pipeline.py` refactor after them,
  are numerically neutral: a rebuild with the current code gives the same PM4 tables as the dataset already on the Mac.
