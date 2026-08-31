# Decision: should Baskfy's web app get an execute route?

**For Maulik. Unsigned as of 31 Aug 2026.** Nothing in this document has been built. It exists
because retiring `desk.modelbasket.in` and running everything from Baskfy runs into one rule that
an engineer is not allowed to decide, and five that are ordinary work. This is the one.

Every quotation below was read out of the file it names on 31 Aug 2026. Every status in
§4 was measured on 31 Aug 2026, not copied from `docs/00-merge-status.md`, which is from 22 Aug
and is stale in at least one important way (§4 says where).

---

## 1. What is being asked

You want the momentum desk switched off and the whole thing run from Baskfy. Five of the six
things that requires — the desk's data, its jobs, its pages, its numbers, its domain — are
ordinary engineering and are being worked as separate leaves. The sixth is not. The desk can
place an order; Baskfy cannot, on purpose. If Baskfy is to replace the desk rather than merely
inherit its screens, something in Baskfy has to be able to say "place these fifteen orders" — and
today nothing in Baskfy can, because four separate rules forbid it and a handful of tests enforce
them. This document sets out exactly which rules would have to be lifted, what stops being true
the moment they are, what must be green before anyone writes the code, and the alternative that
needs no rule lifted at all.

---

## 2. Exactly which rules must change

Four rules. All four have to go, or be narrowed; lifting three of them changes nothing, because
the fourth still refuses.

### 2.1 Non-negotiable #1 — "Never auto-execute"

`CLAUDE.md:47`, under "The desk's seven non-negotiables (survive verbatim, forever)":

> 1. **Never auto-execute.** Orders fire only from `POST /execute` with `confirm=true` and the
>    `plan_id` issued by `/analyze`; plans expire in 30 minutes. `DRY_RUN=true` must simulate end
>    to end.

**What it guarantees today.** There is exactly one `POST /execute` in this repository, at
`kite-momentum-rebalancer/app/main.py:515`, and it is the desk's. It refuses without
`confirm=true`, refuses an unknown `plan_id`, and refuses a plan more than 1800 seconds old. The
plans live in a Python dict in that one process (`main.py:132`), so a plan cannot outlive the
session that made it.

**Note carefully what non-negotiable #1 does *not* say.** It does not forbid a second execute
route. It says orders fire only from a confirmed, plan-gated, 30-minute-expiring `POST /execute`.
A Baskfy execute route built to exactly that shape would satisfy the letter of #1. What forbids it
is the *next* rule, which is about location rather than shape.

### 2.2 The standing stop — "web execute"

`CLAUDE.md:155`, in the list headed "**The only things that still stop work**":

> - Paid multi-tenant launch / Track B flag flips / web execute — C3, D7 amounts, and
>   non-negotiable #1. P4.1 schema and P4.3 gateway isolation are in; P4.2 two-token OAuth,
>   P4.10 RLS, and P4.11 load tests are not.

**What it guarantees today.** This is the sentence that makes web execute a thing an agent must
stop on rather than decide. It is in the same list as "Placing a live order — never, full stop"
and "Destroying or risking unrebuildable data with no verified backup path". It names its own
three conditions: counsel item C3, the D7 pricing amounts, and non-negotiable #1. It also records
that three of the multi-tenant safety pieces (P4.2, P4.10, P4.11) are not built.

### 2.3 The human-track paragraph — "The web app never gains an execute route"

`CLAUDE.md:27–34`, under "Human-track decisions (NOT for agents — never build against a guess)":

> D3 regulatory posture: **written 23 Aug 2026 as B** in `docs/DECISIONS-MERGE.md` §D3
> (⚠ UNREVIEWED). RA/empanelment filings remain counsel C1–C3. **Paid** multi-tenant launch
> still waits on C3; P4.1/P4.3 engineering started 24 Aug 2026. D7 pricing amounts, D10
> market-data display licensing remain human-track. Track B flags stay false. **The web app
> never gains an execute route.** `MERGE-PROMPTS.md` still documents the Phase-3 stop as
> history; D3 is no longer the engineering blocker.

**What it guarantees today.** That sentence is unconditional in its own text and sits in the
section explicitly reserved for you. It is the flat prohibition; §2.2 is the procedural one.

### 2.4 D3 posture B, clause 2

`docs/DECISIONS-MERGE.md` §D3, "Taken — Posture B", clause 2:

