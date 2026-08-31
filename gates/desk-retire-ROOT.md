# Gates: ROOT — retire desk.modelbasket.in

- [x] R1: All five branch gate files worked, with every unmet gate accounted for
  CHECK: for f in gates/desk-retire-1.1.md gates/desk-retire-1.2.md gates/desk-retire-1.3.md gates/desk-retire-1.4.md gates/desk-retire-1.5.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 2
  EVIDENCE: 1.2, 1.4, 1.5 fully met. 1.1 = 4 met + 1 ABANDON (B2 needs a calendar day that has
    not happened: Kite mints one token per login per day, so "a session dated after 31 Aug" is
    unprovable ON 31 Aug; the capability is proven — a live unexpired session is in the store).
    1.3 = 3 met + 1 genuinely UNMET (B2), which is the branch's finding, not a shortfall:
    the migration is a rehearsal in the LOCAL dev Postgres; the deployed box still returns 0 for
    `trades`/`fills`. EXPECT is 2, not 0, because an ABANDONed gate stays unchecked by design and
    a root demanding 0 would pressure a branch into faking a pass.

- [x] R2: A single go/no-go statement on retiring the desk, with every blocking item named
  EVIDENCE: **NO-GO today. Four blockers, none of them the ones the status page named.**
    1. **Non-negotiables #1, #4 and #6 are enforced by files that retiring the desk deletes.**
       confirm/plan_id/30-min expiry exist ONLY at `app/main.py:519-524`; `client_id =
       plan_id:symbol` is constructed at `main.py:581`. 1.2.3 measured it independently: an AST
       scan of all five source roots finds `PLAN_TTL` mentioned 15 times and compared against a
       clock **zero** times. Retiring the desk deletes the enforcement, not just the code.
    2. **The trade record is not in Baskfy.** 43,411 rows migrate correctly — into the local dev
       Postgres. The box has none, and there is no path between the trading box and the Baskfy
       box, so any cutover today is laptop-mediated.
    3. **Two definitional gaps sit under decision cliffs.** RSI: the reference uses Cutler@N-1,
       Baskfy uses Wilder@N (1.4.1, 267/271 exact) — and 1.4.3's surviving WELCORP delta is
       exactly that, desk RSI 79.86 crossing the >78 penalty cliff where Baskfy reads 73.61.
       Volatility ddof=1 vs 0 — same shape, and **it sets the GTT stop**. Both recur weekly.
    4. **The generated scan cannot replace the CSV; it consumes one.** `_carried_columns()`
       (`app/main.py:378`) sources marketcap/beta/circuits/F&O from the most recent UPLOAD and
       `momentum_scan.build` inner-joins on symbol, so the generated universe is defined by the
       upload. The 16-symbol gap that blocked M13 is gone (0); this replaced it.
    Item 1 of Maulik's list (an execute route) was scoped OUT and stayed out: `routers/kite.py`
    has 0 `@router.post`. The decision is written for signature at docs/DECISION-EXECUTE-ROUTE.md.

- [x] R3: Every number in the final report re-measured at report time, not quoted from memory
  EVIDENCE: re-measured 2026-08-31 by the driver, not by a leaf. Ledger 104/113 met, 3 ABANDON,
    counted by `grep -c` over gates/desk-retire-*.md. Suites: 2653 passed / 239 skipped / 0
    failed across execution+core+providers+worker. Friday drill: PASS, 25 of 25, exit 0.
    Independent migration verifier: 19 tables, 43,411 = 43,411, ALL TABLES MATCH, exit 0.
    `packages/execution`: 132 tests, collected for the first time after the driver defined the
    missing `OAuthStart` and added the directory to `testpaths`.
    THREE status-page numbers were caught stale by re-measurement and none was repeated:
    M11's "6,934/9,166" had **never executed** (its two full-row cases skip on an unset
    BASKFY_PARITY_BARS); real figure 6,396, now 4,514. M13's "223 vs 239" is now **239 vs 239**.
    M14's "4 deltas" is 2 at HEAD / 3 with the window fix, and the corporate-action substitution
    is closed. docs/00-merge-status.md:110 still carries the stale M13 figure and should be
    corrected — it keeps M13 blocked for a reason that no longer exists.

- [x] R4: The desk is still able to rebalance on Friday 4 Sep, or Maulik has been told plainly
  EVIDENCE: able, and Maulik has been told what it now depends on. Driver-verified over ssh
    2026-08-31 18:30+: `momentum-web`, `momentum-daily.timer`, `momentum-backup.timer` all
    **active**. The desk ran its own scheduled daily job during this work and wrote exactly the
    four rows a daily run writes — `daily_runs` 26->27, `snapshots` 15->16, `regime_evaluations`
    26->27, `regime_exposure` 26->27 — total 43,411 -> 43,415.
    Friday now depends on 1.1.5's bridge (live 302 verified end to end through the public host)
    plus ONE human Zerodha login. Runbook: NEEDS-MAULIK.md §30, with a no-repo fallback.
    `BASKFY_KITE_API_SECRET` must STAY EMPTY — setting it would let Baskfy redeem the single-use
    request_token and starve the desk. This reverses the driver's own earlier advice, twice given.

- [x] R5: No live order was placed at any point by any leaf
  EVIDENCE: proven by the tables that would have changed. Against 1.3.1's per-table baseline,
    **`trades` 8,530, `fills` 9,609, `rebalance_orders` 425 and `rebalance_versions` 35 are all
    unchanged** — driver-diffed all 19 tables 2026-08-31 18:30+; the only four that moved are the
    daily-run heartbeat above. 1.1.5 additionally confirmed **Kite's own order book reports 0
    orders today**, with 0 `POST /execute` and 0 `POST /stops/arm` in the journal (the single
    `/analyze` is Maulik's own at 14:39 from his IP, three hours before any agent command).
    The Friday drill ran with DRY_RUN=true and journalled 27 lines with zero post-broker events;
    its `--mutate touch-the-broker` probe proves that assertion can fail.
    `DRY_RUN=false` was never set in any shell by any leaf.
