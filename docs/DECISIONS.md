# DECISIONS

Decisions taken under ambiguity during an unattended build, when `docs/` did not settle the
question and stopping to ask was not an option. One entry per decision: what the bundle says, what
was chosen, and why.

This file is append-only. Nothing here overrides `docs/` — where a decision *departs* from a
document rather than filling a gap, that is said explicitly.

---

## Prompt 13 — Plans, checkout, invoices, entitlements (2026-08-21)

Everything below would normally live in `docs/13b-billing-implementation-notes.md`, following the
`07a`/`08a`/`12a` convention. The overnight instructions permit appending to this file and no
other change under `docs/`, so it is collected here instead. **A human should move §13.1–§13.18
into a numbered implementation-notes file** and leave a pointer, so `CLAUDE.md`'s "Where things
live" table can name it.

### 13.1 Advertised prices are GST-inclusive

`docs/01` §1 gives "Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999" and says nothing about tax.
`docs/11` §Compliance requires GST-compliant invoices. Read as *exclusive*, the card would be
charged ₹590 for a plan the page calls ₹500.

**Chosen:** the price in `plan.price_inr` is what is charged. `decile_core.gst.split_inclusive`
back-computes the taxable value (₹423.73 at 18%) and the tax is the remainder, so
`taxable + cgst + sgst + igst == amount_inr` exactly and the invoice total is the payment.

### 13.2 GST rate 18%, SAC 998439 — **both need a chartered accountant**

Nothing in the bundle names a rate or an HSN/SAC. 18% is the rate for online information and
database access services; SAC 998439 is "Other on-line contents n.e.c.". Both are **settings**
(`DECILE_GST_RATE_PERCENT`, `DECILE_GST_SAC_CODE`), not constants, and `payment.gst_rate` /
`payment.sac_code` store what was applied so a later change cannot rewrite an issued invoice.
This is the single item in Prompt 13 most in need of professional review before a real charge.

### 13.3 Place of supply defaults to the supplier's state

IGST Act §12(2)(b): where the recipient's address is not on record, the place of supply is the
supplier's location. So an intra-state supply (CGST + SGST) is the default, and a customer who
supplies a state or a GSTIN gets IGST when they are elsewhere. `payment.place_of_supply` and
`payment.customer_gstin` exist to carry that; **no UI collects either yet** — see §13.17.

### 13.4 Two tables docs/04 does not define: `webhook_event`, `invoice_counter`

`docs/11` §Security requires webhook deduplication "by event id" and `docs/07` requires the
endpoint be "idempotent by event id". That needs durable memory of the ids already seen; Redis is
the wrong place, because a cache flush would let every past event replay into a second payment.
`webhook_event.event_id` is UNIQUE and the insert is `ON CONFLICT DO NOTHING … RETURNING` — no
row returned means the event was already handled.

Prompt 13's acceptance criterion asks for **gapless** invoice numbers. A PostgreSQL `SEQUENCE` is
deliberately non-transactional, so a rolled-back transaction burns its number. `invoice_counter`
is one row per financial year taken with `UPDATE … RETURNING`, which holds a row lock until
commit: concurrent allocations serialise and no number is skipped. Models in
`packages/core/src/decile_core/models/billing.py`, migration `0006_billing_tables`.

### 13.5 Eleven GST columns added to `payment`

`docs/04` gives `payment` a single `gst_inr`. Rule 46 of the CGST Rules requires the taxable
value, each tax head, the SAC, the place of supply and the date of issue on the face of the
document. Those are **stored**, not recomputed at render time: an invoice whose figures move when
a rate setting changes is not a document of record. Columns: `razorpay_order_id`, `invoice_date`,
`taxable_inr`, `cgst_inr`, `sgst_inr`, `igst_inr`, `gst_rate`, `place_of_supply`,
`customer_gstin`, `sac_code`, plus a CHECK on `payment.status`, which `docs/04` leaves open.

### 13.6 The invoice PDF is written by hand, not by a library

