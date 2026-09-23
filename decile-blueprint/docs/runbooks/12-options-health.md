# Runbook 12 — the options book's health (`OPTIONS_*` rules and checks)

**Verified against:** NOT YET — written from `baskfy_api/options_health.py`,
`baskfy_worker/options/checks.py`, `infra/prometheus/alerts.yml` (`baskfy-options`) and the desk's
`app/options_clock.py` (OP14). Every rule has fired from a synthetic series and every check from
fixtures on `baskfy_test`; none has fired on the box. Every options money flag is **false**. Every
position this runbook talks about is a paper position unless `op_journal.simulated` says otherwise.

Two nets watch the same facts. The **worker's checks** (`baskfy.options.checks`, Beat
`options-checks`) run inside the worker at the minute each fact matters and raise
`OPTIONS_CHECK_FAILED` with `labels.check`. The **Prometheus rules** read gauges the API
publishes once per scrape. A failure usually arrives from both.

## `OPTIONS_OPEN_AFTER_HARD_EXIT` / check `FLAT_AFTER_HARD_EXIT` — critical

An `op_position` is still open two minutes (rule) or three minutes (check, at 14:33, 14:48 and
15:03) after its sleeve's hard exit: O1 14:30, O3 14:45, O2 15:00. This should be impossible,
because the monitor raises `HARD_EXIT` at the time and its sweep closes it within a second.

1. Is the desk's monitor running? `systemctl status baskfy-options-monitor` on the box, or the
   desk's `/nifty-options` status bar. If it is down, start it. On start it resumes from
   `op_position`, sees the hard exit has passed and raises the exit at once.
2. Is there an exit plan? `SELECT s.plan_id, p.exit_plan_id, x.status FROM op_position p JOIN
   op_session s ON s.id = p.session_id LEFT JOIN op_plan x ON x.plan_id = p.exit_plan_id WHERE
   p.closed_at IS NULL;`. An `ISSUED` exit that stays `ISSUED` means the sweep cannot close it:
   usually no quotes (the token, below). Press **Close now** on `/nifty-options`.
3. **Paper** (`simulated=true`): nothing is at risk. Close it from the page so the journal is
   complete.
4. **Live** (not built; OP10.3): close in Kite yourself, **shorts first, then the wings**. Never
   leave a short leg without its long.

## Token death with a position open (desk log `options monitor: Kite token rejected`)

Kite tokens expire overnight and can be revoked in the day. When the ticker stops and the
monitor's fallback quote read is refused as a token error, the monitor raises
**`HARD_EXIT / FEED_LOST` for every open position at once** (`04` §8.5, OP14). It does not wait
for 14:00. The sweep then tries to close them, but with the token dead there are no quotes to
price a fill, so the exits stay `ISSUED` until a quote read succeeds again.

What the human does:

1. Log in to the desk again (the Kite login on the desk). The next quote poll succeeds, the sweep
   closes each raised exit, and the journal records `FEED_LOST`.
2. If you cannot log in within a few minutes and any position is **live**: close it in the Kite
   app, **shorts first**, and write the fills into the note on the desk's session. A paper
   position can wait for the login.
3. `NEEDS-MAULIK.md` carries the same steps.

## `OPTIONS_COLLECTOR_GAP` / check `COLLECTOR_FULL_DAY` — warning

The collector (`baskfy.options.collect_chain`, each minute 09:15–15:30 with
`BASKFY_OPTIONS_COLLECT_ENABLED`) has written no chain minute for more than three minutes in session
(rule), or today has fewer than 360 of the session's 375 minutes (check at 15:35). Plans and the
scan read stale chains; Tier 3 loses those minutes for good.

1. `docker compose logs worker | grep collect_chain` on the box: a Kite session missing (`no usable
   Kite session`), the limiter (`KiteRateLimited`), or a trading-day refusal.
2. A token problem is the morning login (`docs/runbooks/kite-token-expired.md`).
3. Nothing to backfill: an option quote has no history. Record the gap in STATUS if it was long.

## `OPTIONS_SCAN_STALE` / check `SCAN_STARTED` — warning

The scan (`baskfy.options.scan`, behind `BASKFY_OPTIONS_SCAN_ENABLED` and the collect flag) has not
written `op_scan` for more than three minutes in session, or at 09:20 some sleeve has no scan row
since 09:15. The web tab shows stale states. Check the collector first: the scan reads what the
collector wrote. Then check the worker's logs for `options.scan`.

## `OPTIONS_NO_SESSION_ON_TRADING_DAY` / check `O1_DECIDED` — warning

With the plan builders running (`BASKFY_OPTIONS_MONITOR_ENABLED`), no `op_session` exists today by
10:20 (rule; O2 decides every trading day). Or, on an O1 day, O1-M or O1-W has no verdict or plan
by 10:20 (check). Beat may not be running (`docker compose ps beat`), the plan tasks may be failing
(`grep plan_o1\|plan_o2 worker logs`), or the collect flag may be off: the builders are dark without
chains.

## `OPTIONS_LIMITER_SHARE_HIGH` — warning

The collector's quote calls over ten minutes are above a quarter of the Kite quote family's
budget. It makes one call a minute by design (about 2 %), so this means it is calling more than it
was built to, and the swing, TWT and VBT mornings share that budget. Turn the collector off
(`BASKFY_OPTIONS_COLLECT_ENABLED=false`, redeploy) and read `op_chain_snapshot`'s minute counts to
see what changed. The desk's own fallback polls are not counted here (DECISIONS-OP OP14.2).
