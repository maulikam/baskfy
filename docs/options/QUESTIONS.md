# QUESTIONS — for Maulik, each with the standing default the run proceeds on

The run never waits on these. An answer here or in NEEDS-MAULIK § Options becomes a
`DECISIONS-OP.md` entry and the module that reads it adjusts.

| # | Question | Standing default |
|---|---|---|
| Q1 | **Sleeve capitals** — ₹ for O1-M, O1-W, O2, O3 (and the account and margin pool) | All **₹0**. Paper runs one lot per plan (PACK.6); live refuses `NO_SLEEVE_CAPITAL` |
| Q2 | **Risk per trade** as % of each sleeve's capital | O1-M 1.0 %, O1-W 0.5 %, O2 0.5 %, O3 0.5 %; max lots 3 / 2 / 2 / 2; ceilings ₹25,000 and 1 % |
| Q3 | **Loss limits** — per sleeve and for the book | Per sleeve: day −2R, week −4R, month −8R. Book: day 1.5 % and month 5 % of total options capital once set, capped at ₹30,000 / ₹75,000 |
| Q4 | **Event days for FY 2026–27**: the RBI MPC decision dates, Budget day, any election-result date, anything else you want skipped | The run seeds only dates it can verify from the RBI / Finance Ministry primary source, each with its URL; anything else you add on `/options/calendar`. **OP2 seeded** the six RBI MPC decision days (8 Apr, 5 Jun, 5 Aug, 7 Oct, 4 Dec 2026, 5 Feb 2027); the Budget 2027-28 date is not yet announced, so not seeded (OP2.6) |
| Q5 | Will you buy **minute-level historical NIFTY option data** (a vendor) for Tier 3? Which vendor, which years? | No purchase. Tier 3 runs on the collector's own days; the gate relies on the paper periods plus a small Tier 3 |
| Q6 | PACK.2 — do you accept that **one confirm covers each sleeve's rule-driven exits** and hard exit? | Yes, as written; each Confirm button says so verbatim |
| Q7 | **O1-W**: expiry day only (PACK.7), or also the day before? | Expiry day only; Monday chains are collected so it can be tested later |
| Q8 | **O2 strike**: one step ITM (default) or ATM? Stop 30 %, target 60 %, time stop 45 min — OK to start? | One step ITM; 30 / 60 / 45 as proposed; Tier 1–3 sensitivity reported, nothing changed without you |
| Q9 | **O3**: are the two setups (range-break and gap-hold debit spreads) what you meant by expiry-day setups, or did you have specific ones in mind? | The two in `01` §4; any setup you name becomes a config group and a decision |
| Q10 | **Paper periods** (O1-M 6 monthly, O1-W 12 weekly, O2 60 sessions, O3 20 expiry days) — long enough? | As proposed (PACK.13) |
| Q11 | Is the **F&O segment** active on the Zerodha account the desk trades, and what does Zerodha now require of API orders (algo tagging / registration)? | OP0 reads `margins()` and the broker's published requirements and records them; nothing depends on it until a flag |
| Q12 | Turn on the **collector** on the box as soon as OP3 is green, so the forward dataset starts before the run finishes? | Yes if the limiter measurement is green (PACK.11) — it moves no money; the run records the day it started |
| Q13 | Delegation of any flag flip (PACK.14) | Not delegated. The run ends at `02` §3 with everything drilled and every money flag false |
