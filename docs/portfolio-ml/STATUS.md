# PM status

**Run opened:** 9 Nov 2026, from the research note of 8–9 Nov. Nothing in Baskfy proper is built.

| # | Module | State | Note |
|---|---|---|---|
| PM0 | Pack lands | ✅ green | Fresh export through 6 Nov 2026; §3/§4 and the ranking regenerated (9 Nov) |
| PM1 | Point-in-time fundamentals | ⚠️ code done, not green | Migration, NSE parsers, `fundamentals_pit backfill`, features and look-ahead tests committed. Verified end to end on 3 names in a throwaway DB. **AC2 coverage needs the full backfill** (`NEEDS-MAULIK.md`) |
| PM2 | Shareholding, point-in-time | code complete, backfill pending | Verified on 4 names, same hash on re-run. Feed starts Sep 2021 (PM2.1) |
| PM3 | Market cap, sector | code complete, data pending | Sector verified on 4 quotes; coverage and mcap check need the ship, a nightly and the backfill |
| PM4 | The re-run | prepared, not run | `run_pm4.py` refuses below 70 % coverage |
| PM5–PM9 | | not started | |

Research results (6 Nov export): investable top-20 book 35.7 % CAGR / −22.0 % / Sharpe 1.98 (Jan 2020 →
6 Nov 2026); 13 book levers all 33–37 %. Signal-year 2024: investable top-20 +5.1 % vs investable universe
+0.6 %, hit 18.8 % vs 7.5 % — passes PM4 AC1 on price-only. 2025 is the weak year (investable −2.2 % vs +3.9 %,
hit 4.5 % vs 5.4 %) and fails it; PM4 AC1 now requires both years (Maulik, 9 Nov 2026).

**Live expectation is set from 2025 until PM4 passes, not from the 2020–25 average.** Signal-year 2025 is the
newest walk-forward year and the price-only model showed no edge there: top-20 hit 4.5 % vs 5.4 % base, −2.2 % vs
+3.9 %. The folds are out of sample for training, but the design was chosen with 2025 visible (10 Sep export).
