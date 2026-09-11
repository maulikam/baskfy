# FIRST-LIVE-MORNING — the first real session of TWT-1

**Written 11 September 2026, before the code it describes.** Module TW10, the runbook half.

Maulik decided on 11 Sep 2026 that this sleeve has **no paper phase**: it goes live at ₹25,00,000
the day he flips the flag himself. A runbook written the night before a first live session is
written by somebody tired. This one is written while the reasoning is fresh, and the modules that
follow it (TW4–TW10) are built to match it rather than the other way round.

`docs/twt/02` §3 makes this document a **condition of the flag**, not a nicety: "the first morning
of a sleeve whose exit is a ratchet is the morning most likely to end with an unprotected line."

---

## 0. What this document is, and what it is not

**It is** the sequence Maulik follows on the first live morning, every step with the command that
runs it, what the expected output looks like, and what to do when it is not that.

**It is not** an instruction to any agent. Three things in here are Maulik's hands and nobody
else's — the **Kite login**, the **flag**, and the **sleeve's capital** — and the root `CLAUDE.md`
safety rails say so in the same words. No agent sets a flag, sets a capital, confirms a line, or
places an order. If you are an agent reading this file: it is documentation of a human procedure.
Run nothing in it.

### The legend, and why half this file is marked

Most of this sleeve does not exist yet. TW0 is green, TW1 and TW3 are in flight, TW4–TW10 are not
started. **A runbook that quietly describes software that was never written is worse than none**,
so every command below carries one of two tags:

| Tag | Meaning |
|---|---|
| `[REAL]` | Exists today, on this tree or on the box. You can run it now. |
| `[NOT YET REAL — TWn]` | Does not exist. Module `TWn` owns building it, **to this spelling**. Running it today gets you "No rule to make target" or a 404. |
| `[BUILT — TWn, never run live]` | The command exists and is tested, and **it has never run against a broker**. Added 11 Sep 2026 when TW6 landed: a runbook that still said "not yet real" about a route that answers is the stale half of the same disagreement `/CLAUDE.md` warns about. |

§11 is the full inventory of what is not real, so the later modules can be built to match.

---

## 1. Set these three shell variables once

Everything below is one line because of them. On a phone, set them once in your terminal app's
startup file so the stop command in §7 is never more than a paste.

```bash
export DESK=https://desk.staging.baskfy.com          # the Phase A box's operator console
export WEB=https://staging.baskfy.com                # the web app, where the Kite login happens
export DESK_PW='<the value of BASKFY_DESK_PASSWORD on the box>'
```

`[REAL]` All three resolve today: `desk.staging.baskfy.com` and `staging.baskfy.com` are both
`3.108.148.38`. `desk.modelbasket.in` (`65.0.226.77`) is the **old Lightsail console** and is not
this sleeve — do not use it here.

`BASKFY_DESK_PASSWORD` lives in `/opt/baskfy/.env.staging.compose` on the box. Read it in an SSM
shell, never through `box.sh` (SSM command parameters are retained in CloudTrail). If you do not
have it to hand, get it before the morning, not during it.

**Two details that will cost you a morning if you learn them at 09:15:**

1. The desk's basic auth ignores the username and compares only the password
   (`app/core/websec.py::_basic_ok`), so `-u desk:$DESK_PW` is right and so is any other username.
2. **Every POST to the desk needs an `Origin` header.** `DeskSecurity` refuses a state-changing
   request that cannot say where it came from — 403, with a sentence about forms on other sites.
   A browser sends it; `curl` does not unless you say so. Every POST below carries
   `-H "Origin: $DESK"`. Leave it out and you get a 403 that looks like a permissions problem and
   is not.

---

## 2. The evening before

### 2.1 The tree is green and the gate's other conditions hold

`docs/twt/02` §3 lists six. Five of them are checkable and four of those are checkable here:

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make test && make lint
```
```bash
cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && .venv/bin/python -m pytest
```

`[REAL]` Expect both suites green — the desk's at 1,852 passed or better, and the weekly book,
R1–R4, the swing book and VBT-1 all still running. **If either suite is red, stop.** A red tree is
not a "known failure to work around" on the morning you first put ₹25 lakh behind a new sleeve.

```bash
cd /Users/maulikdave/Documents/projects/baskfy && uv run python tools/twt/drill.py --database-url postgresql+asyncpg://.../a_scratch_database
```

`[NOT YET REAL — TW10]` The full `DRY_RUN` drill: evening plan, morning plan, confirm every line,
a fill, a GTT, a ratchet, the sweep. It must end with **`0 orders reached a broker.`** Anything
else and the flag does not get flipped.

### 2.2 The schema is migrated and the sleeve has been seeded

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make migrate
```

`[REAL]` — but `0041_twt` is TW3's and is in flight. Expect alembic to report head at `0041_twt`.
The seed writes one `tw_config` row per user with **`sleeve_capital_inr = 0`**. That zero is
deliberate: a sleeve with no money cannot trade by accident, and every signal is skipped
`NO_SLEEVE_CAPITAL` until you change it.

