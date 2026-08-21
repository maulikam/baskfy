Reconciliation report — our formulas against the reference product's published output
============================================================================================

check                          docs                         cells   bad  status
--------------------------------------------------------------------------------------------
sharpe_identity                docs/05 §3                    1355     0  clean
away_from_high                 docs/05 §10                    542     0  clean
positive_days_denominator      docs/13 §3, docs/05 §11       1355     0  clean
blend_ordering                 docs/05 §4, docs/13 §2 finding 3     271     0  clean
circuit_nesting                docs/05 §12 (INFERRED)        2439     0  clean (see UNRESOLVED)
turnover_is_exchange_value     docs/05 §13, docs/13 §2 finding 5     271     0  clean
price_level_ordering           docs/05 §9, §10               1626     0  clean
value_ranges                   docs/05 §2, §5; docs/13 §2 finding 4    2710     0  clean
universe_containment           docs/01 §2                    2710     0  clean
top_risk_flag_monotonicity     docs/06 §Step 4               2870     0  clean
skip_month_momentum            docs/05 §8 (INFERRED)            0     0  clean (see UNRESOLVED)
circuit_detection_rule         docs/05 §12 (INFERRED)         271     0  clean (see UNRESOLVED)

0 disagreement(s) over tolerance.

UNRESOLVED — investigated, documented, not settled by this data
============================================================================================

--- circuit_nesting (docs/05 §12 (INFERRED)) ---
    docs/05 §12's detection rule cannot be validated from this snapshot. Validating it needs per-bar high/low/close and the NSE band for each day; the export carries a single day of counts. Nesting and bounds are all the published data can decide.
    56 of 271 rows report at least one circuit-hit day in the last year, which is the only distributional evidence available for calibrating the band heuristic.

--- skip_month_momentum (docs/05 §8 (INFERRED)) ---
    docs/05 §8 leaves two candidate definitions open and asks Prompt 19 to settle it empirically. IT CANNOT BE SETTLED FROM THIS BUNDLE. Reasons and arithmetic:
      (1) The 93-column export carries no skip-month column at all — the reference product offers 12-1 and 12-2 as sort keys (docs/01 §3) and never exports the values. So the 271 rows contribute nothing to this question.
      (2) The only published values anywhere in docs/ are CUPID's 608.37 and 852.21, from 19 Aug 2026 (docs/05 §8).
      (3) Candidate A — docs/05's primary, P_(t-21)/P_(t-252) — is fully determined by CUPID's other published figures on that day: (1 + 753.00%) / (1 + 37.29%) - 1 = 521.31%, against a published 608.37%. The gap is 87.06 percentage points, or 16.7% of the value. The 21-vs-22 and 247-vs-252 bar mismatches between §8's bar offsets and §1's calendar windows are worth about six bars of a name compounding at ~0.86% a day, i.e. ~5% — a quarter of the gap. Candidate A is therefore INCONSISTENT with the published figure, by a margin the offset mismatch does not explain.
      (4) Candidate B — P_(t-21)/P_(t-273) — is under-determined: it implies a 13-month return of 873% for CUPID, which nothing in the bundle contradicts and nothing confirms. It is NOT CONFIRMED.
      (5) The 12-2 figure (852.21%) exceeds the 12-month return (753.00%), which under either candidate requires CUPID's price to have fallen materially between t-42 and t-21 while rising 37% over the last month. That is possible and is not evidence either way.
      DECISION: the engine keeps shipping candidate A (SkipMonthDefinition.SKIP_END), which is the definition docs/05 §8 instructs us to implement, and the switch to candidate B remains a constructor argument (baskfy_core.momentum.SkipMonthConfig). Resolving it needs CUPID's real adjusted closes for ~294 trading days, which arrives with the first production backfill and not before.

