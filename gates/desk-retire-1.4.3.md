# Gates: 1.4.3 M14 — shadow harness showed 4 order deltas

Scope: the shadow harness compared Baskfy's orders against the desk's and found 4 differences,
one a corporate-action substitution. Re-run it and account for every delta.

Deliverable: `docs/PARITY-M14.md`. Measured at commit `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84`
(Run A, pinned via `git archive` + `PYTHONPATH`) and at the working tree carrying leaf 1.4.1's
uncommitted `windows.py` fix, sha256
`8803fd6f4cf712f8406f214048daffda020be56030e77b96b2862464752c6ac4` (Run B).

- [x] G1: The shadow harness is re-run today and its CURRENT delta count measured
  CHECK: grep -c "ORDER DELTAS" docs/PARITY-M14.md
  EXPECT: /[1-9]/
  EVIDENCE: `scripts/shadow_mode.py --date 2026-08-18` re-run twice on 31 Aug 2026 against
  `baskfy-postgres` (3,534,860 bars to 2026-08-21; `corporate_action` 289 rows).
  **Run A, pinned to HEAD `7d6b7fb`: 2 ORDER DELTAS** (SHILPAMED 81 to 80, WELCORP 35 to 36).
  **Run B, working tree with 1.4.1's `bisect_right` fix: 3 ORDER DELTAS** (SKYGOLD 84 to 83,
  SYRMA 45 to 46, WELCORP 35 to 36). Both console transcripts are quoted verbatim in
  `docs/PARITY-M14.md` section 2, alongside the 22 Aug figure of 4, which is explicitly marked as
  history. Breadth now 68.6/68.6 (was 68.6/68.3); suspect symbols 5 (was 41); 15 orders both
  sides, both runs.

- [x] G2: Every delta is enumerated with symbol, side, quantity on each side, and cause
  CHECK: grep -c "^| " docs/PARITY-M14.md
  EXPECT: /[1-9]/
  EVIDENCE: 56 table rows. `docs/PARITY-M14.md` section 3 carries one table per run — 2 rows for
  Run A, 3 for Run B — each with symbol, side, quantity on the Baskfy side, quantity on the desk
  side, the exact pre-rounding target on both sides, and the cause. Counts are stated and match
  the harness output. Causes are attributed to score components measured on both sides: WELCORP is
  `F_penalty` +2.00 (RSI 79.86 vs 73.61 across the hard 78 cliff); SHILPAMED (Run A), SKYGOLD and
  SYRMA are sub-threshold percentile drift landing on opposite sides of `round()`. All fifteen
  reference prices are identical to the paisa on both sides, so no delta is a price difference.
  Each delta was also checked against the `momentum-rebalance` skill's rebalance discipline
  (retention band, `REPLACEMENT_EDGE`, turnover, runners, caps, liquidity) — a second table records
  that none of those rules explains any delta, and that the empty book bypasses every one of them.

- [x] G3: The corporate-action substitution is traced to the action that caused it
  CHECK: grep -c "2025-10-03" docs/PARITY-M14.md
  EXPECT: /[1-9]/
  EVIDENCE: `docs/PARITY-M14.md` section 4. The action is SHILPAMED, `split`, ex-date
  **2025-10-03**, ratio **2:1**, source `ratio_recovery`, `confirmed: true` — recovered by M24 and
  applied by M28. Desk: export already adjusted, `away_from_high_one_year` -1.89, bought 81.
  Baskfy on 22 Aug: the raw print stepped 778.75 to 384.95, `away_from_high` went past
  `MAX_AWAY_FROM_HIGH = -30`, `far_from_high` rejected the symbol outright, and DIVISLAB took the
  slot at 7 shares. Baskfy today: bars verified from the database — 2025-10-01 close 389.3750 /
  close_raw 778.7500, 2025-10-03 384.9500 / 384.9500 — adjusted series continuous, raw print
  preserved (house rule 6). Largest trailing-year move is now a genuine -15.68% on 2025-09-09; no
  residual jump. `away_from_high_one_year` is -1.890 on both sides. **This delta no longer
  exists.** Five symbols still carry an unadjusted action (ARVIND, CANBK, DIACABS, NMDC,
  PRIVISCL); none reaches this Friday's basket.

- [x] G4: A verdict states whether Baskfy's order generation is trustworthy enough to execute
      from, with the residual risk named. "No" is an acceptable answer if argued.
  CHECK: grep -c "^## 6. Verdict" docs/PARITY-M14.md
  EXPECT: /1/
  EVIDENCE: `docs/PARITY-M14.md` section 6 — **"No. Not yet — and the reason is not the count."**
  Four residual risks named concretely: (1) `rsi_*` reproduces 0/268, definition unknown, and its
  ~3-6 point systematic gap sits directly under the 78 penalty cliff and the 82 `PARABOLIC_RSI`
  trim, so on some Friday the two systems will disagree about trimming a whole position, not a
  share; (2) `volatility_*` has the same shape, came within 0.0004 of the 0.45 cliff in Run A, and
  also sets the GTT stop under non-negotiable #4; (3) the harness has never generated a sell —
  `BOOK = []` bypasses every retention and exit rule; (4) the measured answer moved 2 to 3 inside
  one afternoon on a one-character core edit, so each Friday must be stamped with its commit. Four
  concrete conditions for changing the answer are listed. Section 5 states what the harness cannot
  prove.

- [x] G5: The shadow flag stays off; no live comparison placed an order
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_breadth_and_shadow.py -k "flag_is_off or never_touches_a_broker" -q -p no:randomly
  EXPECT: /passed/
  EVIDENCE: `SCAN_SOURCE_DEFAULT` read `upload` at run time (the default in `app/config.py`, and
  the value in `.env.example`); it was never flipped. `DRY_RUN=true` on every invocation and
  `app.config.DRY_RUN` read `True`. The harness has no path to a broker: it imports
  `app.scan_source`, `app.rebalance`, `app.scoring` and `baskfy_core.momentum_scan` only, and
  `tests/test_breadth_and_shadow.py::test_the_harness_never_touches_a_broker` asserts the module
  source contains none of `place_order`, `OrderGateway`, `gateway(`, `kite(`, `core.gateway`.
  Running that test plus `flag_is_off`, `diff_is_at_order_level` and `small_enough_to_ignore` gave
  **6 passed, 11 deselected in 0.04s**. Both plans were built and neither was sent; no order, live
  or simulated, was placed. Runs were logged to a scratch path via `--log`, so
  `data/shadow-mode.jsonl` still holds only the two 22 Aug lines and the four-Friday counter is
  untouched by this re-measurement.