### 2.3 The night's detection ran

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make twt DATE=<the last published session>
```

`[NOT YET REAL — TW4]` The nightly chain's `COMPUTE_TWT` step runs this itself after
`COMPUTE_VBT`, and Beat retries it at 21:00 IST. Run it by hand only if the chain did not.

Expect a funnel, in these words: universe → with a bar today → clearing the ₹30 close and the
10,000-share volume average → holding the tight state → **first day of the state after five
sessions out** = the signals. Plus one `tw_breadth_daily` row.

* **0 signals is a normal night.** This book enters about eighteen times a year; most nights are
  empty and about fifty names merely *hold* the state without entering it.
* **0 signals AND no breadth row means it did not run.** Those are different answers and the
  funnel tells them apart. Check the worker:
  `AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml logs --since 2h worker | grep -i twt'` `[REAL]`
* **Fewer tight names than Chartink's live scan is expected and is not a bug.** `NEEDS-MAULIK.md`
  T4: the plant reproduces 64.9 % of Chartink's stock-days, and 832 of the misses are days on
  which Baskfy has no bar at all. Both readings are right about their own data.

### 2.4 The evening plan exists and you have read it

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make twt-plan DATE=<the same session>
```

`[BUILT — TW6, never run live]` It places nothing. Expect `entries=N exits=0 arms=0 ratchets=0 gate=OPEN|SHUT`
and every line `PROPOSED`. The `TWT_EVENING` email carries the same thing.

**On the very first morning there are no exit lines**, because the sleeve holds nothing. `ARM_GTT`
and `RAISE_GTT_STOP` start appearing the evening after your first fill.

---

## 3. The first morning, in order

The whole thing is about forty minutes, of which thirty are waiting.

| Time (IST) | Step | § |
|---|---|---|
| by 08:45 | The Kite login | 3.1 |
| 08:50 | Restart the desk so it picks the token up | 3.2 |
| 09:00 | `/status` says `authed: true` | 3.3 |
| 09:00 | The flag — **your hand** | 3.4 |
| 09:02 | The capital — **your keystroke** | 3.5 |
| 09:05 | Build the morning plan | 4 |
| 09:05–09:14 | **Read the plan. This is the step that matters.** | 5 |
| 09:15 | Confirm — exits first, then entries | 6 |
| 09:20 | **The GTT check** | 8 |
| 15:15 | **The sweep, and the GTT check again** | 8 |

### 3.1 The Kite login, before 09:00

Kite's access tokens die around **06:00 IST** every day and cannot be refreshed. That is
structural, not a bug (`NEEDS-MAULIK.md` item 3, and T1 for this sleeve). **Nothing in this sleeve
works without it**: no order, no GTT, no ratchet.

At 08:45 on a weekday, if no usable token is stored, one email arrives — subject "Kite login
needed before 09:15" — carrying a single login link. **Tap it, log in on Zerodha's page, done.**
A link is good for 30 minutes and can be used once; a second and last one follows at 09:05.

No email? Log in by hand at `$WEB` → Portfolio → connect Zerodha. `[REAL]`

To force the nudge yourself:

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_worker.tasks.celery_tasks import kite_login_nudge_task as t; print(t('first'))"
```

`[REAL]`

**This sleeve makes the login matter more than the other two do.** TWT-1's exit *is* a ratcheting
GTT. On a rising book of ten lines most sessions carry `RAISE_GTT_STOP` lines, and a morning
nobody logs in for is a morning on which every one of those stops stays where it was. That is not
a loss — the old stop is still resting, the position is still protected — but a week of it means
the trail is a week behind the highs.

### 3.2 The token sync, and the desk restart

The desk reads its Kite client **once, at construction** (Q-SW13-1). A login after the desk
started does not reach it until it restarts.

```bash
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml restart desk'
```

`[REAL]` Expect the container name and `Started`. Give it twenty seconds.

