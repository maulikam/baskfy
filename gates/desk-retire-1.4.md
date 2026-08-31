# Gates: BRANCH 1.4 Numbers parity — integration

- [x] B1: All three children verified by the driver re-running their checks
  CHECK: for f in gates/desk-retire-1.4.1.md gates/desk-retire-1.4.2.md gates/desk-retire-1.4.3.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 0
  EVIDENCE: driver-verified 2026-08-31. 1.4.1 7/7, 1.4.2 5/5, 1.4.3 5/5 — 0 unchecked, 0 ABANDON.
    Driver independently confirmed: the parity test's two full-row cases were SKIPPING on an
    unset BASKFY_PARITY_BARS (so the 22 Aug "6,934 cells" had never executed at all); the
    windows.py fix landed; and 2653 tests pass across core/execution/providers/worker.

- [x] B2: A single verdict states whether Baskfy's numbers are trustworthy enough to execute
      from. "Not yet" is acceptable and must name what would change it.
  EVIDENCE: **NOT YET — but for one reason, not the three the status page implied.**

    What is now settled. M13's 16-symbol gap is **0** (239 vs 239; the honest ledger is
    16 -> 2 -> 0: M24/M27/M28 backfills took it to 2, 1.4.1's window fix took the last two).
    Top-25 parity is **25/25** (was 23/25), top-15 15/15, max |dSCORE| 4.30 pts against a
    REPLACEMENT_EDGE of 8.0 — no swap decision can differ on that corpus. M11's real count was
    6,396 of 9,186 (never previously executed), now 4,514. M14's substitution delta is CLOSED:
    SHILPAMED's 2:1 split, ex-date 2025-10-03, recovered by M24 and applied by M28; bars verified
    continuous; away_from_high now -1.890 on both sides. Order impact measured directly by
    1.4.1: 239/271 eligible with ZERO filter disagreements, top-15 basket identical.

    **CROSS-LEAF SYNTHESIS — the finding neither leaf could reach alone.**
    1.4.3's #1 residual risk is "rsi_* is 0/268 reproducible, **definition unknown**", read from
    the older PARITY-FAMILIES.md. 1.4.1 **identified it**: the reference uses **Cutler's RSI at
    period N-1**, not Wilder's at N — 267/271 exact vs 1/271 (docs/PARITY-M11.md:154-176).
    That very likely explains 1.4.3's only surviving substantive delta: WELCORP, where the desk
    reads rsi_one_month 79.86 (crossing the >78 cliff for a -2 F_penalty) and Baskfy reads
    73.61 (no penalty) — score 79.4 -> 81.3, rank 6 -> 2. A definitional difference, not drift,
    and it survives the window fix.
    So risk #1 is downgraded from "unknown definition" to "known definition, spec decision
    pending": docs/05 §5 says "Wilder's RSI" explicitly, so changing it is an amendment, and
    1.4.1 correctly did not land it.

    **What would change the verdict**, in order:
    1. Resolve the RSI definition (Cutler N-1 vs Wilder N). It sits under a hard 78 penalty cliff
       AND PARABOLIC_RSI=82.0, which trims a held position to a runner — so some Friday the two
       systems disagree about a whole position, not a share. Recurs weekly.
    2. Resolve volatility ddof (1 vs 0; ratio constant at sqrt((N-1)/N) to 7 s.f.). Same 0/268
       shape, came within 0.0004 of the 0.45 cliff, **and it sets the GTT stop** — fixing it
       moves stop prices (1.4.1 measured 133/269 names differing today by 0.01-0.07%).
    3. **The M13 blocker that replaced the old one**: `_carried_columns()` sources marketcap,
       beta, circuits and F&O from the most recent UPLOADED CSV, and momentum_scan.build
       inner-joins on symbol — so the generated scan's universe is DEFINED BY the upload.
       The generated path does not cut the CSV cord; it requires one. Driver-verified at
       kite-momentum-rebalancer/app/main.py:378.
    4. The shadow harness **has never generated a sell** — BOOK = [] bypasses every retention,
       exit, cap and turnover rule. The cheapest order is covered; the expensive ones are not.
    5. `screen_run_id` hashes definition|as_of|data_version and did NOT change when M28 rewrote
       151,922 bars, so a stored plan can name prices that no longer exist.

- [x] B3: No flag was flipped and no test weakened across the three leaves
  EVIDENCE: driver-verified. `SCAN_SOURCE_DEFAULT="upload"` unchanged at config.py:90 and
    .env.example:163; no .env touched by any leaf; DRY_RUN=true throughout; the shadow harness
    ran to a scratch --log so kite-momentum-rebalancer/data/shadow-mode.jsonl still holds only
    the two 22 Aug lines and the four-Friday counter is untouched. The uploads corpus is clean.
    test_reference_parity.py was NOT modified — it still fails, at 4,514, and reports so.
    One fixture moved (momentum_scan.csv), argued in PARITY-M11 §7 before regenerating: every
    calendar-window column moved and every bar-count column is byte-identical — the exact
    partition resolve_window feeds, which a regeneration cannot fake. Its assertion is unchanged.
