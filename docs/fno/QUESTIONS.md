# QUESTIONS — for Maulik, each with the standing default the FO run proceeds on

The run never waits on these. An answer here, or in NEEDS-MAULIK § F&O, becomes a
`DECISIONS-FO.md` entry, and the module that reads it adjusts. **Q1 and Q2 must be answered before
any FO flag can be flipped** (`02` §3). Nothing before that point depends on them.

| # | Question | Standing default |
|---|---|---|
| Q1 | **Sleeve capitals**: ₹ for each sleeve in `01`, and the FO book's total. Is the F&O margin pool cash, or pledged collateral? | All **₹0**. Paper runs one structure per plan and measures in R; live refuses `NO_SLEEVE_CAPITAL` |
| Q2 | **Non-negotiable 4 for derivatives.** (a) A future (long *or short*) gets a broker-side GTT stop the same session, at the tighter of the sleeve's ATR stop and `stop_from_vol()`. (b) A defined-risk option structure's stop is its structure (max loss fixed at entry), and it gets **no** GTT, because a GTT on the long leg would un-hedge the short. Do you accept both readings? | (a) as written. (b) as written, but **no FO option order leaves paper until you answer** (`02` §2.4; PACK.3) |
| Q3 | **Exit day for stock derivatives**: `E−1` or `E−4` (before the delivery-margin ramp)? | **Moot in v1**: no stock sleeve is built. The research found E−4 worse than E−1 for stock condors (`RESEARCH.md` §B1). F1 is index, cash-settled, and exits at E−1 |
| Q4 | **Do you want any of the rejected families built anyway**, knowing `RESEARCH.md`'s numbers? For example stock-futures trend long-only (+0.033R, but only 2023 was positive) | No. The quarterly re-test watches them (PACK.6) |
| Q5 | **The hedge overlay** (`01` §4, `RESEARCH.md` §D): should the FO book ever hedge the *equity* books (swing, TWT, VBT, weekly) with an index future when the regime turns? It touches the Track C §7 wall between books | Not built. Recorded as research only. It needs your decision *and* a small-cap index series the bhavcopy does not carry |
| Q6 | **The SGB as collateral.** Pledging `SGBDE31III-GB` would fund F&O margin without selling it. That is your act at Zerodha, never code (the SGB is untouchable). Do you want it considered at all? | Not considered. The FO book's margin is whatever cash the account has, and the plan refuses when it will not fit |
| Q7 | **Paper periods** (`04` §9): long enough? | As proposed |
| Q8 | **Earnings.** No free point-in-time results calendar is wired, so no sleeve trades *around* results, and the scan cannot yet skip a name whose results fall inside a hold. Should FO3 wire NSE's event calendar (the existing `results_calendar` read) as a skip filter from the day it starts collecting? | Yes. It is forward-only, so the backtests cannot use it, and the pages say so |
| Q9 | Turn on the **nightly bhavcopy ingest and scans** on the box as soon as FO2/FO5 are green? | Yes. They move no money and make ~1 NSE request a night |
