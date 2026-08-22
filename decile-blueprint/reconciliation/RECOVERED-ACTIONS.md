# Corporate actions recovered from the Kite/bhavcopy ratio
Generated 22 Aug 2026 over the 271-symbol reference corpus, 268 of which had both series. **Nothing here has been written to `corporate_action`.** This is evidence for the decision recorded as M24 in `docs/DECISIONS-MERGE.md`.
## Method
`ohlcv_daily.close_raw` is the NSE bhavcopy's exchange print. Kite's `historical_data` returns *adjusted* history — measured, and contrary to what `docs/09` assumes. So `close_raw / kite_close` is the cumulative adjustment still owed at each date, and every step in it is a corporate action: the step's date is the ex-date, the size of the step is the factor.
A step counts as **confirmed** when the ratio is flat to 0.5% for 5 trading days on both sides. A one-day glitch in either series produces two opposite steps a day apart and fails that test; a corporate action passes it.
`split/bonus` versus `cash/other` is decided by whether the factor is a ratio of small integers (denominator ≤ 20, within 0.2%). A 5:1 split is exactly 5. A dividend is `P/(P−D)`, which is not a round fraction.
## Summary
| | |
|---|---|
| symbols compared | 268 |
| symbols carrying an unapplied action | 65 |
| actions recovered | 85 |
| — share-count (split/bonus) | 47 |
| — cash/other (dividend-shaped) | 38 |
| confirmed by the flank test | 83 |

## Share-count actions — splits and bonuses
| symbol | ex-date | factor | ratio | confirmed |
|---|---|---|---|---|
| ADANIPOWER | 2025-09-22 | 5.0000 | 5 | yes |
| ANANDRATHI | 2025-03-05 | 1.9999 | 2 | yes |
| ANANDRATHI | 2026-06-03 | 2.0000 | 2 | yes |
| ANGELONE | 2026-02-26 | 10.0007 | 10 | yes |
| ASHOKLEY | 2025-07-16 | 2.0000 | 2 | yes |
| BAJFINANCE | 2025-06-16 | 9.9988 | 10 | yes |
| BANCOINDIA | 2024-12-30 | 2.0000 | 2 | yes |
| BSE | 2025-05-23 | 2.9999 | 3 | yes |
| CANBK | 2024-05-15 | 5.0065 | 5 | yes |
| CGCL | 2024-03-05 | 3.9999 | 4 | yes |
| COFORGE | 2025-06-04 | 5.0000 | 5 | yes |
| CUB | 2026-06-12 | 1.3333 | 4/3 | yes |
| CUPID | 2026-03-09 | 4.9997 | 5 | yes |
| GAEL | 2024-03-15 | 2.0001 | 2 | yes |
| GOKULAGRO | 2025-10-14 | 2.0001 | 2 | yes |
| GPIL | 2024-10-04 | 5.0000 | 5 | yes |
| HEG | 2024-10-18 | 5.0001 | 5 | yes |
| INDIAGLYCO | 2025-08-12 | 2.0000 | 2 | yes |
| JINDALSAW | 2024-10-09 | 2.0000 | 2 | yes |
| JLHL | 2026-07-24 | 5.0001 | 5 | yes |
| KARURVYSYA | 2025-08-26 | 1.2000 | 6/5 | yes |
| KIRLPNU | 2026-08-18 | 1.9999 | 2 | window edge |
| LALPATHLAB | 2025-12-19 | 2.0000 | 2 | yes |
| MANORAMA | 2024-03-07 | 4.9998 | 5 | yes |
| MCX | 2026-01-02 | 5.0003 | 5 | yes |
| MOTHERSON | 2025-07-18 | 1.5000 | 3/2 | yes |
| NESTLEIND | 2024-01-05 | 9.9998 | 10 | window edge |
| NESTLEIND | 2025-08-08 | 2.0001 | 2 | yes |
| NMDC | 2024-12-27 | 3.0002 | 3 | yes |
| NUVAMA | 2025-12-26 | 4.9995 | 5 | yes |
| OIL | 2024-07-02 | 1.5000 | 3/2 | yes |
| PARAS | 2025-07-04 | 2.0000 | 2 | yes |
| PGEL | 2024-07-10 | 9.9999 | 10 | yes |
| PHOENIXLTD | 2024-09-20 | 2.0000 | 2 | yes |
| PIDILITIND | 2025-09-23 | 2.0000 | 2 | yes |
| QUESS | 2025-04-30 | 2.3975 | 12/5 | yes |
| SANDUMA | 2024-02-02 | 6.0006 | 6 | yes |
| SANDUMA | 2025-09-22 | 2.9998 | 3 | yes |
| SHILPAMED | 2025-10-03 | 2.0000 | 2 | yes |
| SHRIRAMFIN | 2025-01-10 | 5.0001 | 5 | yes |
| SIEMENS | 2025-04-07 | 1.3127 | 21/16 | yes |
| SKYGOLD | 2025-01-27 | 10.0003 | 10 | yes |
| STAR | 2024-12-20 | 1.1192 | 19/17 | yes |
| THYROCARE | 2025-11-28 | 2.9999 | 3 | yes |
| V2RETAIL | 2026-03-25 | 9.9999 | 10 | yes |
| ZFCVINDIA | 2026-06-24 | 6.0000 | 6 | yes |
| ZYDUSWELL | 2025-09-18 | 5.0000 | 5 | yes |