> 2. Orders fire only in the **user's own** broker account, after explicit confirm, through
>    `packages/execution` (desk non-negotiable #1). The **web app never gains an execute route**.

And the same commitment, compiled into code so it can be checked rather than remembered, at
`decile-blueprint/packages/core/src/baskfy_core/broker_connections.py:76–86`:

> ```
> BROKER_OAUTH_REVIEW: Final = BrokerOauthReview(
>     requirement=(
>         "Posture B (DECISIONS-MERGE.md §D3): publish baskets; user executes in their own "
>         "broker account after confirm. OAuth + encrypted holdings sync are allowed. "
>         "The web app still never places orders — packages/execution only."
>     ),
>     signed_off=True,
>     decision_reference="DECISIONS-MERGE.md §D3",
>     signed_off_on="2026-08-23",
>     signed_off_by="autonomy-charter (⚠ UNREVIEWED until Maulik clears)",
> )
> ```

**What it guarantees today.** Posture B is the whole regulatory story the product currently tells,
including to counsel. §D3 was written by an agent under the autonomy charter and is tagged
**⚠ UNREVIEWED** — you have not cleared it. Its own "Rejected" list contains, in as many words:
"Flipping OAuth *and* web execute together (would break non-negotiable #1)." OAuth was opened on
the explicit basis that execute stayed shut. Reopening execute reopens that bargain.

### 2.5 What the code and tests would have to lose

Not a rule, but the concrete inventory, so nobody discovers it mid-build. These are the guards
that would have to be deleted or narrowed:

| Where | What it asserts |
|---|---|
| `decile-blueprint/services/api/src/baskfy_api/app.py:566` | `# SC2: curated-basket catalog (/explore) + watchlist CRUD. No order/execute routes.` |
| `app.py:569` | `# SC3: invest/apply/exit plan previews only — synthetic desk_plan_id, never execution.` |
| `services/api/tests/test_baskets_readonly.py:97` `test_the_whole_api_has_no_order_route` | Walks the **entire** OpenAPI spec and fails if any mutating path contains `/order`, `execute`, `gtt`, `trade/place` or `place_order`. This is the one that actually catches a route added in a hurry. |
| `test_baskets_readonly.py:123` `test_the_kite_handoff_router_declares_no_mutating_verb` | The Publisher hand-off router must contain no POST at all, "because a POST here would mean this service submitting a basket on a user's behalf, which is the regulated activity the whole posture avoids". |
| `test_baskets_readonly.py:90` | The basket routers may not so much as import `baskfy_execution`, `OrderGateway`, `place_order` or `kiteconnect`. |
| `services/api/tests/test_api_portfolios_tree.py:1218` `test_the_router_declares_no_execute_route` | The portfolio router's source may not contain the string `/execute`. |
| `packages/core/tests/test_broker_connections.py:24` | The OAuth sign-off text must still say the web app never places orders. |
| `docs/COUNSEL-BRIEF.md` §7 | Tells counsel, in writing: "**The web app has no execute route.**" That brief would have to be re-issued. |

Measured today: `decile-blueprint/services/api/src/baskfy_api/routers/kite.py` has **0**
`@router.post` declarations, and the Baskfy API's 166 routes across 35 routers contain **no**
order, execute or GTT path.

---

## 3. What becomes true if you sign

In plain language, and honestly.

**What you get.** One place. You open Baskfy, look at the plan, press a button, and the orders go
— without a second app, a second login, a second box to keep alive, and without the Kite Publisher
form in between. The desk can then actually be switched off rather than kept breathing as the
thing that presses the button.

**What can go wrong afterwards that cannot go wrong today.** This is the part that matters, and it
is not about intentions. It is about what is reachable.

*Today, the button lives somewhere almost nothing can reach.* The desk's `/execute` sits behind
`DeskSecurity` (`kite-momentum-rebalancer/app/core/websec.py`): the request's `Host` header must be
on an allowlist that defaults to `127.0.0.1,localhost` — anything arriving under another name is
refused with a 421 before it routes; every state-changing method must carry an `Origin` or
`Referer` the app recognises; and an optional Basic password can be turned on. On top of that the
route needs a `plan_id` that is a fresh uuid4 held only in that process's memory, less than thirty
minutes old. There is no user model, no session cookie, no API key, no multi-tenancy — there is
one operator and one machine.

*Afterwards, the button lives in a public web application.* Baskfy's API is a 166-route
FastAPI service reached over the internet, with Google sign-in (`POST /auth/google`), refresh-token
rotation, CSRF machinery, and an `X-API-Key` header for programmatic callers. An execute route
placed on that surface is reachable by everything that can reach that surface. Concretely, each of
these becomes a way to fire real orders in a real account, and none of them is a way to do it
today:

- **A stolen or leaked session.** Anything that gets a valid session or refresh token — XSS in the
  web app, a stolen laptop, a phishing page, a browser extension — can now place orders, not just
  read a portfolio. The blast radius of every authentication bug in the product changes from
  "someone sees my holdings" to "someone trades my account".
- **An API key.** `Scope` in `packages/core/src/baskfy_core/api_keys.py:77` has exactly four
  members and its docstring says "**Every member is a read**" — `screens:read`, `factors:read`,
  `breadth:read`, `meta:read` — and the default for a new key is *all* of them, on the stated
  reasoning that every scope is a read so a narrower default buys no security. That reasoning
  stops holding the day a write exists. Either a write scope is added and every existing key's
  default has to be re-thought, or the execute route has to be deliberately excluded from key
  auth — a decision somebody must actually make and test.
- **A CSRF or CORS mistake.** The desk defends against this with a hostname allowlist that only
  works because the app answers to localhost. A public app cannot use that defence; it has to rely
  on CSRF tokens and CORS configuration being right on the one route where being wrong costs money.
- **A bug in someone else's route.** Today, `packages/execution` is not even importable from the
  basket and portfolio routers — a test enforces it. Once one router can reach the order path, that
  wall is down for the process; the remaining protection is code review.
- **Multi-tenancy, which is half-built.** `CLAUDE.md:155` records it plainly: P4.1 tenant columns
  and P4.3 gateway mismatch refusal are in; **P4.2** (two-token OAuth), **P4.10** (row-level
  security) and **P4.11** (per-user rate-limit load tests) are not. With a single user that gap is
  theoretical. With an execute route and a second user it is the gap between "the gateway refuses a
  cross-tenant plan" and "nothing at the database layer would have stopped the plan being built
  from someone else's holdings in the first place".
- **Automation drift.** The desk cannot auto-execute because a human must be in a browser session
  holding a thirty-minute plan. An execute route in a service that already runs Celery Beat jobs
  is one scheduled task away from auto-execution — not by decision, by convenience, six months
  from now, by whoever is fixing something else at the time. Non-negotiable #1's title is *Never
  auto-execute*; the thirty-minute plan expiry is the mechanism that makes it structurally true
  rather than a promise.

**What does not change even if you sign.** Law #2 stays: `packages/execution` remains the only
path to an order, so an execute route would be a thin, guarded caller of the gateway and nothing
else. The seven non-negotiables' substance — CNC-only, product gates default off, untouchable
instruments refused before any network call, `client_id = plan_id:symbol` idempotency, a journal
line per action — all survive, because they live in the gateway, not in the route. **This is the
strongest argument for signing:** the dangerous part of order placement is already built, already
tested, and would be reused rather than reinvented. What you would be adding is a door to it.

---

## 4. Preconditions — what must be green before the route is built

Signing this and building it are two different acts, and the second must wait on real numbers.
These are the preconditions. Each status below was measured on 31 Aug 2026.

### Precondition 1 — GTT lives inside the gateway (leaf 1.2.1)

**Status: ABSENT. Verified today.**
`decile-blueprint/packages/execution/src/baskfy_execution/gateway.py` is 150 lines and contains
**zero** occurrences of `gtt` or `GTT`, in any case. `OrderGateway` has exactly three methods:
`__init__`, `_journal`, `place`. The only GTT path in either tree is the desk's
`kite-momentum-rebalancer/app/kite_client.py:247` `place_gtt_stop`, which carries its own guard and
bypasses the gateway entirely.

**Why it blocks.** Non-negotiable #4 says every buy gets a GTT stop the same session, vol-scaled
8–12%. Non-negotiable #6 already carries this as its one documented exception ("the gateway has no
GTT method yet"). An execute route in Baskfy that placed buys would either place them with no stop
— breaking #4 outright — or reach around its own gateway into the desk's client to place one,
which breaks the "everything goes through the gateway" clause for the *new* code rather than
merely inheriting an old exception. Leaf 1.2.1's gate file calls this "the most dangerous gap".
All 7 of its gates are unchecked.

### Precondition 2 — a DRY_RUN drill end-to-end through Baskfy's own gateway (leaf 1.2.3)

**Status: NOT BUILT. Verified today.**
The gate's command is `uv run python -m baskfy_worker.tasks.desk --drill`. There is no `--drill` in
`decile-blueprint/services/worker/src/baskfy_worker/` — the string does not appear anywhere in the
worker source. All 6 of leaf 1.2.3's gates are unchecked, and it is declared as depending on 1.2.1.

**Why it blocks.** Non-negotiable #1's last clause is "`DRY_RUN=true` must simulate end to end".
Baskfy has never once produced a rebalance plan and driven it through its own gateway, not even in
simulation. Nobody should sign a live door onto a path that has not been walked with the lights on.
(`kite-momentum-rebalancer/.env` currently has `DRY_RUN=true` at line 5.)

### Precondition 3 — the numbers agree with the desk's (leaves 1.4.1, 1.4.2, 1.4.3)

This is three separate things and all three are open. **All 16 gates across the three leaves are
unchecked.**

**1.4.1 — the 271-row reference parity test.** The status page records "6,934/9,166 cells still
fail on window length" as of 22 Aug. Measured today, `uv run pytest
packages/core/tests/test_reference_parity.py -q` reports **44 passed, 2 skipped** — and the two
skipped tests are the ones that matter. `TestStep2FullRowReproduction` (line 700) and its companion
(line 743) skip with the reason: *"BASKFY_PARITY_BARS is not set … requires each instrument's
adjusted daily closes for the 248 trading days ending 2026-08-18. The bundle contains no price
history … Point BASKFY_PARITY_BARS at a Parquet file of real adjusted history to run it."*

**Do not read a green suite as parity.** The decisive comparison has not been executed since the
6,934 figure was recorded; it is neither confirmed nor cleared. The test's own docstring is blunt
that a previous version of this claim — "it runs" — was "**false**, and nobody found out because a
skipped test is never executed". Someone has to point that environment variable at real bars and
report the number before this precondition can be called anything.

**1.4.2 — the generated scan sees a smaller universe than the upload.** `docs/DECISIONS-MERGE.md`
§M13.1, measured on the 2026-08-18 corpus: the generated scan passes **223** symbols where the
upload passes **239**, and **fourteen of the sixteen** are rejected by `far_from_high` because an
unadjusted pre-split high makes a stock look 80% below a peak it never reached. §M13.1's own
sentence is the one to keep: "A desk that silently stopped seeing sixteen names would look exactly
like a desk that was working." I did not re-measure this today — it needs a live database and a
seeded corpus, and leaf 1.4.2 owns it. What I did verify: the flag is still safe.
`SCAN_SOURCE_DEFAULT` defaults to `"upload"` at `kite-momentum-rebalancer/app/config.py:90` and is
`upload` in `.env.example:163`, with a test asserting it (`tests/test_breadth_and_shadow.py:124`).

**1.4.3 — the shadow harness found four order deltas.** `docs/DECISIONS-MERGE.md` §M14.4, first
run 2026-08-18, four deltas out of fifteen orders: `AETHER BUY 40 → BUY 39`, `WELCORP BUY 35 → BUY
36`, `SHILPAMED BUY 81 → —`, `— → DIVISLAB BUY 7`. Two are one share; two are a substitution caused
by `SHILPAMED` stepping 778.75 → 384.95 overnight on 2025-10-03 unadjusted. The harness exists at
`kite-momentum-rebalancer/scripts/shadow_mode.py`; I did not re-run it (leaf 1.4.3 owns that).
§M14.4's own standard: "**A ±1 share difference is not green**, and a red week restarts the counter
at zero. Three green weeks and a rounding delta is a restart."

**Why these block.** An execute route is a machine for turning Baskfy's numbers into money. Until
Baskfy's numbers and the desk's numbers are the same numbers, the route would faithfully execute a
disagreement.

### Precondition 4 — the multi-tenant pieces that are named as missing

`CLAUDE.md:155` names them: **P4.2** two-token OAuth, **P4.10** row-level security, **P4.11**
per-user rate-limit load tests. All three are recorded as not done in `docs/DECISIONS-MERGE.md`
§P4.0's explicit do-not-do list, which also contains, in the same breath, "public signup, fee
collection, or web execute". If Baskfy ever has a second user, these are not optional.

### Precondition 5 — counsel

§5 below. It is its own thing and it does not depend on any of the above.

**Summary: five preconditions, zero currently green.** Two are absent code, one is unmeasured, one
is measured-and-red, one is a legal answer nobody has.

---

## 5. The counsel dependency

**C3 is:** *"Whether posture B needs **RA registration** and/or **Kite-Publisher / empanelment**"*
— `docs/COUNSEL-BRIEF.md` §5. Its "why it matters" column reads: *"Product claims and the SEBI
filing path. **Yes before multi-tenant paid**; no for a sole-tenant operator desk."*

**Is it still open? Yes.** `NEEDS-MAULIK.md` item 13 records D3 as written and the OAuth gate as
flipped, then says: *"Counsel checklist C1–C3 (algo ID, research vs advice, RA/empanelment)
remains open but **non-blocking**."* `docs/COUNSEL-BRIEF.md` §5 says the same: *"None currently
blocks engineering; C3 blocks paid multi-tenant launch."* The brief was compiled 25 Aug 2026 and,
so far as this repository records, has not been sent to anyone.

**What C3 gates, precisely.** Not the OAuth redirect, not the token store, not the hand-off — those
were deliberately unblocked. It gates **paid multi-tenant launch**. And its two siblings bear
directly on an execute route even though neither is named in the stop list:

- **C1** — *"Algo ID / exchange registration when Baskfy supplies order plans to a third-party
  Kite app, versus a personal algo in one's own account at under 10 orders/sec."* Today Baskfy
  supplies *plans* and a human presses submit in Kite. An execute route means Baskfy is the thing
  submitting. C1 is the question of whether that lands on the "algo supplied to others" side, and
  it is unanswered.
- **C2** — *"Research versus advice for ranked baskets — and what changes when a basket is
  personalised to a user's existing holdings."* A rebalance plan is personalised by construction.

**The honest reading.** For **you alone, in your own Zerodha account**, C3's own answer column
says no registration is needed — "no for a sole-tenant operator desk" — which is exactly what the
desk is today and exactly what a Baskfy execute route used only by you would be. For **anyone
else's money**, C3 and C1 are open, unsent, and gating. Whether an execute route is a legal
question therefore depends entirely on whose account it can reach, which means the answer must be
built into the route rather than promised around it.

---

## 6. The alternative that needs no rule changed

**Posture B, as it already works today.** Baskfy publishes the basket; you confirm and execute in
your own Kite terminal. This is not a hypothetical — it is built, tested and live.

`decile-blueprint/services/api/src/baskfy_api/kite_basket.py` builds a Zerodha **Publisher** basket
payload. Its own docstring:

> Zerodha's **Publisher** basket is a plain HTML form POST to
> `kite.zerodha.com/connect/basket` carrying an `api_key` and a JSON `data` array. The user's
> browser makes that request; Kite renders the basket **in the user's own Kite session** and they
> review and execute it there. … Nothing here talks to a broker, holds a credential, or produces a
> fill.

Two read-only routes serve it: `GET /baskets/plan/kite` (the desk's latest plan as a basket) and
`GET /explore/{slug}/kite` (a curated basket at a chosen rupee amount), both in
`routers/kite.py` — and that router is asserted to declare **no** mutating verb at all, on the
stated reasoning that "a POST here would mean this service submitting a basket on a user's behalf,
which is the regulated activity the whole posture avoids".

**What it costs you, honestly.**

- **A second surface, every time.** You leave Baskfy, land in Kite, review, submit. That is the
  convenience the whole retirement idea is trying to buy back.
- **Ten rows per basket.** `MAX_BASKET_ITEMS = 10` — Kite's own limit. A fifteen-order rebalance is
  two form posts, and a rebalance carries sells as well as buys, so this is routine, not an edge
  case.
- **MARKET orders.** `ORDER_TYPE = "MARKET"`, deliberately: a stale reference price pinned as a
  limit is an order that silently does not fill. Kite lets you change the type in front of you,
  which is the right place for that choice — but it is a choice you make by hand, fifteen times.
- **No stops come with it.** The Publisher payload has no GTT field and `kite_basket.py` contains
  no GTT code. Non-negotiable #4 — every buy gets a vol-scaled stop the same session — cannot
  follow the basket into someone else's terminal. Today the desk arms stops separately
  (`/stops/arm`, `main.py:814`). Under a pure hand-off, arming the stop is the user's job, and
  there is nothing in the system that knows whether they did it. **This is the most serious cost
  of posture B and it is easy to overlook.**
- **No fill knowledge.** Baskfy cannot know what actually executed, at what price, or whether the
  user edited quantities — Publisher deliberately lets them. Reconciliation stays a separate,
  after-the-fact problem.

**What it preserves.** Every rule in §2 stays as written; the counsel brief stays true as sent;
posture B keeps its meaning, which is the story told to counsel, to users and in the legal pages;
the multi-tenant gaps (P4.2, P4.10, P4.11) stay theoretical instead of load-bearing; and the
product's blast radius stays "someone could read my holdings" rather than "someone could trade my
account". It also keeps the answer to C1 easy: Baskfy supplies plans, a human submits.

**A third option worth naming, because it is cheaper than either.** Keep the desk's `/execute`
exactly where it is — behind `DeskSecurity`, on localhost, one operator, no user model — and
retire everything *else* about `desk.modelbasket.in` into Baskfy: the data, the jobs, the pages,
the numbers, the domain. You would still open one small local app on rebalance Friday, but nothing
public would gain an order path and no rule in §2 would move. This is not "do nothing"; it is the
five-of-six retirement with the sixth deliberately left as a separate, tiny, unreachable box. It is
also fully reversible in either direction.

---

## 7. Signature block

Lifting this rule must be an explicit act, not something inferred from a Slack line, a ticket
title, or the general momentum of a project. Nothing in the codebase changes until the sentence
below exists, written by you, in this file, with a date.

If you intend to lift it, write **exactly** this, fill in the two brackets, and commit it:

```
I, Maulik Dave, on [DATE], lift the prohibition on a web execute route in Baskfy.

I have read docs/DECISION-EXECUTE-ROUTE.md §3 and I accept that an execute route in the
Baskfy web app is reachable by anything that can reach the Baskfy web app, and that every
authentication weakness in the product then becomes a way to place real orders in a real
account.

Scope of this lift: [SOLE-TENANT, MY OWN ZERODHA ACCOUNT ONLY  |  MULTI-TENANT].

Conditions I am NOT waiving: the route may not be built until preconditions 1 through 4 of
§4 are green and measured, and non-negotiable #1's shape is preserved exactly -- explicit
confirm, a plan_id issued by an analyze step, a 30-minute expiry, and packages/execution as
the only path to the broker. If the scope above says MULTI-TENANT, counsel item C3 must also
be answered in writing first.

Signed: Maulik Dave
```

If you do **not** intend to lift it, no signature is needed. This document then stands as the
record of why not, and posture B (§6) is the route the product takes.

**Reversal, if it is signed and later regretted.** Delete this section's signature, restore the
four rules in §2 verbatim, restore the five tests and two source comments listed in §2.5, delete
the route, and re-issue
`docs/COUNSEL-BRIEF.md` §7. Nothing about signing is irreversible *in the code*. What is not
reversible is an order that has already been placed.

---

## 8. Where the evidence points

The brief was to make the decision possible, not to argue it. But the evidence does lean, and
saying so is more useful than pretending it does not.

**Not yet — and the reason is not the rule, it is the readiness.** Five preconditions, zero green.
The gateway has no GTT method at all, so a Baskfy-placed buy today would be a naked buy. Baskfy has
never driven a plan through its own gateway even in simulation. The decisive parity test has not
been run since it was last red, and the two parity results that *were* measured are both red — a
16-symbol universe gap and four order deltas out of fifteen. A door onto that is a door onto
numbers nobody has checked.

**And when it is ready, the scope matters more than the rule.** For your own account, an execute
route is a convenience with a manageable blast radius, and C3's own answer says no registration is
needed for a sole-tenant operator desk. For other people's accounts, it is a different product with
a different legal posture, three unbuilt multi-tenant safeguards, and two unanswered counsel
questions. The signature block above splits those deliberately, because they are not the same
decision and should not be signed with the same stroke.

**The cheapest honest path to what you actually asked for** is §6's third option: retire
`desk.modelbasket.in` — its data, jobs, pages, numbers and domain — into Baskfy, and leave the
order button on a small local box that the public internet cannot reach, until preconditions 1
through 4 are green. Then re-read this document with real numbers in hand.
