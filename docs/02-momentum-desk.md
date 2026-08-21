# 02 — Kite Momentum Rebalancer (Product B)

**What it is:** a personal momentum-portfolio desk for NSE cash equities. It places real orders,
in a real Zerodha account, from a real hostname, today.

**What it is not:** a product. There is no user model, no audit of who did what, no rate limit on
the login. Its own deployment README says so explicitly and calls that the right next control
"if this ever has a second operator."

---

## 1. Scale and shape

| Area | Lines |
|---|---:|
| `app/` total | 20,296 |
| — `app/main.py` (FastAPI + 13 Jinja pages, 40 routes) | 1,039 |
| — `app/analytics/` (26 modules, SQLite persistence + views) | 9,767 |
| — `app/core/` (gateway, guards, risk, ratelimit, ticker, **regime 1,184 + regime_alloc 414**) | 3,412 |
| — `app/strategies/strangle/` (paper options lab, **gated off**) | 4,347 |
| — `scoring.py` 124 · `rebalance.py` 231 · `kite_client.py` 267 · `costs.py` 118 · `config.py` 290 | 1,030 |
| Tests | 53 files |

Stack: FastAPI + Jinja2 + vendored Tailwind v4 (no CDN) · pandas 3 / numpy 2 · kiteconnect 5.2 ·
**SQLite (WAL)** · uvloop + orjson. Every dependency pinned, with a comment explaining that an
unpinned Starlette once landed mid-session and 500'd the webview.

91 commits, written in complete sentences: *"Fix a crash that killed the session on the tick it
started winning."* *"Stop the execution report calling a refused order submitted."*

## 2. The flow it runs

```
weekly momentum scan CSV (downloaded by hand from momoindiascreener.in)
        │
        ▼  scoring.py — Momentum Quality Score /100
   filters → A_trend 25 · B_momentum 25 · C_sharpe 20 · D_consistency 10
             · E_liquidity 10 · F_penalty −10
        │
        ▼  rebalance.py — vs LIVE Kite holdings + cash
   target weights · cluster caps · breadth-driven cash bands · min trade value
   · cost model · half-size for short history · runner caps · vol-scaled GTT stops
        │
        ▼  webview: the full plan, every order, nothing executed
        │
        ▼  POST /execute  (plan_id + confirm=true, plan expires in 30 min)
   core/gateway.py → guards → risk → rate limit → journal → Kite
        │
        ▼  sells, then buys, then a GTT stop on every position
```

## 3. What makes it good — the non-negotiables

Its `CLAUDE.md` carries seven rules marked *do not "improve" these away*, and each one is a scar:

1. **Never auto-execute.** Orders fire only from `POST /execute` with `confirm=true` and the
   `plan_id` issued by `/analyze`. `DRY_RUN=true` simulates end to end.
2. **Holdings quantity = `quantity` + `t1_quantity` + `collateral_quantity`.** Missing this once
   caused a 103-share position error.
3. Pledged shares **sell directly** on Zerodha (instant-sale, Oct 2024) — flagged as info only.
4. **Every buy gets a GTT stop the same session**, vol-scaled 8–12% via `stop_from_vol()`.
5. Product gates: CNC-only. MIS needs `INTRADAY_ENABLED`, NFO/BFO needs `OPTIONS_ENABLED`.
   Both default **off** and are enforced inside the gateway, not at the call site.
6. **All order flow goes through `core/gateway.py`.** `guards.py` blocks `SGB*`/G-sec at the
   lowest layer — an untouchable instrument raises *before any network call*. `client_id =
   plan_id:symbol`, so re-posting a plan cannot double-send.
7. Filter-rejected stocks are never bought; SGBs are untouchable.

The guard rule earned itself: `EXCLUDED_SYMBOLS` held `SGBDE31III` while the actual holding was
`SGBDE31III-GB`, so an exact-match set missed it and the planner proposed **EXIT −392** on a
₹60 lakh position it must never touch. The fix was moving the check to a prefix- and
series-aware guard shared by planner and gateway — *"a plan is not made safe by the layer that
happens to execute it."*

## 4. Beyond the rebalancer

