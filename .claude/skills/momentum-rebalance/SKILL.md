---
name: momentum-rebalance
description: The complete momentum portfolio strategy spec — eligibility filters, Momentum Quality Score components, RSI bands, position/cluster caps, runner rules, stop sizing, and rebalance turnover discipline. Read before changing scoring.py, rebalance.py, or config.py, and when interpreting any scan CSV.
---

# Momentum Quality Framework (weekly rebalance, NSE cash equities)

## Input CSV schema (per stock)
symbol, name, series, date, close, marketcap (₹ cr), median_volume_one_year (₹ traded value),
absolute_return_{one_month,three_months,six_months,nine_months,one_year} (%),
sharpe_return_{same windows}, rsi_one_month, volatility_one_year (annualised decimal),
beta, circuits_{three_months,one_year}, positive_days_percent_{three,six}_months,
ma_20, ma_50, ma_100, ma_200, away_from_high_one_year (negative %), is_nifty_fno.
Verify units on load: volume fields are RUPEE value; volatility 0.36 = 36%.

## Hard eligibility filters (reject before scoring)
- close < ma_50 AND close < ma_200
- 3M return < 0 AND 6M return < 0
- circuits_three_months > 5  (also: circuits_one_year > 8 → −2 penalty if not rejected)
- median_volume_one_year < ₹5 crore
- away_from_high_one_year < −30
- series == "BE" (trade-to-trade)

## Score /100
Winsorise all return & Sharpe inputs at 1st/99th pct first.
- **A Trend 25**: +4 per MA cleared (20/50/100/200), +5 full alignment
  (close>20>50>100>200), +4 if 0–12% over 20-DMA, +2 if 12–20%, 0 beyond.
- **B Momentum 25**: percentile blend 1M 10% / 3M 30% / 6M 30% / 9M 15% / 1Y 15%;
  −4 if 1M>25% with 6M<40%; −3 if 1M>35% (parabolic).
- **C Risk-adjusted 20**: Sharpe percentile blend 3M 35% / 6M 35% / 9M 15% / 1Y 15%.
  Never rank on 1Y Sharpe alone; 1M Sharpe is noise — excluded.
- **D Consistency 10**: positive-day percentiles (3M .4, 6M .3 → 6 pts) + distance
  bonus (within 8% of 1-yr high +4; within 15% +2.5).
- **E Liquidity 10**: rupee-volume percentile ×6 + F&O +2 + mcap >₹20k cr +2 / >₹10k cr +1.
- **F Penalty ≥ −10**: RSI>82 −4 / >78 −2; ann vol >55% −3 / >45% −1.5; beta>1.6 −2;
  circuits_1y>8 −2; >25% over 20-DMA −3 / >18% −1.5.

## RSI bands
<40 weak unless confirmed recovery · 40–55 neutral · 55–70 healthy · 70–78 strong/extended
(no fresh full-size entries) · >78 wait or tranche · >82 high-risk, trim winners.

## Portfolio construction
- Positions: dynamic, typically 15–23 at ₹1cr scale. Max single 12.5%, min 4%
  (exceptions: deliberate half-size for <18-month listings; "runner" positions below).
- Cluster cap 25% (pharma/CDMO, EMS, auto-comp, cables/pipes, financials, chem, consumer…).
- **Runners**: held names that fail filters (e.g. circuit-prone) or are parabolic winners
  are capped at fixed rupee value, never added to, trailed with hard stops.
- Cash is an active position: strong-bullish 0–10%, bullish 5–15%, neutral 15–30%,
  weak 30–60%. Breadth proxy: % of universe above 20-DMA (≥65 bullish, 45–65 neutral).
- Liquidity check: position ≤ 0.5–1% of median daily traded value.

## Rebalance discipline
- Retain healthy holdings ranked within N+5 of cutoff; replace only if challenger beats
  incumbent score by 5–10+ pts or a hard filter fires. Never exit on one weak week alone.
- Target weekly turnover < 20–30% after initial build.
- Stops: vol-scaled = clamp(ann_vol/√52 × 2.2, 8%, 12%) below entry/ref; GTT same session.
- Costs: STT 0.1%/side, stamp 0.015% buy, STCG 20% (<12m) / LTCG 12.5% (>₹1.25L, >12m).
  Skip swaps whose edge < round-trip cost + tax.
- Pledged holdings must be unpledged (T+1) before sell orders will fill — surface this.
