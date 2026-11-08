# Runbook — a rebalance is half-executed

**Alert:** `desk_orders_failing_systemically` (critical), `desk_buys_without_stops` (critical),
`desk_orders_rejected` (warning), `desk_execute_slow` (warning)
**Raised by:** the Prometheus rules in `infra/prometheus/alerts.yml`, over the metrics
`app/telemetry.py` emits from `/execute` and `/stops/arm`
**Verified against:** NOT YET — written from the 18–19 Aug 2026 incidents in the desk's own
journal and from `app/main.py`'s execute path, not from a rehearsal. Staging arrives with AWS
Phase B. See `docs/runbooks/README.md`.

**A half-executed rebalance is the desk's most dangerous ordinary state**, and it is not rare: the
batch places sells first to free cash, then buys, and any interruption between the two leaves the
book holding cash it meant to deploy — or worse, holding new positions with no stops under them.

It has happened. On **18 Aug 2026** twenty-one orders were fired into the same rejection, *"No IPs
configured for this app"*, because nothing noticed the first three had failed identically. On the
same day the book carried **10,383 shares of GTT against 9,478 held** — SONACOMS covered 2.5×,
RADICO 3.1×, and a 438-share stop sat on PARAS before a single share of it had filled.

## First: is anything unprotected right now?

Before diagnosing anything, answer one question — **does every position have a stop?** An
over-covered trigger sells shares you do not own when it fires, which is short delivery. An
uncovered one is a position with no floor.

Open `/stops`. It sizes every trigger from the broker's own holdings, not from the plan, and it can
cancel the wrong ones. If it shows missing or oversized triggers, fix those **before** reading
further. Nothing else in this runbook is more urgent.

## Then: establish what actually happened

The plan's execution log is the record, and it is local:

```
data/outputs/execution_<plan_id>.json
```

It holds the plan, every order's result, and the GTT state at the time. The `status` on each order
is the gateway's own vocabulary — `COMPLETE`, `REJECTED`, `BLOCKED`, `RISK_BLOCKED`, `ABORTED`.

`ABORTED` means the circuit breaker stopped the batch: three identical failures in a row. **Those
orders were never sent.** Everything before the trip was.

Cross-check against the broker rather than trusting the log alone:

```
/reconcile?plan_id=<plan_id>
```

The desk's record can be wrong in exactly one way that matters — the batch reached the broker and
the write to record it failed afterwards. That is the case `desk.execute` captures to Sentry with
`stage=record_execution`, and it is why this step compares against Kite instead of re-reading our
own JSON.

## The three shapes this takes

### 1. The batch tripped the breaker

Orders before the trip are real. Read the `aborted_for` reason, because it is almost always
systemic and almost never per-order:

* **"No IPs configured for this app"** — the box's IP changed. A residential connection does not
  hold one; `103.238.14.245` became `49.43.34.118` once already. Fix it at
  developers.kite.trade, then re-run Analyze and execute the remainder.
* **A token error** — `docs/runbooks/kite-token-expired.md`. Log in, then re-run.
* **Margin** — the preflight basket-margin check should have caught it. If it did not, the plan is
  larger than the account can fund; re-run Analyze after the sells settle.

**Do not simply re-post the same plan.** Re-run Analyze first. The plan is refused after thirty
minutes for a reason: its limit prices are the prices at the moment it was built, and a quantity is
`capital × weight ÷ price`. A stale price is a wrong position size.

Re-posting *is* safe against duplication if you do it inside the window — every order carries a
deterministic `client_id` of `plan_id:symbol`, so the gateway's idempotency refuses a second send
of the same order. That protects against double-sending. It does not protect against sizing a
position from a price that has moved.

### 2. Buys completed and stops did not

This is `desk_buys_without_stops`, and it is non-negotiable rule 4 — *every buy gets a stop the
same session*.

Stops are armed from `/stops`, deliberately **after** fills are known: these are LIMIT orders and
some rest unfilled, so arming at submission time would size every trigger to the planned position
rather than the actual one. That is precisely how the 18 Aug over-coverage happened.

So: wait for the fills, then arm from `/stops`. If the session is ending and orders are still
resting, cancel the unfilled remainder rather than carrying an unstopped position overnight.

### 3. The orders went out and the desk does not know it

The rarest and the most confusing. Symptom: `/reconcile` shows broker orders the plan has no record
of, or a Sentry event tagged `stage=record_execution`.

The broker is the system of record for what was traded. Re-run:

```
/reconcile?plan_id=<plan_id>
```

and let it fold the broker's state back onto the stored plan. Do **not** re-execute to "make the
records agree" — that places real orders to fix a bookkeeping problem.

## What this runbook cannot tell you

**Whether the strategy still wants the second half.** A rebalance interrupted between sells and
buys leaves the book in a state neither the old plan nor the new one describes. If more than a
session has passed, the honest move is a fresh Analyze against fresh prices, not a resumption:
momentum ranks move, and half of yesterday's basket is not a basket.

## Verifying this runbook

It cannot be verified today. There is no staging environment with a broker in it, and the failure
modes above are not reproducible against production without placing real orders.

What **can** be checked now, and should be after any change to `/execute`:

```bash
DRY_RUN=true .venv/bin/python -m pytest tests/ -q      # the whole desk suite
make backend-parity                                    # both backends still agree
```

When AWS Phase B brings staging, this line becomes a date and a name.
