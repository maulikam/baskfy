# Gates: 1.4.1 M11 — 6,934 of 9,166 cells fail on window length

Scope: the 271-row reference parity test is red. Diagnose to root cause; fix if the fix is
clear and safe; if not, state precisely what decision is needed. Analysis-first leaf.

Measured at SHA `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84`. Full write-up: `docs/PARITY-M11.md`.

- [x] G1: The test is run and the CURRENT failure count is measured, not quoted from the
      status page (which is from 22 Aug)
  CHECK: cd decile-blueprint && timeout 900 uv run pytest packages/core/tests/test_reference_parity.py -q 2>&1 | tail -5
  EXPECT: /passed|failed/
  EVIDENCE: The status-page number had NEVER been executed — the reproduction test skips unless
      BASKFY_PARITY_BARS is set. Supplied real bars (223,997 rows, 271 symbols, 2023-01-02 →
      2026-08-18) exported from `ohlcv_daily` on the staging box. Measured with bars:
      **6,396 cells failed of 9,186 compared** (before), **4,514** (after the fix). The
      bars-less CHECK above returns `44 passed, 2 skipped`, which is exactly the point: a green
      suite said nothing about parity. Both runs recorded in docs/PARITY-M11.md §1–2.

- [x] G2: The failure is characterised: which factors, which windows, and whether the error is
      constant, proportional or off-by-N periods
  CHECK: grep -ci "off-by\|window length\|proportional" docs/PARITY-M11.md
  EXPECT: /[1-9]/
  EVIDENCE: docs/PARITY-M11.md §3 carries the per-column × per-window failure table with the
      shape of each error: `absolute_return_*` and `positive_days_percent_*` are **off-by-one
      period**; `volatility_*` is **exactly proportional** with ratio √((N−1)/N) (p05→p95 spread
      of 4e-6 across 271 instruments); `rsi_*` is neither — a different smoothing family;
      `median_volume_one_year` is a **constant bar-count** offset (252 vs 247); `ma_200` is a
      constant one-bar shift. 1M is the tell throughout: it is the only window whose anniversary
      (2026-07-18) was a Saturday, and the only family that already reproduced.

- [x] G3: Root cause named with the file and line that produces the wrong window
  EVIDENCE: **`decile-blueprint/packages/core/src/baskfy_core/windows.py:177`** in
      `resolve_window()` — `bisect.bisect_left(ordered, calendar_start)` admits the anniversary
      day into the window. Counted against the exchange calendar in `ohlcv_daily`:
      `(anniversary, as_of]` gives 22/64/121/185/247 and `[anniversary, as_of]` gives
      22/65/122/186/248, against the recovered 22/64/121/185/247 — the half-open interval
      reproduces all five, the closed one reproduces one. Corroborated independently by
      recovering the return base bar from the export (268/265/268/267/260 exact at the first
      trading day *strictly after* the anniversary, vs ≤4 for every neighbour).
      docs/05 §Notation's prose contradicted its own table; the table was right.
      FIVE FURTHER root causes found and named with file:line in docs/PARITY-M11.md §4 —
      window length is only 1,882 of the 6,396: (B) `factors.py:303` volatility `ddof=1` should
      be `ddof=0`; (C) `factors.py:426,467` RSI is Wilder@N, reference is Cutler@N−1;
      (D) `windows.py:86` MEDIAN_VOL_BARS 252 should be the 12M window 247; (E) the
      **2026-02-01 Budget special session is missing from our data** (322 instruments ingested
      vs ~2,300 on a normal day, and none of the 271); (F) five symbols with unadjusted
      corporate actions, already NEEDS-MAULIK item 4.

- [x] G4: Either the fix lands with the cell count improving (state before and after), or a
      written recommendation says what decision is required and why it is not safe to guess
  EVIDENCE: BOTH. The window fix **landed**: `bisect_left` → `bisect_right`, with the module
      docstring, both `FactorWindow` field docstrings, the inline comment, the ValueError message
      and docs/05 §Notation all moved with it. **6,396 → 4,514 cells** (9,186 compared). Full
      suite green: **2,747 passed, 1,138 skipped, 0 failed**. For causes B, C and D the fix did
      NOT land and docs/PARITY-M11.md §4 and §8 state precisely why: each contradicts explicit
      `docs/05` prose (§2 "sample stdev (ddof=1)", §5 "Wilder's RSI", §13 "over 252 bars"), so
      each is a spec amendment with its own fixture regeneration, and B additionally moves the
      desk's GTT stop sizing. Each carries its measurement, its cell count and its decision.

