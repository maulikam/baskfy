# The corporate-action feed has been silently truncated to 20 rows a night

**Found 11 Sep 2026**, while diagnosing why the nightly data-quality gate refused to publish
2026-09-11. The gate said: *"1 of 4240 instruments moved more than 50% with no corporate action
or circuit to explain it"*. The instrument was **PGIL** (Pearl Global Industries), 2378.40 ->
1187.70, a fall of 50.1%.

**The move is real and the bar is right.** NSE's own record for PGIL, queried by symbol, says
`11-Sep-2026 | Bonus 1:1`. Kite agrees independently: asked for PGIL's last ten sessions *today*,
it returns 10 Sep close **1189.20** where our table holds 2378.40, and volume **585,852** where
we hold 292,926 — an exact halving of price and doubling of volume, which is what a 1:1 bonus
does and what `bonus_factor(1, 1)` already computes. Nothing about the price data is wrong.

**What is wrong is that we never received the action.** `NSEProvider.corporate_actions` fetches
`/api/corporates-corporateActions?index=equities` **with no date range**, and NSE answers a
default first page. Every one of the twelve archived payloads on the box holds *exactly 20 rows*:

    2026-08-20.json rows=20   2026-08-26.json rows=20   2026-09-02.json rows=20
    2026-08-21.json rows=20   2026-08-27.json rows=20   2026-09-03.json rows=20
    2026-08-24.json rows=20   2026-08-28.json rows=20   2026-09-04.json rows=20
    2026-08-25.json rows=20   2026-08-31.json rows=20   2026-09-01.json rows=20

The same endpoint with `from_date=01-09-2026&to_date=30-09-2026` returns **248**. So we have been
ingesting roughly 8% of the market's corporate actions, chosen by whatever NSE puts first, since
the feed went in. 228 of the 240 rows we ever captured are dividends, because dividends crowd the
head of the queue; on 11 Sep the 20-row page did not even cover that single day's actions, which
is how PGIL fell out.

**Why this is worse than one missed bonus.** House rule 6 says `close` is adjusted. An action we
never store is an action `apply_adjustments` never applies, so the adjusted series is wrong for
every instrument whose action fell outside the window, and the factor engine reads `close`. A
missed dividend is a small error. A missed bonus or split is a fabricated ±50% return feeding
momentum. The gate caught this one only because it was large enough to trip a 50% threshold.

**Archive interaction, which makes the naive fix a no-op.** `fetch_and_archive` never re-fetches
an existing key ("Never re-fetch to re-parse", docs/09), and the key is `since`-dated. Changing
the URL alone would keep re-parsing the truncated file for any `since` already on disk. A changed
request is a different file.

## Gates

- [x] G1: The request names an explicit date window. No un-ranged call to that endpoint survives
      in the provider.
  CHECK: cd decile-blueprint && grep -c "from_date=" packages/providers/src/baskfy_providers/nse.py
  EXPECT: /^[1-9]/
  EVIDENCE: 1

- [x] G2: **Truncation is detected, not assumed away.** The provider refuses a payload that looks
      like a default page rather than an answer to the window it asked for. A silent 20 is the
      whole incident; the code must be unable to accept one quietly again.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests -k "CorporateActionWindow and refused" -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 323 deselected in 0.33s

- [x] G3: The archived payload for the new request is a **different key** from the old truncated
      one, so the old record survives as evidence and is never re-parsed as if it were complete.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests -k "CorporateActionWindow and archived" -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: .                                                                        [100%] | 1 passed, 324 deselected in 0.18s

- [x] G4: A bonus reaches the typed record with the ratio the adjustment math expects. PGIL's
      actual purpose string, `Bonus 1:1`, parses to `("bonus", 1, 1)` and `bonus_factor` turns
      that into price 1/2 and volume 2 — the factor Kite independently applied.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests packages/core/tests -k "bonus" -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: .........                                                                [100%] | 9 passed, 4466 deselected in 2.21s

- [x] G5: **The damage is measured, not hand-waved.** A count of how many actions NSE holds for
      the period we have been running against how many we stored, written into the incident note.
  EVIDENCE: NSE, asked with an explicit range, holds **606** actions with an ex-date in
      2026-08-01..2026-09-30 (August 358, September 248). `corporate_action` holds **146** for the
      same window — 137 dividends, 6 splits, 2 demergers, 1 bonus. **460 missing, 76%.**
      Narrower and sharper: NSE published **87** actions for 2026-09-11 alone; we stored **20**.
      NSE's non-dividend actions in the window number 21 (5 splits, 5 bonuses, 9 rights,
      2 demergers); we hold 9, and 5 of those 9 did not come from the feed at all — they carry
      `measured_factor`/`shape` in `raw`, so `action_recovery` inferred them from the gap between
      Kite's adjusted series and the NSE print. The feed contributed 4.
      Wide query sanity: 01-01-2024..31-12-2026 returns 6,204 rows, so the ranged endpoint is not
      itself capped; the 20 was only ever the missing range.

- [x] G6: The window the nightly asks for covers the actions it needs, including ones announced
      ahead of their ex-date, and is a field of settings rather than a literal.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests -k "CorporateActionWindow and window" -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: ........                                                                 [100%] | 8 passed, 317 deselected in 0.27s

- [x] G7: `make lint` clean and the providers suite green — the tree is not left broken.
  CHECK: cd decile-blueprint && uv run pytest packages/providers/tests -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: .....................................                                    [100%] | 325 passed in 22.91s

- [ ] G8: **2026-09-11 publishes.** PGIL's bonus is in `corporate_action`, the adjusted series is
      corrected, factors are recomputed on the corrected series, and the data-quality gate passes
      on its own terms rather than being lowered.
      ⏳ **Still unmet on 12 Sep 2026, and now for a different reason than before.** It used to be
      blocked on the deploy; the deploy happened (`8b074c7`, `gates/deploy-pc-command-center.md`),
      so the *code* that asks NSE for a date range is on the box. **The data has not been re-fetched,
      and shipping the fetcher does not re-fetch anything.** Measured against the box directly, via
      `tools/deploy/box-sql.sh`:
      | | On the box now | NSE holds (G5) |
      |---|---|---|
      | `ex_date` in 2026-08-01..2026-09-30 | **147** | 606 |
      | `ex_date` = 2026-09-11 | **22** | 87 |
      | PGIL bonus rows | **0** | 1 |
      So the 76% gap G5 measured is still there, unchanged but for one row. What closes this is a
      run of the corporate-action fetch against the repaired code — the next nightly chain, or a
      deliberate backfill of the window. Neither is a deploy and neither happened here: this
      session was asked to verify the box, and writing 460 rows into the production
      `corporate_action` table is remediation, not verification.
  EVIDENCE: pending — code deployed, data not re-fetched. 147/606 for the window, 22/87 for 11 Sep, PGIL bonus absent (12 Sep 2026).

- [x] G9: The quality gate's threshold is **not** weakened anywhere in this repair. The gate was
      right; it is the only reason this was found.
  CHECK: cd decile-blueprint && git diff --stat -- services/worker/src/baskfy_worker/tasks/quality.py | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: 0
