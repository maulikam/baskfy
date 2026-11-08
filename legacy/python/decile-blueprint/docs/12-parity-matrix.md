# 12 — Parity matrix

Track completion here. `Module` = the prompt number in `PROMPTS.md` that delivers it.

## Data

| Capability | Reference has it | We build it | Module | Done |
|---|---|---|---|---|
| Daily OHLCV, full NSE universe | ✅ | ✅ | 3–4 | ☐ |
| Adjusted price series | ✳️ Dec 2026 | ✅ from day 1 | 3 | ☐ |
| History from 2011 | ✳️ Dec 2026 | ✅ | 3 | ☐ |
| Corporate actions | ✅ (display) | ✅ (display + adjustment) | 3 | ☐ |
| Point-in-time index membership | ⚠️ undocumented | ✅ enforced | 4 | ☐ |
| Index PE/PB/DivYield snapshots | ✅ | ✅ | 4 | ☐ |
| NSE listings history | ✅ | ✅ | 11 | ☐ |
| Circuit bands per day | ✅ (derived) | ✅ (from bhavcopy) | 2–3 | ☐ |

## Factors

| Capability | Ref | Ours | Module | Done |
|---|---|---|---|---|
| Absolute return ×5 windows | ✅ | ✅ | 5 | ☐ |
| Sharpe return ×5 (`ret/vol`) | ✅ | ✅ verified | 5 | ☐ |
| RSI ×5 windows | ✅ | ✅ | 5 | ☐ |
| Volatility ×5 windows | ✅ | ✅ | 5 | ☐ |
| 11 blend shapes × 3 families | ✅ | ✅ | 5 | ☐ |
| Beta (1y) | ✅ | ✅ | 5 | ☐ |
| Beta-scaled factors ×5 | ✅ | ✅ | 5 | ☐ |
| 12−1 / 12−2 momentum | ✅ | ✅ | 5 | ☐ |
| MAs 20/50/100/200 | ✅ | ✅ | 5 | ☐ |
| Away from 1Y/ATH high | ✅ | ✅ verified | 5 | ☐ |
| Positive-days % ×5 | ✅ | ✅ | 5 | ☐ |
| Circuit counts ×5 | ✅ | ✅ | 5 | ☐ |
| Median ₹ volume 1Y | ✅ | ✅ | 5 | ☐ |
| Volume averages (day/week/1–12m) | ✅ | ✅ | 5 | ☐ |
| Wasserstein regime | ✅ | ✅ + explainable | 5 | ☐ |

## Screener

| Capability | Ref | Ours | Module | Done |
|---|---|---|---|---|
| 14 index universes | ✅ | ✅ | 6 | ☐ |
| 62 sort factors + direction | ✅ | ✅ | 6 | ☐ |
| Apply-filters-on (deciles/top-N) | ✅ | ✅ | 6 | ☐ |
| Min 1Y return, median volume | ✅ | ✅ | 6 | ☐ |
| MA above/below ×8 | ✅ | ✅ | 6 | ☐ |
| Away-from-high ×2 | ✅ | ✅ | 6 | ☐ |
| Positive days ×5 | ✅ | ✅ | 6 | ☐ |
| Circuits ×5 | ✅ | ✅ | 6 | ☐ |
| Marketcap range | ✅ | ✅ | 6 | ☐ |
| P/E range (+ NULL exclusion) | ✅ | ✅ | 6 | ☐ |
| Series EQ/BE | ✅ | ✅ | 6 | ☐ |
| Ignore top beta / volatility | ✅ | ✅ | 6 | ☐ |
| Ignore above beta | ✅ | ✅ | 6 | ☐ |
| Price (CMP) range | ✅ | ✅ | 6 | ☐ |
| 3-factor combined rank | ✅ | ✅ | 6 | ☐ |
| Custom filters ×3 (field vs field) | ✅ | ✅ | 6 | ☐ |
| Historical date re-run | ✅ (from Nov 2024) | ✅ (from backfill start) | 6 | ☐ |
| Screens CRUD + examples | ✅ | ✅ | 9 | ☐ |
| Column picker (34) | ✅ | ✅ | 9 | ☐ |
| CSV export | ✅ | ✅ + Parquet | 9 | ☐ |

## Product surfaces

| Capability | Ref | Ours | Module | Done |
|---|---|---|---|---|
| Indices dashboard | ✅ | ✅ + sparklines | 11 | ☐ |
| Market health (4 gauges) | ✅ | ✅ + history charts | 11 | ☐ |
| Instrument factsheet | ✅ | ✅ + percentile bars | 10 | ☐ |
| PROS/CONS rules | ✅ | ✅ | 10 | ☐ |
| Listings | ✅ | ✅ | 11 | ☐ |
| Rebalance tracker (CSV) | ✅ beta | ✅ + saved portfolios | 14 | ☐ |
| Backtest engine | ✳️ Dec 2026 | ✅ | 15 | ☐ |
| Auth + profile | ✅ | ✅ | 12 | ☐ |
| Plans, checkout, invoices | ✅ | ✅ | 13 | ☐ |
| FAQ / blog / legal / pricing | ✅ | ✅ | 18 | ☐ |
| Public API + keys | ❌ | ✅ | 20 | ☐ |
| Screen alerts / email digests | ❌ | ✅ | 20 | ☐ |

Legend: ✅ shipped · ✳️ announced · ⚠️ present but undocumented · ❌ absent
