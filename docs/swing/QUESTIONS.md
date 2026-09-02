# Questions for Maulik — written instead of asked (STANDING-ANSWERS §C)

Append, dated. Each carries the recommendation that was applied so the run continued.

## Q-SW13-1 (2 Sep 2026) — the desk process holds yesterday's Kite token until it is restarted

`app/main.py` caches the Kite client in a module global and `Kite.__init__` reads the token store
once, so the token Baskfy writes after the morning login is invisible to a desk container that
started earlier. Options: (a) restart the desk after each login — `box.sh 'cd /opt/baskfy &&
docker compose --env-file .env.staging.compose -f compose.prod.yml restart desk'` (≈10 s, no
data at risk, DRY_RUN either way); (b) a `/reload-token` route or an mtime check in `kite()` — a
desk-code change, outside SW13's ownership and the trading path's; (c) a daily 09:05 restart from
the host's cron. **Applied: (a)**, written into STATUS as a morning step; (b) recommended for a
later leaf if the restart is forgotten twice.

## Q-SW12-1 (3 Sep 2026) — the plan sizes on the unsnapped levels and shows the snapped ones

Found by the golden lane (`go/testdata/golden/L1/swing/build_entries.case_016.json`): a watch
item at trigger 123.37 / stop 118.11 is sized on that 5.26 distance (950 shares, `risk_inr`
4997.00, "stop 4.26% below"), then `_entry_line` snaps the levels to the tick — trigger
**123.35**, stop 118.10 — so the line the desk sends risks 950 × 5.25 = 4987.50, not the number
on the line, and its trigger sits *below* the pivot high it was meant to clear (`to_tick` rounds
half-up to the nearest tick; its docstring says "rounding a trigger UP"). Never more than one
tick × quantity, and `04` §9.1 only says "snapped to ₹0.05" — but A9's principle is "the line
shown is the line sent". Options: (a) leave it (bounded, documented here); (b) snap first —
trigger to the tick at or above the pivot, stop to the tick at or below the reference — then
size on the snapped levels, so `risk_inr`, `stop_distance_pct` and the `STOP_TOO_WIDE` refusal
all read the levels the order carries; (c) snap in the desk only. **Recommendation: (b)**, one
edit in `plan._entry_line` plus a `to_tick(…, rounding=)` argument, a test in
`test_swing_contract_book.TestTheTickSnap`, and a regenerated golden — not done here because
SW12's goldens dump the code as it is (the case is kept so the change is visible when it lands).