If you are working from the laptop as well and want the same token locally:

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make token-sync TARGET=momentum-desk
```

`[REAL]` It writes **both** local encrypted stores — the desk's and the pipeline's — and verifies
with one `profile()` call. **The token is never printed.** Re-run it after *any* Kite login, not
just the first of the day: minting a new token invalidates the previous one, so a second login on
the box silently kills the copy on the laptop.

### 3.3 Prove the desk is awake, from the phone

```bash
curl -fsS $DESK/status
```

`[REAL]` `/status` is the one route that answers without the password.

Expect `"authed": true` and `"dry_run": false`.

* `"authed": false` → the login did not land, or the desk did not restart. Back to 3.1.
* `"dry_run": true` → the desk is still simulating. That is a **safe** state, not a broken one:
  go live only when you mean to (3.4). A TWT order is real only when the desk's `DRY_RUN` is off
  **and** the TWT execution flag is on — either one being wrong means the morning is a rehearsal.
* Connection refused or 502 → the desk container is down.
  `box.sh '... ps'`, then `... up -d desk`.

### 3.4 The flag — your hand, and only yours

`docs/twt/02` §3 condition 5, and `NEEDS-MAULIK.md` T2. **The run never sets this flag. No agent
touches it. The swing sleeve's 5 Sep delegation is about the swing flag and does not extend
here.** If you ever want that delegation for this sleeve you write one line in `NEEDS-MAULIK.md`
§ TWT and an agent records it in `DECISIONS-TW.md` before acting on it.

The key is **`BASKFY_TWT_EXECUTION_ENABLED`**. It is created by TW3 with the value `false`, in two
files on the box, and a flag is read **once per process at startup** — never from a form, never
from a route. So changing it is a file edit plus a restart, by design: the friction is the point.

**In an SSM shell on the box** (not through `box.sh` — you are editing a file, and SSM command
parameters are retained in CloudTrail):

1. Open `/opt/baskfy/.env.staging.compose` — this is what the **desk** reads. Find the line whose
   key is `BASKFY_TWT_EXECUTION_ENABLED` and change its value from `false` to the opposite.
2. Open `/opt/baskfy/.env.staging` — this is what the **api, worker and beat** read. Same key,
   same change.
3. While you are in the first file, confirm `BASKFY_DESK_DRY_RUN` is `false`. **The TWT flag alone
   changes nothing while the desk is in dry run.** This is the thing the swing book's first live
   morning found: *"a live flip is two files and three lines, not one."*
   Note what else that line does — `BASKFY_DESK_DRY_RUN=false` takes the **weekly book** out of
   dry run on the same desk. Its Friday `/execute` still needs your confirm click, but it is live
   too from that moment. Know that before you type it.

Then:

```bash
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml up -d desk worker beat'
```

`[REAL]`

```bash
curl -fsS $DESK/status
```

`[REAL]` Expect `"dry_run": false`. Record the date under `NEEDS-MAULIK.md` § TWT T2.

**There is no auto-execute flag for this sleeve and none is ever added.** Non-negotiable 1's named
exception belongs to the *swing* sleeve alone. TW10's property test greps both trees for
`BASKFY_TWT_AUTO`-anything and asserts nothing is found. A TWT order exists only because you
pressed Confirm on an unexpired plan.

### 3.5 The capital — your keystroke

`NEEDS-MAULIK.md` T3. Seeded at **0**; the run never writes it. A sleeve at ₹0 plans nothing and
every signal is skipped `NO_SLEEVE_CAPITAL` — **that is the intended behaviour and not a fault to
debug at 09:10.**

```bash
curl -fsS -X PATCH $WEB/api/v1/twt/config -H "Authorization: Bearer $BASKFY_TOKEN" -H 'Content-Type: application/json' -d '{"sleeve_capital_inr": "2500000.00"}'
```

`[NOT YET REAL — TW3/TW8]` The `PATCH /api/v1/vbt/config` shape, for `tw_config`. Or use the
settings form at `$WEB/me/twt`. The value travels as **a string**, the way the swing form sends
its decimals: `Number("2500000.00")` is a float and money is never a float (house rule 9).

Read it back before you trust it:

```bash
curl -fsS $WEB/api/v1/twt/config -H "Authorization: Bearer $BASKFY_TOKEN"
```

`[NOT YET REAL — TW3/TW8]` Expect `sleeve_capital_inr: 2500000.00`, `max_open_positions: 10`,
`max_position_pct: 12.50`, `stop_pct: 20.00`, `trail_pct: 20.00`,
**`first_live_entries_left: 10`**. Every write is audited into `tw_config_audit`.

**One thing worth your eye before you type it** (`NEEDS-MAULIK.md` T3): **no backtest in this
repository was produced at ₹25 lakh.** Every number in `docs/twt/01` was measured at ₹10 lakh. The
₹5 crore liquidity floor is the pack's answer to the size change; it is not a measurement at the
new size.

---

## 4. Build the morning plan — 09:05

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make twt-plan DATE=<the signal session> SOURCE=MORNING
```

`[BUILT — TW6, never run live]`

Or open `$DESK/twt` in a browser, which builds and shows the same thing. `[BUILT — TW6, never run live]`

**Three things about the clock, so you do not "fix" one of them:**

* `DATE` is the **last completed trading session** — yesterday, on a weekday morning. There is no
  "today" bar until today ends, and this sleeve's whole test is a weekly *close*. The root
  `CLAUDE.md`'s settled rule of 9 Sep 2026 governs and the freshness pill is correct when it reads
  yesterday at 09:05.
* The morning plan **re-sizes, it does not re-detect**. It reads the same session the evening plan
  read. If it detected anything new you would be trading a partial day.
* **A plan expires thirty minutes after it is issued.** Last night's cannot be confirmed at 09:15.
  Build it at 09:05 and it is good until 09:35; build it at 08:30 and it is dead before the open.
  This is why the step is here and not earlier.

