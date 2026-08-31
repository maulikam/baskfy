# Gates: 1.4.2 M13 — generated scan passes 223 symbols, upload passes 239

Scope: the CSV cord cannot be cut while the generated scan disagrees with the upload by 16
symbols. Identify all 16 and explain each.

**Outcome: the 16-symbol gap no longer exists.** Measured 31 Aug 2026 at HEAD
`7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84` with leaf 1.4.1's uncommitted `windows.py` fix in the
tree: upload 239, generated **239**, symmetric difference **0**. The historical gap resolved
16 → 2 → 0 (M24/M27/M28 backfills, then 1.4.1's window-anniversary fix). Full record with the
per-symbol enumeration, causes, directions and verdict: `docs/PARITY-M13.md`.

- [x] G1: The current gap is measured, not quoted — both counts stated from a real run
  EVIDENCE: Harness built on `reconciliation/desk_parity.py` (real `momentum_scan.build`, desk's
  unmodified `app/config.py`, live screener Postgres on :5433 — 3,534,860 bars, 289 corporate
  actions), oracle `data/uploads/scan_1787143663_Investing_001__1_.csv` (271 rows, as-of
  2026-08-18, opened read-only). Output, reproduced identically on three separate runs:
  `upload passes apply_filters: 239` / `generated passes apply_filters: 239` /
  `net gap: 0` / `symmetric difference: 0 (upload-only 0, generated-only 0)` /
  `symbols in oracle but absent from generated frame: 0`. `screen_run_id: 0db6fe59674293e7`.
  Both counts are from the run; neither is quoted. Recorded in `docs/PARITY-M13.md` §1, with the
  22 Aug baseline (223/239) shown alongside for comparison rather than restated as current.

- [x] G2: Every differing symbol is enumerated by name. Not a sample: the full list, with its
      length stated and equal to the measured gap
  CHECK: grep -c "^| [A-Z0-9]" docs/PARITY-M13.md
  EXPECT: /[1-9][0-9]*/
  EVIDENCE: 13 | 13. The measured gap is 0, so the gap table has 0 rows — §2a states the
  arithmetic explicitly (`239 − 239 = 0`; `|upload △ generated| = 0`; rows = 0 = the gap) so the
  equality can be checked rather than asserted. Because an empty table is a claim and not a
  finding, §2b enumerates instead **every** symbol in the 271-row corpus differing in eligibility,
  reject reason, or contaminated inputs: ASTRAL, HINDCOPPER, DIACABS, INOXGREEN, ARVIND, CANBK,
  NMDC, PRIVISCL — 8 rows, complete, no symbol outside it differs in any of those three ways. The
  CHECK counts 13 `| SYMBOL` rows: those 8 plus the 5 window-anniversary rows in §3. Note the
  literal CHECK cannot be satisfied by a zero gap (`EXPECT /[1-9][0-9]*/` demands a positive
  count); the criterion is scoped to its Goal per the Autonomy charter's precedence rule 5, and
  the Goal — no differing symbol left unnamed — is met exhaustively.

- [x] G3: Each symbol has a cause: missing bars, missing corporate action, filter difference,
      marketcap gap, or unexplained. No symbol left uncategorised.
  CHECK: grep -ci "unexplained" docs/PARITY-M13.md
  EXPECT: /[0-9]/
  EVIDENCE: 3 | 3. **Unexplained count: 0.** All 8 enumerated symbols carry a cause from the fixed
  vocabulary — ASTRAL and HINDCOPPER `filter difference` (window anchor off by one bar, traced to
  the paisa against the bar series in §3); DIACABS `missing bars` (11 of 121) + `missing corporate
  action`; INOXGREEN `missing bars` (10 of 121); ARVIND / CANBK / NMDC `missing corporate action`
  with the step dated (2018-11-28 / 2017-10-25 / 2022-10-27) and confirmed outside every factor
  window; PRIVISCL a back-adjustment rounding artefact, also outside every window. Zero is a
  measured result: each `suspect` name was chased to a dated price step and to the presence or
  absence of its `corporate_action` row, not left in a residual bucket. Direction is given per
  symbol; both historical gap entries were upload-only (coverage we lacked) and there has never
  been a generated-only symbol.

- [x] G4: A verdict states whether the generated scan is safe to make the default, with the
      residual risk named
  EVIDENCE: `docs/PARITY-M13.md` §7 — **"Not yet, but the reason has changed."** Parity itself is
  answered (239/239 eligible, top-25 25/25 vs 23/25 at M12, top-15 15/15, max |ΔSCORE| 4.30 pts
  against a `REPLACEMENT_EDGE` of 8.0 — so no swap decision can differ). The blocker named is
  different: `app/main.py::_carried_columns()` sources marketcap/beta/circuits/F&O/series from the
  most recent uploaded CSV and `momentum_scan.build` inner-joins on `symbol`, so the generated
  scan's universe is *defined by the last upload* — 271 rows out of 2,540 instruments with bars.
  **The generated path does not cut the CSV cord; it requires one.** Six residual risks named:
  (1) the cord is not cut — blocking; (2) stale carried columns, silent; (3) margin fragility —
  GOKULAGRO sits 0.00 pts from the 3M line, BELRISE ₹0.46 below its MA, so the zero gap is partly
  luck at the margin; (4) one corpus, one date; (5) undiagnosed 9M/12M and RSI drift (9M/12M
  returns match only 3 and 5 of 271, median 1.4 pts) plus a phantom 2026-02-01 session carrying
  21 instruments that inflates every window crossing it; (6) the headline 0 depends on 1.4.1's
  uncommitted work. Recommendation to the driver: do not flip, and correct the stale 223/239 on
  `docs/00-merge-status.md`.

- [x] G5: The flag is NOT flipped by this leaf — that is the driver's call after 1.4.3
  CHECK: git diff --name-only | grep -c "\.env" || echo 0
  EXPECT: 0
  EVIDENCE: 0 | 0. No `.env` file touched and no flag flipped. `SCAN_SOURCE_DEFAULT` read as
  `'upload'` during the run and was not written. This leaf wrote exactly two files —
  `docs/PARITY-M13.md` and this gates file; all measurement code was scratch, outside the repo.
  The pre-1.4.1 counterfactual in §4 was produced by patching `resolve_window` in-process, so no
  source file was edited to obtain it. The read-only regression corpus was opened read-only, and
  no order was placed.