`docs/02` locks the stack and names no PDF library; `CLAUDE.md` house rule 1 says nothing outside
it without saying why first. `docs/11` requires GST-compliant invoices and Prompt 13 §4 requires
PDF generation. `packages/core/src/decile_core/pdf.py` writes PDF 1.4 directly — a catalogue, one
page, an uncompressed content stream and two of the fourteen standard fonts every reader already
has. ~150 lines, no supply chain, and deterministic: the same invoice renders to the same bytes,
which is what makes the test an assertion rather than a smoke test.

**Courier, not Helvetica.** Every Courier glyph is 600/1000 em, so figures right-align exactly
with no font-metrics table. Transcribing Helvetica's AFM widths from memory would be inventing
data in a document of record. A designed invoice is later, deliberate work.

The rupee sign postdates WinAnsiEncoding, so amounts read `INR 500.00` rather than `₹500.00`.

### 13.7 No `razorpay` SDK

`docs/02` names Razorpay as the provider, not a library. The official package is a synchronous
`requests` wrapper with no `py.typed` — it would block the event loop on every checkout. The three
calls a checkout makes go over `httpx`, which this service already depends on for Resend.
Signature verification is `hmac` from the standard library, compared with `compare_digest`.

### 13.8 A "Forever" purchase is a `subscription` row with `current_period_end IS NULL`

`docs/04` makes `payment.subscription_id` nullable, so a one-time payment *can* stand alone — but
then nothing links the account to the plan it bought and `GET /me` has no plan to report.
`subscription` is therefore the record of a **grant**; a NULL period end means it never lapses,
which is exactly what Forever is.

### 13.9 A checkout creates the subscription row immediately, as `past_due`

The row must exist to hold `razorpay_subscription_id` before the gateway calls back. `docs/04`
offers four statuses and none means "created, not yet paid". `past_due` is the only non-terminal
one that does not entitle, which makes it the safe placeholder; `subscription.activated` promotes
it to `active`.

### 13.10 Razorpay's subscription events, mapped onto docs/04's four statuses

`activated`/`charged`/`resumed`/`updated` → `active`; `authenticated`/`pending`/`halted`/`paused`
→ `past_due`; `cancelled` → `cancelled`; `completed`/`expired` → `expired`. `halted` is Razorpay's
"we retried and gave up", which has no `docs/04` equivalent; `past_due` is the closest and the
only recoverable one, which is right — a halted subscription resumes when the customer pays.

### 13.11 A failed webhook rolls back rather than recording "failed"

`decile_api.billing.handle_event` does **not** catch exceptions from processing. If it did, the
`webhook_event` row would be committed as `failed`, the event id would be consumed, and Razorpay's
retry would dedupe into a no-op — losing a payment in silence. Letting it propagate rolls the
claim back with everything else, so the retry gets a clean attempt.

### 13.12 Entitlements are read from `plan.features`, and two behaviours changed

Prompt 7's stub hard-coded "an active subscription grants the three gated features".
`decile_api.entitlements` now reads the plan row, so:

* **Backtests are paid.** The stub granted them to everyone; Prompt 13 §6 lists them among the
  gated features. No endpoint enforces it yet — Prompt 15 builds `/backtests` — so today it shows
  up only in the `/me` payload.
* **An unpaid account's `max_screens` is 5, not 50.** `docs/07`'s example payload shows 50, which
  is the paid number. `docs/04` §Retention says "prune runs older than 400 days for free users",
  so an unpaid account is expected to exist and save screens; nothing says how many. **Five is
  invented.** It is one constant, `decile_core.entitlements.FREE_MAX_SCREENS`.

An `active` subscription whose `current_period_end` is in the past does **not** entitle, even
though the status column still says `active`. The row is only as fresh as the last webhook, and an
entitlement that outlives its period because `subscription.expired` was never delivered is a free
subscription.

### 13.13 `/me` keeps exactly seven entitlement keys

`docs/07` §Entitlements fixes them. `community_slack` and `ama_access` — two of `docs/01` §1's
five gated features — are **not** API surfaces (nothing here can grant a Slack invitation), so they
are plan *features* published by `GET /plans` and not members of the `/me` payload. The ₹0 tier's
universe restriction is likewise internal: it is enforced server-side and advertised on the plan,
not added as an eighth key.

### 13.14 The ₹0 tier's "limited universe" is NIFTY 50

