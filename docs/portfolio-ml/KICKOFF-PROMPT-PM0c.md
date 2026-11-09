Continue the portfolio-ML run (docs/portfolio-ml/). Read README.md, STATUS.md, DECISIONS-PM.md,
STRATEGY.md and PM0.6 (survivorship) first. Save this as docs/portfolio-ml/KICKOFF-PROMPT-PM0c.md.
Autonomy charter and token budget apply. Maulik's answer to your question: yes, delisted names come first —
and the fundamentals backfill runs in parallel on the live names (it is resumable; dead names get a second
pass once they exist as instruments). Do not touch the running stack; a new migration ships with the image.

## PM0.7 — the dead: delisted and suspended names back into the plant

Goal: every symbol that traded on NSE cash from 2011 and no longer does exists as an instrument with its
bars, so no backtest here is survivors-only again.

- Source: the NSE bhavcopy archive the plant already reads (M9), 2011 →. Each daily file lists every
  symbol that printed. Key everything by ISIN, not symbol — symbols are reused and renamed
  (`symbol_alias` exists). A symbol/ISIN that appears in bhavcopy and is absent from `instrument` becomes
  a new instrument row with `is_active=false`, `listed_on` = first bar seen, `delisted_on` = last bar seen
  + 1 session, `kite_token` null, series from the file (EQ/BE/BZ in; SM/ST out). Record the count of
  resurrected names by year.
- Bars: `close_raw`/`volume_raw` from bhavcopy; `close` adjusted where `corporate_action` has rows (it
  mostly will not for dead names — say so in a coverage line); `adj_factor` 1 otherwise. Idempotent.
- `delisted_on` must also be set for existing instruments whose bars stopped — the CLI found none set.
- Terminal outcome for a dead name in the labels: when bars end before the 250-session horizon,
  `fwd_ret_h` is computed two ways and both are reported: (a) last close carried forward, (b) last close
  for 60 sessions then −100 %. Mergers, demergers and schemes of arrangement are not deaths — where
  `corporate_action` or the NSE delisting-reason field says so, use (a) only. Add `terminal: str` to the
  dataset (`alive`, `merged`, `delisted`, `suspended`) and the `--terminal` flag to the research scripts.
- AC1 migration + backfill command in baskfy-worker, parsers unit-tested on three archive days
  (2012, 2018, 2024). AC2 `test_no_survivorship`: the share of instruments with `delisted_on IS NOT NULL`
  is > 10 % of the 2011 → universe (if the true figure is lower, record the true figure and make the test
  assert it). AC3 idempotent — same table hash on re-run.

## PM0.8 — re-run twelve years with the dead included

Same features, same model, same purge, both terminal variants. Report the §3 table and the §4 book from
2014 (investable, top-10 and top-20, rebuy and skip-held) beside the survivors-only numbers already in
STRATEGY, and rewrite §0, §3, §4 and §6 from what they show. The number Maulik plans on is the (b)
variant's book from 2014. Then re-test the regime gates over 2014 → (Smallcap-250 above 50-DMA;
breadth200 ≥ 0.40; breadth200 ≥ 0.50): the gate was rejected on a bull-only window, and the twelve-year
table says the top-20 lags the universe in every weak year, so the decision deserves the full window.
Record as PM0.8; do not pick the best gate, report all three with and without.

## PM4 gate — update

PM4 now runs with the dead included and judges 2024, 2025 and pooled 2024+2025 on terminal variant (b).
Update 06-module-plan PM4 AC1 and `run_pm4.py`'s refusal check (it must also refuse if PM0.7 has not
run: `delisted_on` all null).

## Report

Short: resurrected-name counts by year, the survivors-vs-all §3 and §4 tables, the gate table, commit
hashes, open items. The fundamentals backfill (Maulik's hand) is unchanged and may already be running.
