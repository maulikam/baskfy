# QUESTIONS — for Maulik, each with the standing default the FO run proceeds on

The run never waits on these. An answer here, or in NEEDS-MAULIK § F&O, becomes a
`DECISIONS-FO.md` entry, and the module that reads it adjusts. Q1, Q2 and Q4 were answered in session on 23
Sep 2026 (`DECISIONS-FO` M.1). The rest carry their defaults.

| # | Question | Standing default |
|---|---|---|
| Q1 | **Sleeve capitals** | **Answered 23 Sep 2026 (M.1): F1 ₹10 lakh.** F2 stays ₹0 (one lot on paper) until asked |
| Q2 | **Answered 23 Sep 2026 (M.1): "Structure stop + close order"**, loss close at 1.5 × credit; futures get a GTT. The original question: **Non-negotiable 4 for derivatives.** (a) A future (long *or short*) gets a broker-side GTT stop the same session, at the tighter of the sleeve's ATR stop and `stop_from_vol()`. (b) A defined-risk option structure's stop is its structure (max loss fixed at entry), and it gets **no** GTT, because a GTT on the long leg would un-hedge the short. Do you accept both readings? | (a) as written. (b) as written, but **no FO option order leaves paper until you answer** (`02` §2.4; PACK.3) |
| Q3 | **Exit day for stock derivatives**: `E−1` or `E−4` (before the delivery-margin ramp)? | **Moot in v1**: no stock sleeve is built. The research found E−4 worse than E−1 for stock condors (`RESEARCH.md` §B1). F1 is index, cash-settled, and exits at E−1 |
| Q4 | Build a rejected family anyway? | **Answered 23 Sep 2026 (M.1): futures trend, long only → F2, paper** |
| Q5 | **The hedge overlay** (`01` §4, `RESEARCH.md` §D): should the FO book ever hedge the *equity* books (swing, TWT, VBT, weekly) with an index future when the regime turns? It touches the Track C §7 wall between books | Not built. Recorded as research only. It needs your decision *and* a small-cap index series the bhavcopy does not carry |
| Q6 | **The SGB as collateral.** Pledging `SGBDE31III-GB` would fund F&O margin without selling it. That is your act at Zerodha, never code (the SGB is untouchable). Do you want it considered at all? | Not considered. The FO book's margin is whatever cash the account has, and the plan refuses when it will not fit |
| Q7 | **Paper periods** (`04` §9): long enough? | As proposed |
| Q8 | **Earnings.** No free point-in-time results calendar is wired, so no sleeve trades *around* results, and the scan cannot yet skip a name whose results fall inside a hold. Should FO3 wire NSE's event calendar (the existing `results_calendar` read) as a skip filter from the day it starts collecting? | Yes. It is forward-only, so the backtests cannot use it, and the pages say so |
| Q9 | Turn on the **nightly bhavcopy ingest and scans** on the box as soon as FO2/FO5 are green? | Yes. They move no money and make ~1 NSE request a night |
| Q10 | **F3's capital** (M.5). ₹0 sizes paper to one lot; a live size needs a number | ₹0 until you say; the scan records what live would have sized at the ceiling |
| Q11 | **F3's numbers** (`04` §11): the rolling five-session weekly candle, the ten-bar 75-minute confirm, the 0.10 % level buffer, the 2 % wing, the 2 × credit cut, the 15:00 expiry-day hard exit | As written; each is one field of `F3Config` |
| Q12 | **`BASKFY_FNO_F3_AUTO_EXIT` on the box** — your hand, like TW18's. Paper only until `02` §3 | Off. The monitor raises the exit plan and an alert and waits for the click |