---

## 5. What the plan must show before you confirm

**This is the step that matters most in this document.** Everything before it is plumbing and
everything after it is a click. At ₹25,00,000 over ten slots, with the half-size rule live, these
are the numbers.

### 5.1 The arithmetic you are checking against

| | |
|---|---|
| Sleeve capital | **₹25,00,000** |
| Slots | **10** → a full line is **₹2,50,000** |
| First ten live entries | **half size** → a line is **₹1,25,000** |
| Most new entries in one session | **3** → at most **₹3,75,000** of buying on any morning |
| Per-position ceiling | 12.50 % = ₹3,12,500 (above the slot; it binds only if equity drifts) |
| Smallest line worth taking | ₹10,000 — below it, `BELOW_MIN_TRADE_VALUE` and the line is skipped |
| The stop on every buy | **20 % below the fill** |
| The GTT's resting limit | **3 % under its trigger** (`TWT_GTT_LIMIT_FRACTION` 0.97) |
| The gate | OPEN when **more than 40.0 %** of the measured universe is above its 200-DMA |

### 5.2 Read the session strip first

* `mode` — **LIVE**, not `DRY_RUN`. `DRY_RUN` is a badge on this page, not a footnote.
* The flag states — TWT execution **on**, desk `dry_run` **false**.
* `gate` — **OPEN** or **SHUT**, and the `pct_above_dma` that decided it.
* The half-size counter — **"10 left"** on the first morning.
* The date — the last completed session.

### 5.3 Exits first, then entries

The page puts exits first for the reason the swing desk does: a morning that runs out of attention
should have armed the stops. On the **first** morning there are none.

**Entry lines.** Each `BUY_AT_OPEN` line shows symbol, quantity, value, the 20 % stop the fill
will be given, the cap that bound if one did, and the rank key (the signal session's own
turnover). Below them, **every skip with its reason in words** — `GATE_SHUT`, `ALREADY_HELD`,
`BELOW_LIQUIDITY_FLOOR`, `NO_BAR`, `LOCKED_UPPER_CIRCUIT`, `SESSION_CAP`, `SLOTS_FULL`,
`NO_SLEEVE_CAPITAL`, `BELOW_MIN_TRADE_VALUE`, `EXPOSURE_FULL`.

Read the skips. On this sleeve they are most of the page, and a skip you did not expect is
information.

### 5.4 What a correct first plan looks like

* **One, two or three `BUY_AT_OPEN` lines.** Never four — a fourth confirm of a morning is
  refused, not surprising.
* **Each line about ₹1,25,000**, and none above it. A line at ₹2,50,000 means the half-size
  multiplier did not apply.
* **Each stop about 80 % of the price the line quotes** — a ₹1,000 name gets a stop near ₹800.
* **No `SELL_AT_OPEN` line.** `exit_lines` cannot emit one; TW10 asserts it over a generated book.
* **No symbol the sleeve does not own** in any exit line. `tw_position` is the only source of
  truth for what this sleeve holds; a holding you bought elsewhere is invisible to it.
* The quantity is computed from the last price the plan had. **The order is a market order at the
  open**, so the value you actually spend moves with the gap, and the **stop is armed off the real
  fill**, not off this estimate.

### 5.5 Stop, and do not confirm, if any of these is true

Each one is a real failure mode, not a worry.

1. **The session strip says `DRY_RUN` and you meant live** — or says LIVE and you did not. Fix the
   state before you fix the plan.
2. **A line is larger than ₹1,25,000 while the counter says entries remain.** Half size is applied
   *at plan time* precisely so the line shown is the line sent. If the page disagrees with the
   counter, one of them is lying.
3. **More than three `BUY_AT_OPEN` lines**, or entries totalling more than ₹3,75,000.
4. **The gate reads SHUT and there are entry lines anyway.** A SHUT gate skips every entry
   `GATE_SHUT`. Entries under a SHUT gate is a wiring fault.
5. **A stop that is not below the entry**, or one further than 25 % or closer than 15 %.
   `STOP_NOT_BELOW_ENTRY` should have refused the first; the band for this sleeve is 0.5–30 %
   (`TWT_STOP_BAND`) and 20 % is the only value the rules produce.
6. **A `RAISE_GTT_STOP` whose new trigger is at or below the old one.** *A stop never falls.*
   Three separate layers refuse it and the page should never show it. If it does, something is
   wrong upstream of all three.
7. **A `RAISE_GTT_STOP` at or above the last traded price.** It would fire the moment it was
   armed, selling the position at the next tick for no reason.
8. **A line whose note says `TURNOVER_CAP` bound.** At ₹25,00,000 over ten slots this is
   arithmetically impossible: the cap is 1 % of a name's 20-session average turnover, the
   liquidity floor is ₹5 crore, so the smallest cap any planned name can have is ₹5,00,000 —
   twice a full line and four times a half one. If you see it, either the capital is not what you
   think it is or the floor is not what the config says.
