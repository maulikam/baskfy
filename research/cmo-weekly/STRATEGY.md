# The weekly CMO scan — studied, not adopted

*Research note, 11 Sep 2026. Same data and simulator as `../volume-breakout/`; the fourth Chartink
scan of the series. Nothing here has traded real money — and, on this evidence, should not.*

## 0. The one-paragraph answer

The scan fires once a week, at Friday's close, on a stock whose **weekly Chande Momentum
Oscillator (10) has just crossed above 21** while its **monthly CMO(10) is above 21**, on a
week whose volume is about **three times a normal week** (`Weekly Volume / 15 > Yearly Volume / 252`),
in names above **₹1,000 crore market cap** — roughly five names a week. It is a momentum-turn
signal with a volume confirmation, and on nine years of Baskfy bars it does not carry an edge
worth a sleeve. The raw entry is the weakest of the four scans (+0.2 % after 20 sessions in the
first half of the history, +2.1 % in the second; +5 % at 60 sessions, against +7 % for the
tight-close scan), it lost 11 % per entry through 2018, and **in the names that most surely pass
the market-cap rule (₹20 crore+ daily turnover) the edge is zero** (1–4 % CAGR in every exit
variant). Across 60 rule combinations the best cell — not-extended entries, a 40 % breadth gate, a
20 % stop, sell when the weekly CMO drops back below 21 — does **13.0 % CAGR at −28.5 %**, 36 %
invested, on 256 trades with ten of them 44 % of the profit; NIFTY Midcap 150 did 14.3 % over the
same window. One cell out of sixty beating nothing is noise, and it is reported as such. The
recommendation is to **leave this scan as a watchlist** and put the effort into TWT-1 and VBT-1.

## 1. The scan, and how faithfully it was reproduced

| # | Rule | Reading used here |
|---|---|---|
| 1 | Monthly CMO(10) > 21 | ten completed monthly closes plus the month in progress, read at the week's last session |
| 2 | Weekly CMO(10) crossed above 21 | complete ISO weeks; last week ≤ 21, this week > 21 |
| 3 | Weekly Volume / 15 > Yearly Volume / 252 | this week's volume sum vs the last 252 sessions' |
| 4 | Market Cap > 1,000 crore | **proxied**: the export carries no shares outstanding; 20-day average turnover ≥ ₹5 crore is the threshold that best separates Chartink's in/out names (its Smallcap rows have a median turnover of ₹16 cr and a first quartile of ₹8.5 cr; the names it excludes a median of ₹3.6 cr) |

CMO(n) = 100 × (Σ up-moves − Σ down-moves) / (Σ up + Σ down) over the last *n* closes.

**Verification against the Chartink backtest CSV (weekly rows, Aug 2023 → Sep 2026, 767
stock-weeks): recall 71 %, precision 46 % without the cap proxy, 67 % / 64 % with it.** The row
dated Monday 7 Sep 2026 lists exactly the ten names in Thursday's live scan, so a row's date is
the Monday *of* the signal week and the candle is that week's — readable at Friday's close,
tradable Monday. The misses are split between the volume rule (90) and the monthly rule (74),
where Chartink's candle semantics on a weekly scan are not documented; reading the monthly candle
with look-ahead (the full current month) recovers a few (74 %). Weakest reproduction of the four,
and the market-cap proxy is a real approximation: a ₹1,000-crore company trading ₹3 crore a day is
in Chartink's list and not in this one.

## 2. Event study

Buy Monday's open, mark *k* sessions later (`out/entry_slices.csv`; IS to 2022-12, OOS 2023 →):

| slice | IS n | IS +20 | IS +60 | OOS n | OOS +20 | OOS +60 |
|---|---|---|---|---|---|---|
| all signals (cap proxy) | 620 | +0.17 | +5.03 | 899 | +2.11 | +5.34 |
| 20-day return < 15 % at entry | 189 | +2.45 | +6.14 | 322 | +2.87 | +6.21 |
| 20-day return ≥ 30 % | 161 | −1.57 | +2.11 | 178 | +3.10 | +7.35 |
| turnover ≥ ₹20 cr | 283 | −0.47 | +3.01 | 537 | +1.75 | +4.53 |
| weekly CMO 21–40 at cross | 317 | +0.78 | +5.60 | 488 | +2.19 | +5.13 |
| breadth > 40 % | 521 | +0.13 | +5.05 | 787 | +2.26 | +5.67 |