--- circuit_detection_rule (docs/05 §12 (INFERRED)) ---
    docs/05 §12's rule has two branches (published NSE bands; a |r_t| band heuristic with a 0.25% tolerance) and the snapshot can distinguish neither. Distinguishing them needs, per bar: high, low, close_raw, the previous close and the day's NSE band. The export has one day of counts.
    What the counts do say: 56 of 271 rows recorded at least one hit in the last year; the largest is 16 days out of 247. A detection rule that fired on, say, every 20% up-day would produce far more than that on a 271-row momentum screen, so the reference product's rule is at least as strict as ours. That is a bound, not a validation.
    DECISION: the engine keeps docs/05 §12's rule as written, with the published-band branch preferred and the method recorded per row (baskfy_core.circuits.CircuitMethod). Recalibration needs bhavcopy history.

NOTES
============================================================================================

    blend_ordering: 48 inversions, worst 0.0075, all within 0.010 (docs/13 §2 reports 48 inversions, all <= 0.0075)
    turnover_is_exchange_value: mean 0.999905, sd 0.008363 over 271 rows
    top_risk_flag_monotonicity: nifty_50/beta: 2/18 visible rows flagged (11.1%)
    top_risk_flag_monotonicity: nifty_50/volatility: 2/18 visible rows flagged (11.1%)
    top_risk_flag_monotonicity: nifty_next_50/beta: 2/20 visible rows flagged (10.0%)
    top_risk_flag_monotonicity: nifty_next_50/volatility: 2/20 visible rows flagged (10.0%)
    top_risk_flag_monotonicity: nifty_100/beta: 4/38 visible rows flagged (10.5%)
    top_risk_flag_monotonicity: nifty_100/volatility: 4/38 visible rows flagged (10.5%)
    top_risk_flag_monotonicity: nifty_200/beta: 9/79 visible rows flagged (11.4%)
    top_risk_flag_monotonicity: nifty_200/volatility: 9/79 visible rows flagged (11.4%)
    top_risk_flag_monotonicity: nifty_500/beta: 15/184 visible rows flagged (8.2%)
    top_risk_flag_monotonicity: nifty_500/volatility: 23/184 visible rows flagged (12.5%)
    top_risk_flag_monotonicity: nifty_total_market/beta: 28/271 visible rows flagged (10.3%)
    top_risk_flag_monotonicity: nifty_total_market/volatility: 40/271 visible rows flagged (14.8%)
    top_risk_flag_monotonicity: nifty_large_mid_250/beta: 10/92 visible rows flagged (10.9%)
    top_risk_flag_monotonicity: nifty_large_mid_250/volatility: 11/92 visible rows flagged (12.0%)
    top_risk_flag_monotonicity: nifty_midcap_150/beta: 7/54 visible rows flagged (13.0%)
    top_risk_flag_monotonicity: nifty_midcap_150/volatility: 6/54 visible rows flagged (11.1%)
    top_risk_flag_monotonicity: nifty_smallcap_250/beta: 5/92 visible rows flagged (5.4%)
    top_risk_flag_monotonicity: nifty_smallcap_250/volatility: 16/92 visible rows flagged (17.4%)
    top_risk_flag_monotonicity: nifty_microcap_250/beta: 10/87 visible rows flagged (11.5%)
    top_risk_flag_monotonicity: nifty_microcap_250/volatility: 12/87 visible rows flagged (13.8%)
    top_risk_flag_monotonicity: nifty_mid_small_400/beta: 12/146 visible rows flagged (8.2%)
    top_risk_flag_monotonicity: nifty_mid_small_400/volatility: 20/146 visible rows flagged (13.7%)
    top_risk_flag_monotonicity: nifty_allcap/beta: 25/271 visible rows flagged (9.2%)
    top_risk_flag_monotonicity: nifty_allcap/volatility: 7/271 visible rows flagged (2.6%)
    top_risk_flag_monotonicity: nifty_fno/beta: 7/83 visible rows flagged (8.4%)
    top_risk_flag_monotonicity: nifty_fno/volatility: 7/83 visible rows flagged (8.4%)

