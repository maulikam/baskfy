# 06 — Module plan: FO0–FO12

One commit per module, `FO<N>: green — <one line>`. A module is green when its acceptance criteria
pass as tests (or, for a page, when it renders in the dev stack and a browser check covers it),
`make lint` is clean in the touched trees, **both trees' suites still pass** (the desk must
rebalance on any Friday; swing, TWT, VBT and the O-sleeves must run on any morning), and
`STATUS.md` + (if judgement was exercised) `DECISIONS-FO.md` are updated. Criteria are proxies for
Goals; the charter's precedence order applies.

**When: after the OP run.** Maulik, 23 Sep 2026 (M.1): FO0 starts only when OP15 is ✅.

**Order: data and information ship first.** FO0–FO5 end with the Stock F&O page and F1's nightly
scan on the web. The gateway change comes after that, and it is the only module that touches
`packages/execution`.

```
FO0 → FO1 → FO2 → FO4 → FO5            (data + scan + pages on the web)
             FO2 → FO3                   (spread sample; independent)
             FO2 → FO9                   (re-test engine; reproduces RESEARCH.md)
      FO1 → FO6 → FO7 → FO8 → FO10       (guard, desk, paper, journal)
                  FO9 + FO10 → FO11 → FO12
```

**Coordination with the OP run.** OP8–OP15 are unfinished on the same branch and touch the same
files: `guards.py`, `celery_app.py`, the Options tab and the desk nav. FO6 must rebase its guard
change onto whatever OP13 has asserted, and it must never weaken an OP test. If OP work is
uncommitted in the tree, an FO module commits only its own files (never `git add -A`).

---

### FO0 — Baseline and the facts this run stands on

**Goal:** a fresh session knows where it stands and has verified what `04` depends on.

- Record in STATUS: branch, HEAD, others' dirty files, the Alembic head, the Beat inventory, the
  flags in every env file, and the OP run's position.
- Verify and record, with URL and date: (a) the broker's current **physical-delivery margin ramp**
  for stock F&O (for `07` §3; the numbers are not asserted by this pack); (b) BANKNIFTY's monthly
  expiry in the NFO master (weekday, lot size, strike step) for the next four months; (c) that the
  box can read the F&O bhavcopy through `NSEProvider.fo_bhavcopy` (one day, archived); (d) the
  current F&O ban-list file on NSE and its URL shape, via the NSE provider only.
- Re-run `RESEARCH.md`'s B4 (N = 15) from `docs/fno/evidence/research/` against the stored data
  and record that it reproduces: n = 100, +0.033R ± 0.002 (and +0.022R with the loss close); and
  F2's spec (`res_f2.py`): n = 2,334, +0.017R.

### FO1 — The pure core `baskfy_core.fno`

**Goal:** every number in `04` is a pure function with a test, and the research families are
importable, not scratch scripts.