## Cash and other — dividend-shaped
| symbol | ex-date | factor | ratio | confirmed |
|---|---|---|---|---|
| ABSLAMC | 2026-07-22 | 1.0251 | — | yes |
| ADANIENT | 2025-11-17 | 1.0315 | — | yes |
| ASHOKLEY | 2024-04-03 | 1.0296 | — | yes |
| BAJAJ-AUTO | 2025-06-20 | 1.0253 | — | yes |
| CANBK | 2024-06-14 | 1.0273 | — | yes |
| CANBK | 2025-06-13 | 1.0358 | — | yes |
| CANBK | 2026-06-12 | 1.0330 | — | yes |
| CHENNPETRO | 2026-08-07 | 1.0436 | — | yes |
| ETHOSLTD | 2025-06-12 | 1.0353 | — | yes |
| GNFC | 2024-09-06 | 1.0244 | — | yes |
| GNFC | 2025-09-02 | 1.0347 | — | yes |
| HEROMOTOCO | 2024-02-21 | 1.0212 | — | yes |
| HEROMOTOCO | 2025-02-12 | 1.0251 | — | yes |
| IIFL | 2024-04-23 | 1.0295 | — | yes |
| INDIANB | 2025-06-10 | 1.0255 | — | yes |
| INDIANB | 2026-06-10 | 1.0213 | — | yes |
| INDUSTOWER | 2026-08-10 | 1.0376 | — | yes |
| KTKBANK | 2025-09-16 | 1.0283 | — | yes |
| LLOYDSENGG | 2025-04-28 | 1.1308 | — | yes |
| M&MFIN | 2024-07-16 | 1.0214 | — | yes |
| M&MFIN | 2025-05-14 | 1.0277 | — | yes |
| M&MFIN | 2025-07-15 | 1.0249 | — | yes |
| M&MFIN | 2026-07-13 | 1.0225 | — | yes |
| MRPL | 2026-03-11 | 1.0211 | — | yes |
| NATIONALUM | 2025-02-14 | 1.0213 | — | yes |
| NMDC | 2024-02-27 | 1.0251 | — | yes |
| NMDC | 2025-03-21 | 1.0346 | — | yes |
| NMDC | 2026-02-13 | 1.0304 | — | yes |
| OFSS | 2024-05-07 | 1.0322 | — | yes |
| OFSS | 2025-05-08 | 1.0321 | — | yes |
| OFSS | 2026-05-07 | 1.0287 | — | yes |
| QUESS | 2026-02-06 | 1.0234 | — | yes |
| REDINGTON | 2026-07-03 | 1.0220 | — | yes |
| SCI | 2025-09-04 | 1.0306 | — | yes |
| SOUTHBANK | 2024-02-27 | 1.0883 | — | yes |
| TATASTEEL | 2024-06-21 | 1.0202 | — | yes |
| TATASTEEL | 2025-06-06 | 1.0233 | — | yes |
| THANGAMAYL | 2025-02-11 | 1.0322 | — | yes |