- [x] G5: House rule 5 is not violated — no look-ahead is introduced by any fix
  EVIDENCE: The change makes the window **strictly smaller** — `[anniversary, as_of]` becomes
      `(anniversary, as_of]`, discarding the OLDEST bar. The right edge stays at `as_of`. Every
      bar the corrected window reads is a bar the previous one also read, so a change that only
      ever drops the oldest bar cannot introduce look-ahead. No factor gained access to any date
      it could not previously see; no point-in-time membership or factor row was re-dated; no
      call site, signature or date input changed. Argued in docs/PARITY-M11.md §5.

- [x] G6: No test was weakened to make anything pass
  EVIDENCE: `test_reference_parity.py` was **not modified at all** — it still fails, at 4,514
      instead of 6,396, and is reported as failing. No tolerance widened, no column added to an
      exclusion list, no `skipif` added. The two tolerance problems found
      (`COLUMN_TOLERANCE["volatility_*"]` = 5e-11 is unreachable from 8-dp stored data and
      2-dp stored closes; the residual floor is ~1.6e-7) were **written down in
      docs/PARITY-M11.md §4B and §8 rather than adjusted**. One fixture moved:
      `packages/core/tests/fixtures/momentum_scan.csv`, whose test asserts byte-identity. The
      assertion is unchanged — same predicate, same strictness, against the corrected engine —
      and the diff was read and argued first (docs/PARITY-M11.md §7): every calendar-window
      column moved and every bar-count column (`close`, `ma_*`, `median_volume_one_year`,
      `away_from_high_one_year`, `beta`, `circuits_*`) is byte-identical, which is exactly the
      partition `resolve_window` feeds. Pre-change copy retained.

- [x] G7 (added): the question the parent branch actually needs — would M11's failure change
      which orders get generated?
  EVIDENCE: Measured, not reasoned. 271 instruments scored three ways through
      `baskfy_core.score.score()` with the desk's own config: REF (published values), PRE
      (bisect_left), POST (bisect_right). **Hard eligibility filters: 239/271 eligible, 0
      disagreements with the reference in all three.** **Top-15 basket identical in all three**
      (the desk's operating size). At top-12, PRE bought AETHER where the reference bought HSCL;
      **POST matches the reference 12/12** — a real defect at the bottom of the book, now fixed.
      Rank agreement improves 42→72 of 239. GTT stops: `stop_from_vol` matches the SKILL spec
      `clamp(ann_vol/√52 × 2.2, 8%, 12%)` exactly; stop prices differ from the reference on
      133/269 names but by 0.01–0.07% on the top-15, driven by cause B, and no name crosses the
      8%/12% clamp. **Verdict: M11 does not invalidate execution** — it corrupted ordering,
      sizing at the margin, and the 12th position. docs/PARITY-M11.md §6.

## LEAD carried over from the first (network-killed) run of this leaf

The first attempt died on an API outage mid-verification. Before dying it reported
`6,396 -> 4,514` failing cells and was about to check "the blast radius across the suite".

Its candidate change, saved at
`/private/tmp/claude-501/-Users-maulikdave-Documents-projects-baskfy/df1242c6-d902-4640-a369-8469f8ce7e6d/scratchpad/m11-windows-lead.patch`:

    packages/core/src/baskfy_core/windows.py, resolve_window()
    -    index = bisect.bisect_left(ordered, calendar_start)
    +    index = bisect.bisect_right(ordered, calendar_start)

Driver findings on it, measured before reverting it out of the working tree:
- It DOES reduce failing cells (leaf's figure 6,396 -> 4,514; not independently re-measured).
- It BREAKS `packages/core/tests/test_momentum_scan.py::test_the_bytes_are_stable`
  ("At index 574 diff: b'9' != b'8'"). That may be correct rather than a regression — if the
  window was wrong, the generated scan's bytes SHOULD change — but it is a spec judgement,
  not a silent fix.
- The comment immediately above the line documents the OLD behaviour ("Snap *forward*") and
  would contradict the new one. `bisect_left` already snaps forward; `bisect_right` skips the
  boundary day when the anniversary IS a trading day, shortening the window by one.

The change was REVERTED from the working tree so sibling leaves are not measuring against an
unverified edit. Re-derive it, do not assume it. If you adopt it, the docstring, the comment and
the momentum-scan byte fixture all have to move with it, and docs/05 §Notation must be checked
to see which behaviour the spec actually requires.