- **Regime overlay** (`core/regime.py`, 1,184 lines, pure/I-O-free by design): R1–R4 exposure
  tiers with hysteresis, confirmation days, staleness and coverage refusals, and a separate
  allocation solver (`regime_alloc.py`). Explicit invariants: unknown/stale inputs can never
  permit R1, never enable new buys, and never *independently* force selling. Modes:
  `observe | propose | enforce`, currently gated off.
- **Analytics** (26 modules): EOD snapshots, NAV/index, PRI/TRI benchmarks, breadth, tax lots,
  corporate actions, tradebook import, reconciliation, performance, ops jobs, daily runs.
- **Daily job** `python -m scripts.daily` — idempotent, one failure never stops the rest,
  `--check` reports without touching. **A missed session is not recoverable:** `kc.margins()`
  has no history and Zerodha flushes `/trades` nightly.
- **Options/strangle lab** — 4,347 lines, paper-only, three underlyings, its own calendar,
  sizing, adjustment and attribution. Gated `OPTIONS_ENABLED=false`.

## 5. It is deployed, and the deployment is thought through

Live at **`desk.modelbasket.in`**, on an AWS Lightsail box in Mumbai (`ap-south-1`).

- **Why a box at all:** Kite authorises order placement against an IP allowlist and a residential
  connection does not hold one — `103.238.14.245` became `49.43.34.118` inside a day. Separately,
  three collection days were lost to a sleeping laptop.
- **Sizing from measurement, not estimate:** desk 148 MB, Claude CLI 467 MB, Ubuntu ~150 MB →
  4 GB is the size to take, with a 2 GB swapfile and `MemoryMax` on the web unit so the kernel
  takes the CLI first rather than the process holding live `plan_id`s.
- **Security:** binds `127.0.0.1` only; Caddy is the only public-bound process; host allowlist
  against DNS rebinding; Origin required on POST/PUT/DELETE against CSRF; `DESK_PASSWORD` basic
  auth in constant time; API docs off; credentials forced to `0600` at startup; fail2ban jail on
  failed logins; SSH keys only.
- **Backups:** `momentum-backup.timer` at 19:15, *after* the 18:30 collection. A live SQLite file
  must not be `cp`'d — the script uses sqlite's own backup API, reopens the copy, integrity-checks
  it and counts the rows that matter; a run that does not say `ok` exits non-zero.
- **Why SQLite, answered with a measurement:** 5.7 MB, ~42,000 rows, one host. Five processes ×
  150 transactions = **750 writes in 0.2 s, zero lock errors** against a workload needing a few
  hundred a day. Postgres would add a service to patch, monitor and back up in exchange for
  nothing measurable. *Revisit when: a second application host, writers on a different machine,
  many gigabytes, or a need for replication.* **The merge makes three of those four true.**
- **Three ways TLS looked installed and did nothing** are written up — root-owned Caddy log,
  bare-epoch timestamps no fail2ban date template parses, and `backend = systemd` making
  `logpath` silently ignored. Each found by testing behaviour, not status.

## 6. Live data — the thing that cannot be rebuilt

| Table | Rows | Rebuildable |
|---|---:|---|
| `trades` | 8,198 | No — Zerodha flushes `/trades` nightly |
| `fills` | 9,262 | No — only a partial Console export exists |
| `index_series` | 21,276 | Yes, re-fetchable from Kite |
| `benchmark` | 3,328 | Yes |
| `rebalance_orders` | 133 | **No** — what was decided, and what happened |
| `rebalance_versions` | 13 | **No** |
| `regime_evaluations` / `regime_exposure` | 13 / 13 | **No** |
| `snapshots` (EOD) | 8 | **No** — `kc.margins()` has no history |
| `breadth_readings` | 1 | **No** — needs a scan you no longer have |

~10 MB in total, and it is the evidence the strategy is being judged on. Any merge step that
touches this database is the highest-risk step in the plan.

## 7. The gap this product has

Its brain is fed by hand, weekly, from a third party's website. That is:

- a **manual step** in an otherwise automated loop;
- a **single point of failure** you do not control (their schema, their uptime, their pricing,
  their terms of use);
- **unversioned** — you cannot re-run last month's plan because you cannot reproduce last
  month's input;
- **unbacktestable** — the desk cannot replay a decision it has no history of inputs for;
- and it is **the exact thing Decile builds.**