---

## The dividend question, settled by measurement

Generated by `uv run python -m reconciliation.dividend_convention --write`.

M24 recovered 85 actions and split them in two: 47 share-count (splits and bonuses) and
38 cash-shaped (dividends). Applying the second set turns every return in the screener
from a *price* return into a *total* return, which reorders the ranking and changes what
the desk buys — a product decision, not a bug fix. The reference corpus is the answer key
the merge is graded against, so it was asked rather than anyone's preference.

Both conventions are computed over the identical window, so any residual window error is
common to both and cancels. A row only votes where a dividend actually falls inside the
window; everywhere else the two series are identical by construction.

symbols carrying a dividend and enough history : 25
symbol-windows compared                        : 125
  price                42
  total                3
  tie                  0
  indistinguishable    80

deciding rows                : 45  price 42  total 3
EXACT match at 2dp — price   : 75
EXACT match at 2dp — total   : 56

By window (deciding rows only; 9M and 12M carry the known window-length residual):

| window | price wins | total wins | price exact | total exact |
|---|---|---|---|---|
| 1M | 3 | 0 | 25 | 22 |
| 3M | 7 | 0 | 25 | 18 |
| 6M | 9 | 0 | 25 | 16 |
| 9M | 9 | 2 | 0 | 0 |
| 12M | 14 | 1 | 0 | 0 |

Cross-check over **all 271 corpus rows**, exact matches at stored precision:

| window | rows compared | today (nothing applied) | price (splits+bonuses) | total (all 85) |
|---|---|---|---|---|
| 1M | 269 | 264 | 266 | 263 |
| 3M | 269 | 260 | 263 | 256 |
| 6M | 269 | 255 | 261 | 252 |

**VERDICT: PRICE RETURN.**

Two independent readings agree. In the vote, the price convention wins 42 of 45 deciding
rows. In the per-window breakdown it matches **all 25 of 25** dividend-paying symbols
*exactly* at stored precision on 1M, 3M and 6M — the three windows M11 established
reproduce — while the total convention matches only where no dividend falls inside. And
in the 271-row cross-check, applying the splits and bonuses moves exact matches up at
every window, while applying the dividends on top pushes them below even today's
unadjusted baseline.

9M and 12M match exactly under neither convention, which is the known window-length
residual rather than an adjustment question: the seeded calendar is short about nine
lunar-calendar holidays a year, so those two windows resolve long. All three of the
total-return 'wins' sit there, where both conventions are wrong and total is accidentally
the nearer of two misses.

### The deciding rows