Prompt 13 §5 says "a ₹0 free tier with a limited universe" and does not say which. NIFTY 50 is the
narrowest of the fourteen and the one a new user recognises. Behind `DECILE_FREE_TIER_ENABLED`,
default **off**, and the `free` plan row is only seeded when the flag is on — a flag that leaves
the row in the catalogue while hiding it from one endpoint is a flag that half-works.

### 13.15 The Forever disclosure says both sentences

`docs/11` §Compliance: the plan "must state, at the point of sale, that it means the lifetime of
the service". Prompt 13 §5 quotes the reference product's own words: "Forever means the lifetime
of the website". Both are in `seed_data.FOREVER_DISCLOSURE`, the reference's sentence first,
followed by what it means for the buyer. Served by `GET /plans`, so `/pricing` renders the API's
words rather than a second copy in a component.

### 13.16 Invoice PDFs are streamed, not presigned

Prompt 13 §4 says "stored in R2"; `docs/07` says `GET /invoices/{id}/pdf`. A presigned URL is a
bearer credential for a document carrying the customer's name and GSTIN, and it works for anyone
who obtains it. The endpoint streams the bytes so the ownership check happens on every read.
Storage reuses `decile_providers.archive` — R2 when configured, a directory otherwise — rather
than growing a second storage abstraction; the boto3 calls run on a worker thread.

### 13.17 What is **not** built

* **No UI collects a customer GSTIN or state.** The columns and the arithmetic exist and are
  tested; every invoice raised today is B2C, intra-state, at the supplier's own place of supply.
* **`payment.status` never becomes `refunded`.** `refund.*` events are acknowledged and ignored;
  `docs/07` has no refund endpoint and `docs/11` names a Refund Policy page that is not written.
* **Nothing enforces the backtests entitlement**, because `/backtests` does not exist (Prompt 15).
* **The webhook is verified but has never seen a real Razorpay delivery.** The signature scheme,
  the event names and the entity shapes are written from Razorpay's documented formats, not
  against the live gateway — the suite is network-blocked. Confirm against a test-mode delivery
  before the first production charge.

### 13.18 Webhooks get their own rate-limit bucket

`docs/07` §Conventions meters callers: 60/min authenticated, 10/min anonymous. A payment gateway
is neither, and Razorpay retries a failed delivery — metering it at the anonymous rate would drop
events. `/webhooks/*` has its own per-IP bucket at `DECILE_RATE_LIMIT_WEBHOOK_PER_MINUTE`
(default 600), so the endpoint is still metered rather than open.

---

## Prompt 14 — Portfolios and rebalance tracker (2026-08-21)

Sources: `docs/01` §8 (the tracker as the reference product ships it), `docs/07`
§"Portfolios & rebalance" (six routes and the `inside_wrh` definition), `docs/08` §Routes and
§"Rebalance tracker" (the wizard), `docs/04` (`portfolio`, `portfolio_holding`). Each decision
below is a place those documents do not settle the question.

### 14.1 Target weights are equal-weight

`docs/07` asks the rebalance response for `target_weights` and says nothing about how they are
computed. `docs/10` §"Execution model" offers four schemes (`equal | inverse_volatility | rank |
marketcap`), but that is the *backtest's* configuration and Prompt 14 §3's wizard has no weighting
input — portfolio, screen, `top_n`, `hold_buffer`, and nothing else. Equal weight is `docs/10`'s
own default, needs no factor the tracker does not already have, and is the only scheme that cannot
be wrong for a reason the user was never asked about. `decile_core.rebalance._target_weights`.
The last name absorbs the rounding remainder so the column sums to exactly `1.000000` at
`numeric(10,6)` — `docs/04`'s precision for `index_member_daily.weight`, reused because the
bundle gives portfolio weights none.

### 14.2 A fourth list, `holds`

`docs/01` §8 and `docs/07` name three lists: exits, inside_wrh, entries. A held name ranked
*inside* `top_n` is in none of them, yet it is in the target weights. It is returned as `holds`,
an additive field: three columns that omit half of someone's portfolio read as if we had lost it.
The web app renders the three docs/01 columns and nothing more; `holds` feeds the weights table.

