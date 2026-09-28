# F3 — the EOD re-test of the daily rules (F3-3)

Run 28 Sep 2026 14:25 IST by `tools/fno/f3_retest.py` over 1165 archived F&O bhavcopies, 2022-01-03 → 2026-09-22, through `NSEProvider.fo_bhavcopy` from the local archive. Rules and numbers: `docs/fno/04` §11 with `F3Config`'s defaults (pivot 60/3, trend 20, weekly 5, distance 1.0 %, wing 2.0 %, decay 80 %, cut 2.0x, buffer 0.10 %, add at 20 %). One lot per entry, one add; R = net P&L per unit ÷ the spread's max loss per unit; costs are the research's index-option costs per leg per crossing.

## What this cannot test — read first

The bhavcopy is a closing file. The proxy **cannot test** and claims no number for:

* the 75-minute confirm;
* the intraday alignment with the session's open;
* the intraday cut at the moment the level breaks (the proxy exits at that session's settle);
* the loss cut read from a live mark (the proxy reads the settle);

The method's edge, as Maulik describes it, lives in exactly those intraday rules. What is tested is the daily half: the levels and direction at the close, the next-session entry at settle, the mark at each settle, the level break on the session's high or low (exited at that session's settle — worse than his rule when the break comes early, better when it reverses), the loss cut and the decay target at settle, the expiry-day settle, the next-session add. The index bars are the **front-month future's** OHLC, a basis away from the cash index. Treat every number below as a floor for the daily half and as silence about the intraday half.

## Headline

| Sleeve | n | exp R | t | win | worst | gross R | cost R |
|---|---|---|---|---|---|---|---|
| NIFTY | 206 | -0.021 | -2.26 | 67% | -0.85 | -0.015 | 0.006 |
| BANKNIFTY | 166 | -0.020 | -1.60 | 57% | -0.69 | -0.008 | 0.012 |

372 trades in all.

**Reading.** After costs the daily half loses on both underlyings in this proxy: the decay-target wins are small (a few percent of max loss each) and the level breaks and loss cuts, exited at the settle, give back more than the wins collect. Gross of costs the result is near zero, so the daily rules alone carry no edge here; whatever edge the method has must come from the intraday half this file cannot see. That is consistent with `01` §1c's honest limit and is why F3 runs on paper. Nothing here is a reason to turn it on, and nothing here disproves the intraday rules either.

## NIFTY

Per year (by entry):

| Year | n | exp R | win |
|---|---|---|---|
| 2022 | 41 | -0.004 | 76% |
| 2023 | 35 | -0.002 | 71% |
| 2024 | 47 | -0.048 | 60% |
| 2025 | 49 | -0.027 | 65% |
| 2026 | 34 | -0.017 | 62% |

By exit:

| Exit | n | exp R | mean sessions |
|---|---|---|---|
| DECAY_TARGET | 106 | +0.034 | 2.8 |
| HARD_EXIT | 19 | -0.029 | 3.9 |
| LEVEL_BREAK | 58 | -0.073 | 1.7 |
| LOSS_CUT | 22 | -0.145 | 1.9 |
| OPEN_AT_END | 1 | +0.001 | 1.0 |

Signals not entered: thin_credit 392.

Trades (first 15):

| signal | entry | exit | expiry | direction | option_type | short | wing | credit | lots | reason | R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2022-03-30 | 2022-03-31 | 2022-04-04 | 2022-04-07 | UP | PE | 16850.0 | 16450.0 | 18.35 | 2 | DECAY_TARGET | +0.021 |
| 2022-04-05 | 2022-04-06 | 2022-04-12 | 2022-04-13 | UP | PE | 17200.0 | 16800.0 | 16.95 | 2 | DECAY_TARGET | +0.018 |
| 2022-04-12 | 2022-04-13 | 2022-04-18 | 2022-04-21 | UP | PE | 17300.0 | 16900.0 | 50.8 | 1 | LOSS_CUT | -0.282 |
| 2022-04-20 | 2022-04-21 | 2022-04-22 | 2022-04-28 | DOWN | CE | 17900.0 | 18250.0 | 12.9 | 1 | DECAY_TARGET | +0.024 |
| 2022-04-26 | 2022-04-27 | 2022-05-02 | 2022-05-05 | DOWN | CE | 17650.0 | 18000.0 | 15.6 | 2 | DECAY_TARGET | +0.026 |
| 2022-05-02 | 2022-05-04 | 2022-05-05 | 2022-05-12 | DOWN | CE | 17600.0 | 17950.0 | 9.25 | 1 | DECAY_TARGET | +0.015 |
| 2022-05-17 | 2022-05-18 | 2022-05-19 | 2022-05-26 | DOWN | CE | 16500.0 | 16850.0 | 59.1 | 1 | DECAY_TARGET | +0.161 |
| 2022-05-19 | 2022-05-20 | 2022-05-24 | 2022-05-26 | DOWN | CE | 16550.0 | 16900.0 | 47.8 | 2 | DECAY_TARGET | +0.093 |
| 2022-05-24 | 2022-05-25 | 2022-05-30 | 2022-06-02 | DOWN | CE | 16600.0 | 16950.0 | 26.7 | 1 | LOSS_CUT | -0.336 |
| 2022-05-31 | 2022-06-01 | 2022-06-06 | 2022-06-09 | UP | PE | 15700.0 | 15350.0 | 13.15 | 2 | DECAY_TARGET | +0.017 |
| 2022-06-06 | 2022-06-07 | 2022-06-09 | 2022-06-09 | UP | PE | 16250.0 | 15900.0 | 42.05 | 2 | HARD_EXIT | +0.109 |
| 2022-06-09 | 2022-06-10 | 2022-06-13 | 2022-06-16 | UP | PE | 16100.0 | 15750.0 | 76.9 | 1 | LEVEL_BREAK | -0.558 |
| 2022-06-21 | 2022-06-22 | 2022-06-23 | 2022-06-30 | DOWN | CE | 16050.0 | 16400.0 | 9.7 | 1 | LOSS_CUT | -0.035 |
| 2022-06-23 | 2022-06-24 | 2022-06-30 | 2022-06-30 | DOWN | CE | 15900.0 | 16250.0 | 59.65 | 2 | HARD_EXIT | +0.152 |
| 2022-06-30 | 2022-07-01 | 2022-07-05 | 2022-07-07 | DOWN | CE | 16100.0 | 16450.0 | 29.2 | 1 | LEVEL_BREAK | +0.059 |

## BANKNIFTY

Per year (by entry):

| Year | n | exp R | win |
|---|---|---|---|
| 2022 | 29 | +0.045 | 69% |
| 2023 | 40 | -0.033 | 62% |
| 2024 | 42 | -0.037 | 48% |
| 2025 | 28 | -0.007 | 54% |
| 2026 | 27 | -0.060 | 52% |

By exit:

| Exit | n | exp R | mean sessions |
|---|---|---|---|
| DECAY_TARGET | 77 | +0.079 | 7.3 |
| HARD_EXIT | 3 | +0.101 | 16.0 |
| LEVEL_BREAK | 61 | -0.093 | 3.3 |
| LOSS_CUT | 24 | -0.171 | 4.5 |
| OPEN_AT_END | 1 | -0.003 | 0.0 |

Signals not entered: short_not_traded 4, thin_credit 17.

Trades (first 15):

| signal | entry | exit | expiry | direction | option_type | short | wing | credit | lots | reason | R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2022-03-30 | 2022-03-31 | 2022-04-12 | 2022-04-28 | UP | PE | 34700.0 | 33900.0 | 140.9 | 2 | DECAY_TARGET | +0.167 |
| 2022-04-12 | 2022-04-13 | 2022-04-18 | 2022-04-28 | UP | PE | 37000.0 | 36200.0 | 193.65 | 1 | LEVEL_BREAK | -0.220 |
| 2022-04-18 | 2022-04-19 | 2022-04-25 | 2022-04-28 | DOWN | CE | 38500.0 | 39300.0 | 23.2 | 2 | DECAY_TARGET | +0.013 |
| 2022-04-25 | 2022-04-26 | 2022-05-06 | 2022-05-26 | DOWN | CE | 37700.0 | 38500.0 | 196.3 | 2 | DECAY_TARGET | +0.225 |
| 2022-05-06 | 2022-05-09 | 2022-05-13 | 2022-05-26 | DOWN | CE | 37200.0 | 37900.0 | 24.3 | 2 | DECAY_TARGET | +0.008 |
| 2022-05-13 | 2022-05-16 | 2022-05-17 | 2022-05-26 | DOWN | CE | 35200.0 | 35900.0 | 55.15 | 1 | LOSS_CUT | -0.100 |
| 2022-05-17 | 2022-05-18 | 2022-05-19 | 2022-05-26 | DOWN | CE | 35200.0 | 35900.0 | 81.65 | 1 | DECAY_TARGET | +0.095 |
| 2022-05-19 | 2022-05-20 | 2022-05-25 | 2022-06-30 | DOWN | CE | 35000.0 | 35700.0 | 263.95 | 1 | DECAY_TARGET | +0.645 |
| 2022-05-25 | 2022-05-26 | 2022-06-14 | 2022-06-30 | DOWN | CE | 35200.0 | 35900.0 | 353.4 | 2 | DECAY_TARGET | +0.570 |
| 2022-06-14 | 2022-06-15 | 2022-06-20 | 2022-06-30 | DOWN | CE | 36000.0 | 36700.0 | 24.75 | 2 | DECAY_TARGET | +0.008 |
| 2022-06-20 | 2022-06-21 | 2022-06-29 | 2022-06-30 | DOWN | CE | 34200.0 | 34900.0 | 96.9 | 2 | DECAY_TARGET | +0.083 |
| 2022-06-29 | 2022-06-30 | 2022-07-07 | 2022-07-28 | DOWN | CE | 34600.0 | 35300.0 | 177.6 | 1 | LOSS_CUT | -0.410 |
| 2022-07-07 | 2022-07-08 | 2022-07-18 | 2022-07-28 | UP | PE | 32800.0 | 32100.0 | 40.6 | 2 | DECAY_TARGET | +0.027 |
| 2022-07-18 | 2022-07-19 | 2022-07-22 | 2022-07-28 | UP | PE | 34100.0 | 33300.0 | 32.15 | 2 | DECAY_TARGET | +0.019 |
| 2022-07-22 | 2022-07-25 | 2022-08-08 | 2022-08-25 | UP | PE | 34500.0 | 33700.0 | 89.55 | 2 | DECAY_TARGET | +0.061 |

## What was not run

* F3 is **not** in the quarterly re-test's families (`baskfy_core.fno.retest.FAMILIES`): the worker's option loader filters to the two nearest expiries with a future, which drops the NIFTY weeklies F3N trades. Registering it needs a raw-options loader; recorded in F3-PLAN.
* No parameter was tuned; the numbers are `04` §11's defaults, first run.
