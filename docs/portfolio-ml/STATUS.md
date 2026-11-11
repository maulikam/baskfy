# PM status

**Run opened:** 9 Nov 2026, from the research note of 8–9 Nov. Nothing in Baskfy proper is built.

| # | Module | State | Note |
|---|---|---|---|
| PM0 | Pack lands | ✅ green | Fresh export through 6 Nov 2026; §3/§4 and the ranking regenerated (9 Nov) |
| PM1 | Point-in-time fundamentals | ⚠️ code done, not green | Migration, NSE parsers, `fundamentals_pit backfill`, features and look-ahead tests committed. Verified end to end on 3 names in a throwaway DB. **AC2 coverage needs the full backfill** (`NEEDS-MAULIK.md`) |
| PM2 | Shareholding, point-in-time | code complete, backfill pending | Verified on 4 names, same hash on re-run. Feed starts Sep 2021 (PM2.1) |
| PM3 | Market cap, sector | code complete, data pending | Sector verified on 4 quotes; coverage and mcap check need the ship, a nightly and the backfill |
| PM0.7 | Dead names | code complete, data in throwaway DB | 501 names; ship the image, then run the backfill (`NEEDS-MAULIK.md`) |
| PM0.8 | Twelve years with the dead | ✅ done on merged export | STRATEGY §0, §3, §4, §6 rewritten |
| PM4 | four-way re-run | ❌ red for fundamentals (11 Nov) | price-only kept as the sleeve's model (PM6.0) though it also fails the 2024 and pooled gate natively (PM4.10); `out/pm4/PM4-RESULT.md` |
| PM9a | TimesFM-3 zero-shot features | ❌ not adopted (11 Nov) | raw forecast anti-predictive; as features, noise |
| PM0.18–0.19 | indicators (CMO/CCI/chop/ER); average-Sharpe features | ❌ no gain as inputs | Sharpe as a stand-alone ranker halves less; untested as a book |
| PM6 | nightly scorer | ✅ live locally (11 Nov) | migration 0007, `mb_task.py`, post-commit hook, Beat `mb-train`; needs the image and a first `train` (`NEEDS-MAULIK.md`) |
| PM7 | pages | ✅ live locally (11 Nov) | API `/mb/*` GET, web `/mb` (staff watchlist), migration 0008 (PM7.1–7.2) |
| PM8 | paper book | ✅ live locally (11 Nov) | migration 0009, nightly with the score, seed of 143 backtest tranches, web `/mb/paper` (PM8.1–8.3) |
| PM5, PM9 | | not started | after PM8 |

**Plan on (PM0.8, from 2014, dead included, every line marked, variant b):** top-20 skip-held 17.2 % CAGR / −66.3 % / Sharpe 0.75; top-10 skip-held 17.0 % / −72.6 % / 0.69; investable equal-weight 13.0 % / −55.8 %; Smallcap 250 16.3 % / −60.8 %.

⚠️ Earlier book figures, including the 35.7 % / −22.0 % below, froze lines that left the investable set and are overstated: 2020 → is 31.3 % / −35.2 % marked on real bars, 30.9 % / −34.9 % with the dead (b).

Research results (6 Nov export): investable top-20 book 35.7 % CAGR / −22.0 % / Sharpe 1.98 (Jan 2020 →
6 Nov 2026); 13 book levers all 33–37 %. Signal-year 2024: investable top-20 +5.1 % vs investable universe
+0.6 %, hit 18.8 % vs 7.5 % — passes PM4 AC1 on price-only. 2025 is the weak year (investable −2.2 % vs +3.9 %,
hit 4.5 % vs 5.4 %) and fails it; PM4 AC1 now requires both years (Maulik, 9 Nov 2026).

**Live expectation is set from 2025 until PM4 passes, not from the 2020–25 average.** Signal-year 2025 is the
newest walk-forward year and the price-only model showed no edge there: top-20 hit 4.5 % vs 5.4 % base, −2.2 % vs
+3.9 %. The folds are out of sample for training, but the design was chosen with 2025 visible (10 Sep export).

**Did the edge exist before 2020?** On survivors-only data the ranking did (AUC 0.58–0.80, 2014–19), the return edge only in rising years: top-20 lagged the universe by 12 points in signal-years 2018 and 2019, as it did by 10–11 in 2024 and 2025.


**PM4 (11 Nov 2026): FAIL for fundamentals; price-only model stands.** See DECISIONS-PM PM4.6–4.10 and `machinelearning/python/research/portfolio-ml/out/pm4/PM4-RESULT.md`.