### 14.3 `portfolio_rebalance` — a table not in `docs/04`

Prompt 14 §4 requires the history in as many words. `payload` stores the response **verbatim**
rather than columns to re-render from: the screen can be edited, the buffer changed and a name
delisted afterwards, and a record of advice given must still say what the user saw. `screen_id` is
`ON DELETE SET NULL` so deleting a screen cannot erase the history of what it once told someone;
the screen's name and public id are inside `payload`. Migration `0007_portfolio_rebalance`, which
also indexes `portfolio_holding.instrument_id` — the composite primary key leads on
`portfolio_id` and cannot serve the direction the rebalance join reads.

### 14.4 A BSE scrip code is reported, never resolved

Prompt 14's acceptance criterion names "a BSE-style code" among the messy inputs. This service
holds NSE instruments and has no BSE-code mapping, so a six-digit numeric token is classified as
`bse_code` and reported unmatched **with that reason**. Guessing an NSE symbol from a BSE code
would put a position in someone's portfolio that they did not choose.

### 14.5 "Ambiguous" is a verdict, not a tie to break

`instrument` is unique on `(exchange_id, symbol, series)`, so one symbol can name several rows —
the `EQ`/`BE` pair of one company is the ordinary case. Resolution goes active listing → any
listing → `symbol_alias`, and a tier returning more than one row stops there as `ambiguous` with
its candidates listed. Nothing is imported for it. Silently preferring `EQ` would be a guess about
which security someone owns.

### 14.6 Four routes `docs/07` does not list

* `PATCH` and `DELETE /portfolios/{id}` — Prompt 14 §1 says "Portfolio CRUD"; `docs/07`'s six
  routes have no update or delete, and a portfolio that can never be renamed or removed is not
  CRUD.
* `GET /portfolios/sample-csv` — `docs/01` §8 and `docs/08` both require a downloadable sample.
  Served by the API rather than as a static asset in the web app so the file a user downloads is
  the file `decile_core.portfolio_csv` is tested against.
* `GET /portfolios/{id}/rebalances` and `/rebalances/{rebalance_id}` — §4's history has to be
  readable to be worth persisting. The single-record read returns the stored payload verbatim.

### 14.7 `{id}` is the numeric `portfolio.id`

`docs/04` gives `screen` a `public_id` and `portfolio` none. So the path parameter is the numeric
id, and a portfolio belonging to someone else is a `404` — the rule `docs/07`'s screens surface
already follows, for the same reason: a `403` confirms the id exists.

### 14.8 The tracker is not entitlement-gated

`docs/07` §Entitlements gates seven things and a portfolio is none of them, so any signed-in
account can keep portfolios and rebalance them. The **screen** a rebalance runs keeps its own
gates: the ₹0 tier's universe restriction, and `historical_ranks` when a past `as_of` is asked
for.

### 14.9 A holdings `PUT` replaces, and keeps `added_on`

`PUT /portfolios/{id}/holdings` is a replacement, not a merge — a merge would leave no way to sell
a position. A name that was already held keeps its original `added_on`, so re-uploading the same
file does not reset every purchase date; `docs/04` gives the column no meaning other than "the day
this row appeared".

### 14.10 `python-multipart` is a new dependency

`docs/07` specifies `POST /portfolios/import-csv  multipart`, and Starlette cannot parse a
multipart body without it. Not named in `docs/02`; the alternative is hand-rolling RFC 7578
boundary parsing. Flagged here per house rule 1.

### 14.11 What is **not** built

* **No holdings editor.** Holdings are set by CSV upload or by a JSON `PUT`; there is no
  add-one-row form in the web app. `docs/08` §"Rebalance tracker" describes the wizard and does
  not ask for one.
* **Nothing reconciles quantities.** `quantity` and `avg_price` are stored and echoed; no weight,
  exposure or P&L is computed from them. `docs/01` §8's tracker is a symbol diff, and inventing a
  valuation would be inventing a feature.
* **The rebalance response is not cached.** The screen behind it is executed uncached on every
  call, because `run_screen`'s cached bytes carry no `instrument_id` and the rule matches on
  `instrument_id`, never on symbol (a symbol diff would exit a position on the day NSE renames
  it).
