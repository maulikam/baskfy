# Retiring the systemd timers — and then retiring Beat for collection

**Status (13 Sep 2026):** collection is no longer a Beat job. It runs in the
`desk-daily` compose service on the `baskfy-desk` image (`scripts.desk_daily_loop`,
18:30 daily + 18:50 autorun, IST weekdays). Maulik, NEEDS-MAULIK §33.

The systemd timers on the old Mumbai desk box, if they still exist, are a separate
tree. The Phase A Docker box never ran them; Beat was the only scheduler, and it
was firing into an image that did not contain the desk tree.

## Why Beat was the wrong host

`baskfy_worker.tasks.desk` resolved `DESK_ROOT` six parents up from itself. In
`baskfy-py` that is `/kite-momentum-rebalancer`, which is not in the image. Both
`desk-daily-collection` (18:30) and `desk-autorun-safety-net` (18:50) failed every
weekday. Same-day fill capture was dead.

Shipping the desk tree into `baskfy-py` was rejected: it puts the live trading
code into the image the web API and every worker run.

## What replaced it

The same image as `desk`, a sibling of `swing-monitor`: no port, collection only.
`scripts.daily --quiet --source schedule` at 18:30 IST Mon–Fri, `scripts.autorun`
at 18:50. Catch-up on a weekday restart after 18:30 (`Persistent=true` of the old
timer). Both jobs are independently idempotent.

Beat keeps the Celery *names* registered as refusal stubs so a leftover Redis
message logs the move instead of looking unregistered. It must not grow those
keys back.

## The five-run rule (historical)

M19 §2 said: retire the timers only after five green Beat runs. That overlap
never completed, because Beat never had a tree to run against. The rule is
moot. Do not put collection back on Beat to "finish" it.

## Rolling back

Restore the two Beat entries in `celery_app.BEAT_SCHEDULE` and delete the
`desk-daily` compose service. That returns the weekday failure. The rejected
alternative — copying the desk tree into `Dockerfile.python` — is still rejected.
