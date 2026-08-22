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