- `config` (every field of `04`, with bounds), `series` (continuous futures, the CA flag, ATR,
  RV), `vol` (ATM IV via `options.greeks`), `calendar` (monthly expiry and entry session from the
  master and `trading_day`), `condor` (F1's strikes, credit, max loss, sequences), `covered`
  (`is_covered(book_after)`, and the prefix and partial-fill property), `sizing`, `costs` (reusing
  `options.costs`), `exits`, and `research` (the `RESEARCH.md` families as functions of frames).
- Purity test (no I/O imports), mypy strict, and the escape-hatch scan.
- **The property test for `covered`** over every prefix and partial fill of both sequences, with
  randomised strikes and quantities.

### FO2 — Schema, the nightly bhavcopy ingest, the backfill, the widened master

**Goal:** `fo_contract_daily` is filled nightly from 2022 on and idempotent, and the NFO master
knows every F&O underlying.

- Migration `<next>_fno` (the tables of `03`, with monthly partitions for `fo_contract_daily`).
- Task `baskfy.fno.ingest_bhavcopy` and Beat `fno-bhavcopy` (18:30, retry hourly to 23:30), behind
  `BASKFY_FNO_SCAN_ENABLED`. It uses `NSEProvider.fo_bhavcopy` (already built, PACK.2) and applies
  `03` §1's retention.
- The resumable CLI backfill from 2022-01-03. It is one request per session at the NSE limiter,
  about an hour and a half on the Mac as measured on 23 Sep 2026.
- `op_contract`'s nightly refresh widened from NIFTY to every underlying in `instruments("NFO")`,
  **with the O-sleeves' reads still filtering to NIFTY** (their tests prove it).
- `fo_underlying_daily` derived after each ingest.

### FO3 — The 15:00 spread sample

**Goal:** real bid-ask spreads replace the research's assumed 3 %.

- Task at 15:00 on trading days: one Kite `quote()` over the ATM ± 3 strikes of the near monthly
  of the top 30 stock underlyings by futures turnover, plus NIFTY and BANKNIFTY (≤ 500 keys), into
  `fo_spread_sample`. It goes through the shared read limiter, behind `BASKFY_FNO_SCAN_ENABLED`,
  and makes no call without a Kite session.
- It also reads NSE's event calendar for those names (QUESTIONS Q8), forward only.

### FO4 — The nightly scan

**Goal:** after each ingest, F1's state for the next session is on the web.

- `baskfy.fno.scan` writes `fo_scan` per underlying for F1 and per candidate stock for F2 (`04` §8,
  §10) from the database only, idempotent per `(user, sleeve, date, symbol)`.

### FO5 — API and pages

**Goal:** Maulik sees the Stock F&O page and F1's overnight panel on his phone.

- `routers/fno.py`: GET-only, plus the settings PATCH (money-free, audited). Every other verb is
  a 405, and `test_fno_readonly.py` checks both sides.
- `/options/overnight`, `/options/fno`, and the sub-nav (`05`). The clock labels are exactly
  `05`'s. **Root CLAUDE.md's clock table gains the two rows in the same commit.**

### FO6 — The gateway: carry on NFO, covered-overnight guard, `fno_gates()`

**Goal:** an FO order can be NRML only when it is covered, and nothing else changes.

- `product_exchange_refusal` gains the NRML branch of `02` §1, and the GTT guard its F2
  stock-future branch (option GTTs stay refused; tested).
  `assert_overnight_option_is_covered` runs before the network. It reads the broker's positions
  and the plan's filled legs, and refuses otherwise.
- `fno_gates(sleeve)` ANDs the four flags. The execute path consults it, and with the sleeve flag
  off the result is simulated regardless of `DRY_RUN`.
- Tests: every product × venue × flag combination; an O-sleeve order under NRML is still refused
  by the old guard; a naked short is refused at every prefix; recording-broker cases show 0 calls
  on refusal. **The desk's `test_seven_non_negotiables` and the OP gating tests stay green,
  unedited.**

### FO7 — Desk process `fno_monitor`

**Goal:** plans are raised, exits happen, and positions carry, on paper.

- F1: at 09:20 on an entry day, the plan from `fo_scan`, re-priced on live quotes, with the
  broker's basket margin as a ceiling. During the session, the profit-take and **loss-close**
  checks on live mids every 60 s. At 15:00 on `hard_exit_date`, the exit plan, shorts first.
- F2: the 09:20 plans; the GTT placed in the fill's session; each evening the trail moved and the
  GTT modified; the E−1 15:00 roll plan; the 40-session exit.
- Nightly: a `fo_mark` at the settle.
- **Replay tests** over recorded fixture months: F1 entry, carry, profit take, loss close and E−1
  exit; F2 entry, trail, GTT modification, a roll, a stop-out and a time exit.

### FO8 — Desk page `/fno` and `POST /fno/execute` (paper)

**Goal:** one click confirms a paper F1 or F2 plan, and the simulated fills, GTT, carry, rolls and
exits all happen.

- `05` §4. The simulated fill walks the live depth (options PACK.2's simulator). The journal is
  `simulated=true`.

### FO9 — The re-test engine

**Goal:** `RESEARCH.md` reproduces from the database, and re-runs itself each quarter.

- `baskfy.fno.retest`: every family in `RESEARCH.md`'s tables, run through
  `baskfy_core.fno.research` on `fo_contract_daily`, into `fo_backtest_run`.
- **A golden test**: on the 2022-01-03 → 2026-09-22 data, B4 N=15 gives n = 100 and net R within
  ±0.002 of +0.033 (+0.022 with the 1.5× loss close), F2's spec gives n = 2,334 and +0.017R with
  rolls, and B1's N=10 top-30 row matches its table. A drift is a bug in the port, not
  a new finding.
- Measured slippage replaces the assumption where FO3 has ≥ 20 sessions of it.

### FO10 — Journal, ledger, pauses

**Goal:** per underlying and book, in ₹ and R, with `04` §7's pauses, paper and live never pooled.

### FO11 — Gating and safety proof

- No `BASKFY_FNO_*AUTO*` name is read anywhere. The web app has no route to the gateway. With
  every FO flag false, no NRML order can be formed (a fuzz over the execute route). The options
  pack's `OPTIONS_ENABLED` side-door test is green. `frozen/` is untouched.

### FO12 — Hardening, deploy, final report

- Alerts: plan issued, exit done, hard exit tomorrow, `LATE_EXIT`, a missing bhavcopy. Deploy
  per the deploy memory rule, outside market hours and the nightly window, with every money flag
  still false. Then write `FO-FINAL-REPORT.md` at the root: what was built, what was decided, what
  is NOT done, what needs Maulik, and the first paper entry day step by step.