9. **A `TWT_ADJUSTMENT_RESET` note on a held name.** A split or a bonus has moved the ground under
   a stop that has been resting for months, and the resting GTT at the exchange is now quoting a
   pre-split price on a post-split instrument. **A person has to look at it.** The sleeve will
   never lower a stop on its own to "fix" this — it refuses and raises the alert instead. Handle
   it by hand (§9) before you confirm anything else on that name.
10. **The plan's date is today.** It should be the last completed session.
11. **An SGB, a G-sec, or anything on `EXCLUDED_SYMBOLS`.** The gateway blocks these at its lowest
    layer, so you should never see one; seeing one means a filter did not run.
12. **Anything you do not understand.** The sleeve enters about eighteen times a year. Missing one
    costs a fraction of a year's trades; confirming one you did not read can cost more.

**Stopping is free.** Nothing below step 6 has happened yet; the plan simply expires in thirty
minutes and the morning ends with no position and no order.

---

## 6. Confirm — 09:15

One click, one line. **There is no "confirm all" and there never will be.**

Exits first — `ARM_GTT`, then `RAISE_GTT_STOP` — then entries.

From the desk page, press Confirm on the row. From a terminal:

```bash
curl -fsS -X POST $DESK/twt/execute -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true -d plan_id=<the plan id> -d line_id=<the line id>
```

`[BUILT — TW6, never run live]`

Three things are required and each is a refusal, not an error:

* **`confirm=true`** — a missing or false confirm is a **400**.
* **An unexpired `plan_id`** — older than thirty minutes is a **410**. Rebuild (§4) and re-read
  (§5); do not reach for a way around the expiry.
* **A line still `PROPOSED`** — a second confirm of the same line is refused by the idempotency
  key `plan_id:symbol`, so a re-posted plan cannot double-send.

**What happens inside the confirm, so an outcome you did not expect is legible.** Under a row lock
on the day's `tw_session` the desk re-reads the book and **re-sizes the line against the same
caps**. A line that fits goes as planned. One that only the exposure ceiling refuses is **shrunk
to the headroom** — never grown past what the page showed. One the rules cannot line at all comes
back `BLOCKED` with the skip code leading the reason, marked `REJECTED`, never sent. That is the
rule that stops a stale page becoming a wrong order.

Expected outcomes:

| Outcome | What it means |
|---|---|
| `FILLED` | The order filled and **a GTT was armed in the same request.** This is the good one. |
| `SIMULATED` | The desk is still in dry run, or the flag is off. Nothing reached a broker. Go back to §3.4. |
| `BLOCKED (SESSION_CAP)` | A fourth entry this session. Correct behaviour. |
| `BLOCKED (EXPOSURE_FULL)` | The sleeve's own cash, not the account's. Correct behaviour. |
| `BLOCKED` naming the position **NAKED** | The ratchet cancelled the old stop and could not arm the new one. **Go to §9 now.** |
| A 421 | The desk answered to a host it does not recognise. Use `$DESK` exactly as in §1. |
| A 403 with a sentence about forms on other sites | You left out `-H "Origin: $DESK"`. |

**Every fill arms a GTT in the same request as the fill it belongs to.** That is non-negotiable 4
and, on this sleeve, it is also the fill-day rule: the backtest's `stop_day0` — a name whose entry
session's own low is 20 % under its open — is *only* modelled in the live book by that
same-session GTT. A fill without one has no fill-day protection at all.

---

## 7. How to stop the sleeve — one command

**This is the most important line in this file.** One line, runnable from a phone, with nothing
else set up first except §1's three variables.