By year, the 60-session mean: −11.2 (2018), −0.3, +10.0, +10.9, −2.0, +11.8, +2.9, −4.2, +7.7 —
beta, with a thin positive drift in the good years and nothing that survives the bad ones.

## 3. What was tried (`out/grid_cw.csv`, `out/study_cw.log`)

Entries: Monday open, and a limit at Friday's close. Exits: the weekly CMO falling back below 21
(the scan's own state), 20- and 40-session holds, 21-EMA, 50-SMA, 15 % and 20 % trails. Stops
10 / 15 / 20 / 30 %. Gates: none, breadth > 35 %, > 40 %. Filters: none, not-extended
(20-day return < 15 %), large names (turnover ≥ ₹20 cr). Ten slots, five new a week, 25 bps.

| | CAGR | max DD | trades | note |
|---|---|---|---|---|
| Monday open · 50-SMA exit · 20 % stop · no gate | 11.5 % | −44 % | 423 | index-like |
| same · CMO-state exit · breadth > 40 % | 14.6 % | −35 % | 340 | IS 5.9 % / OOS 26.7 % |
| **not-extended · CMO-state exit · 20 % stop · breadth > 40 %** (best cell) | **13.0 %** | **−28.5 %** | 256 | IS 12.6 / OOS 12.8, 36 % invested, Sharpe 0.93 |
| large names only (turnover ≥ ₹20 cr), any exit | 1–4 % | −33 to −48 % | | the names that surely pass the cap rule have no edge |
| 20 % trail, any gate | 0–6 % | −38 to −44 % | | the trail that made TWT-1 work does nothing here |
| NIFTY Midcap 150, same window | 14.3 % | −44 % | | |

The best cell's year table: 2018 −7.5, 2019 0 (no trades), 2020 +11, 2021 +66, 2022 +7, 2023 +59,
2024 +9, 2025 −14, 2026 +8 — two years carry it. Its trades and equity are in `out/bestcell_*`
for the record.

## 4. Why it is not adopted

Three independent reasons, any one of which would be enough. The raw entry's expectancy is the
smallest of the four scans and negative through the one bear market in the sample. The edge that
does exist sits in the names the scan's own market-cap rule is designed to exclude, which means
the live list (with a true cap filter) will be worse than this backtest, not better. And the
reproduction is 67 % with an approximated filter — the study is testing a cousin of the scan.
A 13 % cell picked from sixty, with a Sharpe under 1 and the index a point ahead over the same
window, is not a strategy; it is the kind of number a weekly momentum-turn signal produces in a
decade dominated by 2021 and 2023.

If the scan is kept, keep it as a **Monday watchlist**: five names whose weekly momentum just
turned on volume, to be judged by hand against the other three sleeves' entry rules — a TWT-1
tight base or a VBT-1 breakout in one of these names is a better-founded trade than the CMO cross
alone.

## 5. Files

| file | what |
|---|---|
| `cscan.py`, `cscan_verify.py` | weekly/monthly CMO, the volume rule, the week-end signal; reproduction check and the market-cap proxy search |
| `study_cw.py` | event study (`out/entry_slices.csv`) and the 60-cell grid (`out/grid_cw.csv`, `out/study_cw.log`) |
| `final_cw.py` | the best cell run once for the record (`out/bestcell_*`) |
| `chartink_backtest.csv`, `chartink_today.csv` | Chartink's weekly backtest export and 11 Sep's ten names |

Run order: `python cscan_verify.py`, `python study_cw.py`, `python final_cw.py` (needs
`../volume-breakout/data/panel.pkl`; `vbt/` is a symlink to `../volume-breakout/vbt`).