| symbol | window | corpus | price-return | total-return | error (price) | error (total) | wins |
|---|---|---|---|---|---|---|---|
| CHENNPETRO | 12M | 113.98 | 123.91 | 133.67 | 9.93 | 19.69 | price |
| CHENNPETRO | 6M | 56.62 | 56.62 | 63.45 | 0.00 | 6.83 | price |
| CHENNPETRO | 3M | 42.92 | 42.92 | 49.15 | 0.00 | 6.23 | price |
| CHENNPETRO | 9M | 32.4 | 28.04 | 33.62 | 4.36 | 1.22 | total |
| KTKBANK | 12M | 80.22 | 81.86 | 87.01 | 1.64 | 6.79 | price |
| CHENNPETRO | 1M | 15.65 | 15.65 | 20.69 | 0.00 | 5.04 | price |
| OFSS | 6M | 74.73 | 74.73 | 79.74 | 0.00 | 5.01 | price |
| SCI | 12M | 38.36 | 39.22 | 43.48 | 0.86 | 5.12 | price |
| GNFC | 12M | 17.51 | 19.98 | 24.15 | 2.47 | 6.64 | price |
| INDUSTOWER | 12M | 9.55 | 10.31 | 14.46 | 0.76 | 4.91 | price |
| OFSS | 9M | 39.48 | 42.13 | 46.21 | 2.65 | 6.73 | price |
| ADANIENT | 12M | 29.67 | 28.66 | 32.72 | 1.01 | 3.05 | price |
| OFSS | 12M | 35.2 | 35.90 | 39.80 | 0.7 | 4.6 | price |
| CANBK | 12M | 15.61 | 18.07 | 21.97 | 2.46 | 6.36 | price |
| QUESS | 9M | 51.08 | 59.87 | 63.61 | 8.79 | 12.53 | price |
| NMDC | 12M | 19.05 | 21.00 | 24.68 | 1.95 | 5.63 | price |
| INDUSTOWER | 1M | -7.36 | -7.36 | -3.88 | 0.00 | 3.48 | price |
| INDUSTOWER | 9M | -7.65 | -7.41 | -3.93 | 0.24 | 3.72 | price |
| ABSLAMC | 9M | 37.1 | 37.51 | 40.96 | 0.41 | 3.86 | price |
| NMDC | 9M | 11.87 | 11.19 | 14.57 | 0.68 | 2.70 | price |
| CANBK | 3M | 2.34 | 2.34 | 5.72 | 0.00 | 3.38 | price |
| M&MFIN | 12M | 46.84 | 44.97 | 48.23 | 1.87 | 1.39 | total |
| INDUSTOWER | 3M | -13.54 | -13.54 | -10.29 | 0.00 | 3.25 | price |
| REDINGTON | 3M | 47.49 | 47.49 | 50.73 | 0.00 | 3.24 | price |
| MRPL | 12M | 41.78 | 43.73 | 46.77 | 1.95 | 4.99 | price |
| REDINGTON | 12M | 33.92 | 36.04 | 39.04 | 2.12 | 5.12 | price |
| INDUSTOWER | 6M | -21.25 | -21.25 | -18.29 | 0.00 | 2.96 | price |
| QUESS | 12M | 24.43 | 26.35 | 29.30 | 1.92 | 4.87 | price |
| ABSLAMC | 12M | 13.12 | 17.74 | 20.69 | 4.62 | 7.57 | price |
| CANBK | 9M | -14.15 | -13.36 | -10.50 | 0.79 | 3.65 | price |
| CANBK | 6M | -13.6 | -13.60 | -10.75 | 0.0 | 2.85 | price |
| ABSLAMC | 6M | 13.07 | 13.07 | 15.91 | 0.00 | 2.84 | price |
| REDINGTON | 6M | 28.01 | 28.01 | 30.83 | 0.00 | 2.82 | price |
| INDIANB | 12M | 30.28 | 29.38 | 32.14 | 0.90 | 1.86 | price |
| M&MFIN | 3M | 20.82 | 20.82 | 23.54 | 0.00 | 2.72 | price |
| M&MFIN | 9M | 17.08 | 19.60 | 22.29 | 2.52 | 5.21 | price |
| REDINGTON | 9M | 11.01 | 12.36 | 14.83 | 1.35 | 3.82 | price |
| ABSLAMC | 3M | -2.28 | -2.28 | 0.18 | 0.00 | 2.46 | price |
| M&MFIN | 6M | 1.79 | 1.79 | 4.08 | 0.00 | 2.29 | price |
| INDIANB | 3M | 7.09 | 7.09 | 9.37 | 0.00 | 2.28 | price |
| ABSLAMC | 1M | -9.41 | -9.41 | -7.14 | 0.00 | 2.27 | price |
| INDIANB | 9M | -1.19 | -1.34 | 0.76 | 0.15 | 1.95 | price |
| MRPL | 9M | 1.92 | -0.53 | 1.57 | 2.45 | 0.35 | total |
| INDIANB | 6M | -5.65 | -5.65 | -3.64 | 0.00 | 2.01 | price |
| MRPL | 6M | -8.42 | -8.42 | -6.49 | 0.00 | 1.93 | price |