```bash
curl -fsS -X POST $DESK/twt/halt -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

`[BUILT — TW6, never run live]` Built to this spelling, and a test pins each of the four behaviours
below — the third one twice, through the mounted route as well as directly.

**What it must do, and this is the specification TW6 builds to:**

1. Write `tw_config.sleeve_capital_inr = 0`, audited into `tw_config_audit` with the previous
   value, so it can be restored by reading the audit row rather than by remembering.
2. Expire every unexpired `tw_plan`, so no `plan_id` in anyone's browser can still be confirmed.
3. **Leave every resting GTT exactly where it is, and keep `ARM_GTT` and `RAISE_GTT_STOP`
   working.** Stopping the sleeve must never remove protection. A halted sleeve is a sleeve that
   cannot *buy*; every position it already holds keeps its stop and keeps ratcheting.
4. Return the previous capital in its response, so the reply itself is the record.

Expect `{"halted": true, "sleeve_capital_inr_before": "2500000.00", "plans_expired": N}`.

At ₹0, every signal is skipped `NO_SLEEVE_CAPITAL` and every confirm of a `BUY_AT_OPEN` is
`BLOCKED` — because the confirm re-sizes through the same rules the plan did. To restore, set the
capital again (§3.5).

### If the halt route is not there yet, or does not answer

Three fallbacks, in order of speed, all of them real today:

**a. Set the capital to zero directly.** Same effect as the halt, without the plan expiry:

```bash
curl -fsS -X PATCH $WEB/api/v1/twt/config -H "Authorization: Bearer $BASKFY_TOKEN" -H 'Content-Type: application/json' -d '{"sleeve_capital_inr": "0.00"}'
```

`[NOT YET REAL — TW3/TW8]`

**b. Put the desk back in dry run and restart it.** Needs a laptop and AWS credentials, so it is
minutes not seconds — but it stops both this sleeve and everything else on that desk:

```bash
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml stop desk'
```

`[REAL]` Then `curl -fsS $DESK/status` returns nothing — the vhost answers 502 — which is itself
the confirmation.

**c. Do nothing.** This is worth saying out loud because it is true and because at 09:20 it is
easy to forget: **a TWT order exists only because you pressed Confirm on an unexpired plan.** No
job, no scheduler and no flag in this sleeve can place one. If you simply close the page, the plan
expires in thirty minutes and the sleeve does nothing at all for the rest of the day. The ratchet
is a plan line a person confirms, never a background job that talks to the broker.

**What none of these does — deliberately.** Nothing here cancels a resting GTT, sells a position,
or touches the weekly book, the swing book or VBT-1. Stopping a sleeve means stopping it from
opening new risk. Closing a position is a separate, manual decision, taken in the Kite app with
your eyes on the chart.

---

## 8. The GTT check — 09:20, and again at 15:15

A live line with no stop is the one thing that can actually hurt this sleeve. Check twice.

### 09:20 — five minutes after the last confirm

```bash
curl -fsS $DESK/twt/data -u desk:$DESK_PW | python3 -m json.tool
```

`[BUILT — TW6, never run live]` Or just read the book panel on `$DESK/twt`.

**Every `OPEN` position with `quantity_open > 0` must have a non-null `gtt_id`.** Count them:
positions in the book = GTT ids in the book. There is no acceptable difference.

Cross-check against the broker, which is the only opinion that matters:

```
Kite app → Orders → GTT
```

`[REAL]` One GTT per open TWT line, SELL, CNC, trigger about 20 % under your fill, limit about
3 % under the trigger. If Baskfy says a GTT exists and Kite does not show it, **believe Kite** and
go to §9.

The desk page's **15:15 strip** carries a red band naming every open line without a resting GTT,
and it stays until the sweep is clean.

### 15:15 — the sweep

```bash
curl -fsS -X POST $DESK/twt/sweep -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

`[BUILT — TW6 route, TW7 tool; never run live]` **The desk's clock does not run it yet** — `app/swing_clock.py` has
no TWT entry — so at 15:15 it runs because you ran it. This is how you run
it by hand or confirm it ran. It is idempotent and keyed on the day, so running it twice is safe.

Or from the laptop:

```bash
cd /Users/maulikdave/Documents/projects/baskfy && uv run python tools/twt/sweep.py --date <today>
```

`[NOT YET REAL — TW7]`

Expect `naked: 0`. The sweep re-arms anything it finds naked and raises
**`TWT_GTT_MISSING_AT_1515`** for anything it could not fix. **`TWT_POSITION_NAKED` fires on any
naked line at any time of day**, not only at 15:15.

**Do not leave the desk for the evening on a non-zero count.** The whole of §9 is about that
number.

---

## 9. When a GTT is missing

The failure that leaves a live line with no stop. It has exactly three causes and they want
different things.

### 9.1 How it happens

1. **A fill without an arm.** The buy filled and the GTT call failed in the same request. Rare —
   the arm is in the same request precisely so this is one failure and not two.
2. **A ratchet that cancelled and did not re-arm.** The `RAISE_GTT_STOP` path is
   delete-and-replace: cancel the resting trigger, arm the new one. When the cancel succeeds and
   the arm fails, the desk records the intent, nulls the `gtt_id`, and returns `BLOCKED` naming
   the position **NAKED**. *This is the most likely one on this sleeve*, because the ratchet runs
   on up to ten lines a session, for months. (When the **cancel** fails, the **old stop is still
   resting** and nothing was placed. That is not naked — that is a stop one session behind, which
   is a nuisance and not a risk.)
3. **A corporate action.** `TWT_ADJUSTMENT_RESET`: the resting GTT is quoting a pre-split price on
   a post-split instrument. The sleeve refuses to lower a stop to "fix" this and raises the alert
   instead — cancelling a resting stop to arm a lower one is the one thing it must never do on its
   own, and a split is the one event that would make it look correct.

### 9.2 What to do, in order — and do it now, not after lunch

**Step 1 — re-arm from the desk.** One position, one button, nothing else.

```bash
curl -fsS -X POST $DESK/twt/rearm -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true -d position_id=<id>
```

`[BUILT — TW6, never run live]` The swing book's `rearm_gtt` path, reused. Expect a new `gtt_id` and the
red band clearing. Re-run §8's check.

**Step 2 — if the re-arm is refused, read the refusal.** It is almost always one of:

* *the trigger is at or above the last price* — the name has fallen to or below its own stop while
  you were not looking. **The GTT is not the answer any more.** Decide whether to sell, and if you
  sell, sell by hand in the Kite app. A GTT that would fire on the next tick is not protection,
  it is a market order with extra steps.
* *an untouchable instrument* — SGB, G-sec, `EXCLUDED_SYMBOLS`. The sleeve should never have
  bought it. Close it by hand and record how it got in.
* *the kill switch is live* — the account's daily-loss cap has tripped. Note that the gateway
  **still arms protective stops** with the kill switch live, and still refuses to *remove* one, so
  a refusal here means something else; read the message.

**Step 3 — arm the stop by hand, in the Kite app.** This is a human placing a protective order and
it is always allowed.

```
Kite → the holding → GTT → Single → SELL → CNC
  Trigger price : the trigger the plan shows (about 20 % under your fill, or the ratcheted level)
  Limit price   : the trigger × 0.97
  Quantity      : the full open quantity of that line
```

`[REAL]` Then tell Baskfy, so the book and the exchange agree:

```bash
curl -fsS -X POST $DESK/twt/reconcile -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

`[BUILT — TW6, never run live]` It reads the broker's GTT list and attaches what it finds to the
position. **It has never met a real GTT list** — the field names are Kite's documented ones and no
live response has been read; if one is spelled differently the reconcile attaches nothing and the
page keeps calling a protected line naked, which is the right way round for that to fail.
Until it runs, the page will keep calling a protected line naked — which is the right way round
for a discrepancy to fail.

**Step 4 — if you cannot arm a stop at all, close the line.** A position this sleeve cannot
protect is not a position this sleeve should hold. Sell it in the Kite app and record why in the
position's note. The strategy's edge comes from giving names room *behind a stop*; the same room
without a stop is a different strategy and not one anybody measured.

### 9.3 The one thing never to do

**Never cancel a resting stop in order to "tidy up" a discrepancy.** If Baskfy and Kite disagree
about whether a GTT exists, the safe direction is always *more* protection: leave the resting one,
add nothing that would double-sell, and reconcile. A stop that quietly disappeared is the failure
nobody notices until it does not fire.

---

## 10. The daily routine after the first morning

Four steps and about ten minutes. Most mornings have no entries at all — this book enters about
eighteen times a year — so most mornings are steps 1, 2 and 4, and step 2 is the whole job.

### 1. Log in — by 09:00

Tap the 08:45 email. Then, because the desk caches its Kite client:

```bash
AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml restart desk'
```
```bash
curl -fsS $DESK/status
```

`[REAL]` `"authed": true`, `"dry_run": false`.

### 2. The ratchet plan — 09:05

```bash
cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make twt-plan DATE=<the last completed session> SOURCE=MORNING
```

`[BUILT — TW6, never run live]`

**On a normal day this plan is nothing but ratchets.** The arithmetic was done last night: the
nightly job raised each position's `high_since` with the session's high and computed
`next_trigger` per the 20 % trail, tick-floored and clamped just under the close. The morning plan
is that number, on a page, waiting for a click.

Read three things on each `RAISE_GTT_STOP` line:

* **the new trigger is above the old one** — always, on every line, without exception;
* **`high_since`** — it should equal the highest high since your fill, including the fill day's own
  high;
* **the distance to the last price** — a trigger sitting a hair under the last price is the clamp
  doing its job, and it is also a name about to be stopped out.

A line carrying `TWT_ADJUSTMENT_RESET` is §9.1 case 3: handle it by hand.

### 3. Confirm

Exits first, entries after. One click per line. `confirm=true`, an unexpired `plan_id`, a
`PROPOSED` line. Everything in §5.5 still applies every morning, not just the first.

**While `first_live_entries_left > 0`, entries are still half size** (§11.1).

### 4. The sweep — 15:15

```bash
curl -fsS -X POST $DESK/twt/sweep -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true
```

`[BUILT — TW6 route, TW7 tool; never run live]` Expect `naked: 0`. **The desk's clock does not do it yet**; this is
the check, and on a day you ratcheted ten lines it is the check that matters.

### The evening takes care of itself

21:00 detect, 21:05 the evening plan and the `TWT_EVENING` email — signals, plan preview, the
gate, naked GTTs, ratchets due. **Read that email.** It is the cheapest way to know that tomorrow
morning has something in it, and the only thing that tells you a stop did not get raised.

### What to watch over the first year

The ratchet is the strategy: **137 of the research's 164 exits were the trailing stop.** If your
exits are not overwhelmingly trailing stops, something is different about the live book and it is
worth knowing early. And `trail_pct` is bounded **below** at 18 % for a measured reason — tightening
it from 20 % to 15 % took the study's CAGR from 20.9 % to 9.6 % and its drawdown from −24.7 % to
−43 %. The instinct to tighten a trail after a give-back is the single most expensive instinct
this strategy can provoke; the 20 % give-back is routine and is how the method works.

---

## 11. What is not real yet — the inventory

Every command tagged `[NOT YET REAL]` above, in one table, so the modules that follow build to
these exact spellings.

| Command / route | Owner | Note |
|---|---|---|
| `make twt DATE=…` | TW4 | Named in `06`. The nightly chain runs `COMPUTE_TWT` itself. |
| `make twt-plan DATE=… [SOURCE=EVENING\|MORNING]` | **TW6** | **BUILT.** `baskfy_worker.twt_cli --plan`. Writes `tw_plan`; places nothing. |
| `make twt-backtest` | TW9 | Named in `06`. |
| `GET $DESK/twt`, `GET $DESK/twt/data` | TW6/TW8 | **BUILT** (TW6's shape; TW8's polish went to the web page). |
| `POST $DESK/twt/execute` | TW6 | **BUILT.** `04` §10.4. Needs `Origin`. 400 / 404 / 410 / 409, and a `SELL_AT_OPEN` is a 400 (TW6.4). |
| `POST $DESK/twt/rearm` | **TW6** | **BUILT**, and exported as the callable TW7's sweep injects (`twt_execute.rearm_callable`). |
| `POST $DESK/twt/sweep` | **TW6/TW7** | **BUILT.** Idempotent, keyed on the day. **No clock runs it yet.** |
| `POST $DESK/twt/reconcile` | **TW6** | **BUILT.** Never met a real GTT list — see §9.2 step 3. |
| **`POST $DESK/twt/halt`** | **TW6** | **BUILT to §7's four behaviours.** The third — protection is never removed — is pinned twice: directly, and through the mounted route. |
| `GET` / `PATCH $WEB/api/v1/twt/config` | TW3/TW8 | Mirrors `/api/v1/vbt/config`. `sleeve_capital_inr` travels as a string. |
| `$WEB/me/twt` settings form | TW8 | The bounded settings of `02` only. |
| `tools/twt/sweep.py` | TW7 | Named in `06`. |
| `tools/twt/drill.py` | TW10 | Named in `06`. Must end `0 orders reached a broker.` |
| `BASKFY_TWT_EXECUTION_ENABLED` | TW3 | Created **false**, in both env files on the box. |
| `TWT_EVENING`, `TWT_POSITION_NAKED`, `TWT_GTT_MISSING_AT_1515`, `TWT_ADJUSTMENT_RESET` | TW4/TW6/TW7 | **BUILT** in `baskfy_worker.alerts.AlertName`, one runbook behind all four: `decile-blueprint/docs/runbooks/09-twt-morning.md`. |

**Real today:** `make test`, `make lint`, `make migrate`, `make token-sync`, the Kite login and its
08:45 nudge, `tools/deploy/box.sh`, `curl $DESK/status`, the Kite app's GTT screen, and §7's
fallbacks (b) and (c).

### 11.1 Why the half-size rule counts entries and not sessions

`02` §3 condition 6 and `04` §6.4. The first **ten live entries** — not ten sessions — are sized at
0.5 × the slot, and the multiplier is applied **at plan time** so the line shown is the line sent.

The swing book and VBT-1 count sessions because they enter most days. **This book enters about
eighteen times a year.** A five-session allowance here would be exhausted by one quiet week in
which nothing triggered at all, and the discipline would have protected nothing. Ten entries is
most of a first year's cohort, which is what "prove it with less money first" was supposed to mean.

The counter lives in `tw_config.first_live_entries_left`, is decremented **once per filled entry by
the session that filled it**, and never by a request, never by a proposed line nobody confirmed,
never twice for the same fill. It is a counter in the sleeve, **not a flag** — there is nothing
about it anybody can switch off.

**A `DRY_RUN` plan is full size.** Half size is a live-money discipline; a paper plan that is not
the plan is not a rehearsal.

---

## 12. What this document does not authorise

Stated once, in the same words as the root `CLAUDE.md`, because a runbook is exactly the kind of
document from which somebody later quotes a line out of context.

* **No agent places an order.** Ever. Full stop.
* **No agent flips `BASKFY_TWT_EXECUTION_ENABLED`**, or any other flag, in any environment.
* **No agent sets `tw_config.sleeve_capital_inr`.**
* **No agent confirms a line on Maulik's behalf**, and nothing in §6, §9 or §10 is an instruction
  to do so.
* **No auto-execute flag exists for this sleeve and none is added.** Non-negotiable 1's named
  exception belongs to the swing sleeve alone, by Maulik's own hand, and an agent may not widen
  it, add a second one, or default any flag to true.
* **`DRY_RUN=true` is the default in every environment an agent creates.**

The three things in this file that are Maulik's hands are in `NEEDS-MAULIK.md` § TWT as **T1** (the
daily Kite login), **T2** (the flag) and **T3** (the capital). **T4** — the plant's missing
instrument-days — is the one that will make the live scan look thin and is not a bug.
