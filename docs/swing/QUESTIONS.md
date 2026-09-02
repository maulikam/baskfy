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
