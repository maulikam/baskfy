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

---

## Prompt 15 — Backtest engine (2026-08-21)

As with §13 and §14, these would normally live in a numbered implementation-notes file
(`docs/10a-backtest-implementation-notes.md`, following the `07a`/`08a`/`12a` convention). The
overnight instructions permit appending to this file and no other change under `docs/`, so they
are collected here. **A human should move §15.1–§15.20 into that numbered file** and leave a
pointer, so `CLAUDE.md`'s "Where things live" table can name it.

### 15.1 The dividend policy default is `ignore`, and `cash`/`reinvest` are refused today

> **Reversed at M39, and this entry was left standing for months after it stopped being true.**
> Corrected 23 August 2026 (M45.9). The original text is preserved below the rule, because it is
> the premise four user-facing surfaces were written from and a reader who finds one of those
> should be able to see where it came from.

`docs/10` §"Execution model" step 7 offers `dividends: "reinvest" | "cash" | "ignore"` and says
"Corporate actions are already in the adjusted series; cash dividends are optionally credited as
cash". What the series actually contains was an open question until it was measured.

M27 put it to the reference corpus: the **price** convention won 42 of 45 deciding symbol-windows
and matched all 25 dividend-paying symbols exactly at stored precision. M28 then applied the 47
share-count actions and deliberately **not** the 38 dividend-shaped ones
(`reconciliation/RECOVERED-ACTIONS.md`, "VERDICT: PRICE RETURN").

So `ohlcv_daily.close` is a **price-return** series: splits and bonuses are inside it, cash
dividends are not.

**Chosen:** `ignore` is the default. It is exact, needs no extra data, and is what the engine has
always actually computed — until M39 it was doing precisely this under the name `reinvest`, which
is why the pre-M39 read shim rewrites the label and the arithmetic is bit-identical. `cash` and
`reinvest` both have to *add* a dividend back, so both need a dividend schedule, and both are
**refused with a `BacktestConfigError`** rather than quietly serving a price return under a
total-return name. See `DividendPolicy`, and `docs/DECISIONS-MERGE.md` §M39.3 and §M43.

Every return this product publishes is therefore a price return, and is lower than a total return
by roughly the dividend yield — about 1.2% a year on NSE, compounding. Any new surface that shows
a return owes the reader that sentence.

<details><summary>Superseded original (pre-M39)</summary>

> Those two sentences cannot both be acted on with the series this repository stores. `docs/09`'s
> adjustment algorithm — implemented in `decile_core.adjustments` — folds **cash dividends** into
> `adj_factor` alongside splits and bonuses, so `ohlcv_daily.close` is already a *total-return*
> series. Crediting the dividend as cash on top of it counts it twice.
>
> **Chosen:** `reinvest` marks to the adjusted series as-is, which is exactly what back-adjustment
> models, and is the default. `cash` and `ignore` are implemented in the engine against a
> dividend-stripped price series (`price_open`/`price_close` on the panel) and are **refused with
> a `BacktestConfigError`** until a loader supplies one.

The measurement that overturned it is M27/M28; the belief was never tested when it was written.

</details>

### 15.2 Prices are float for lookup, `Decimal` for every rupee that moves

CLAUDE.md house rule 9 says money and prices are `numeric`, never `float`. A 2,800 × 2,000 price
matrix of `Decimal` objects is 5.6 million Python objects, which `docs/10` §Performance's
ten-second budget cannot afford.

**Chosen:** the lookup table is `float64`; every price is converted back to `Decimal` at the point
of use with `Decimal(f"{x:.4f}")`. `ohlcv_daily` stores prices at four decimal places, and a
four-decimal value below 10¹¹ survives a float64 round trip exactly, so the conversion recovers
the stored number rather than approximating it. **Cash, notional, cost, dividend and equity are
`Decimal` throughout.** A departure from the letter of house rule 9, stated out loud.

### 15.3 Statistics are float

Volatility, Sharpe, Sortino, beta, tracking error and the rest live in
`decile_core.backtest_metrics` and are computed in NumPy. They are ratios estimated from a sample,
not money; a Sharpe ratio in `Decimal` would be false precision on a number whose second decimal
place is noise. `Metrics.as_dict` rounds them to ten places, which is also what makes `docs/10`'s
determinism test meaningful.

### 15.4 Annualisation is observed, not assumed

`docs/10` names no annualisation factor. 252 is the number that gets copied from US texts; NSE
gives roughly 247 trading days a year (`docs/13` §3). The engine divides the number of daily
returns by the elapsed year fraction and uses that, so the factor is a property of the data.

### 15.5 The risk-free rate is a flat annual rate, defaulting to zero

`docs/10` §Outputs asks for "Sharpe (rf from a configurable T-bill series)". `docs/04` has no
T-bill table and no pipeline step fetches one. A flat `risk_free_rate` on the config is the only
thing this service can serve; **zero** is the default, because an invented 6.5% would move every
Sharpe and Sortino on the page. It is configuration rather than a constant so it stops being a
placeholder the moment a series exists.

### 15.6 The first trading day is always a rebalance date

`docs/10` §Config gives a frequency and a day-of-period but no rule for the stub period at the
front of the window. Waiting for the end of the first month would leave the whole initial capital
in cash for up to a month and would silently change the answer for any short backtest.

### 15.7 A decision on the final day of the window is never filled

`docs/10` §4 requires execution at the next trading day's open. On the last day of the run there
is no next day inside the window, so the decision is dropped rather than filled at that day's
close — filling it at the close is precisely the shortcut §4 exists to forbid.

### 15.8 Position limits are water-filled, and an impossible bound is relaxed rather than raised

`docs/10` §3 says weights are "clipped by `position_limits`, renormalised" and stops there.
Clipping once and renormalising pushes clipped names back through their own cap, so
`apply_position_limits` iterates to a fixed point, resolving ceilings before floors. Twenty names
cannot each hold ten per cent; rather than refusing a portfolio the user can see on the screen,
the offending bound is relaxed to `1/n`, which is equal weight.

### 15.9 Rank weighting is linear **within the selected set**

`docs/10` names `rank` as a weighting scheme and does not define it. Weighting by the screen's
absolute rank would make a portfolio drawn from ranks 400–420 very nearly equal-weighted. The best
selected name gets `n`, the worst gets 1.

### 15.10 Delisting is detected by a backward-looking staleness rule

`docs/10` §8 says to liquidate a delisted holding at its last available close and never
forward-fill. `instrument.delisted_on` is the primary trigger. Where it is absent, a position is
closed once its instrument has printed no bar for five consecutive trading days — a question
answerable on the day it is asked, so no future data decides it. A halt of a day or two is carried
at its last close; a series that has stopped for a week has stopped.

### 15.11 Costs are charged on both legs

`docs/10` §5 says "Apply costs on traded notional" without distinguishing buys from sells. In
India STT is levied on both legs of a delivery trade, brokerage is per order, and slippage is a
property of crossing the spread in either direction, so all three are charged on every fill.
`impact_model` accepts only `"fixed"`, the one value `docs/10` names.

### 15.12 Hit rate and average win/loss are measured over **round trips**

`docs/10` asks for them and does not define the unit. Counting every partial trim of a winner as
its own winning trade would put the hit rate wherever the rebalance frequency happened to put it.
A round trip is one position, from the first share bought to the last share sold, net of every
cost on the way in and out.

### 15.13 The loader reads only the top `top_n + hold_buffer` rows of each screen

A name ranked worse than the buffer limit is sold at the next rebalance whether it appears in the
frame or not (`decile_core.rebalance` exits it either way, and the engine does not record the
reason). Loading four thousand rows for each of 180 rebalance dates to decide something already
decided would be the slowest possible way to reach the same answer. The bar panel is restricted
to the union of those candidates for the same reason.

### 15.14 Three artefacts, one `trades_key` column

`docs/10` sends "trades, per-day holdings" to R2; `docs/04` gives `backtest` a single
`trades_key`. Three CSVs are written — the trade log, the per-rebalance holdings and the full
daily equity/drawdown series — under one deterministic prefix derived from `public_id`, and
`trades_key` records the trade log's key. The other two are `artefact_key(public_id, …)` of the
same prefix.

### 15.15 The "signed URL" is signed by this service

`docs/07` asks `GET /backtests/{id}/export` for a "CSV/Parquet signed URL". A Cloudflare R2 bucket
can mint a presigned GET; a directory on a laptop cannot, and the archive abstraction covers both
(`docs/02` §"Object storage"). The link is therefore an HMAC over `(public_id, artefact, expiry)`
keyed on the JWT secret, redeemed at an **unauthenticated** download route that streams the
object. It expires in fifteen minutes and names exactly one object. **CSV, not Parquet:** `docs/07`
offers either, `docs/02` locks no Parquet writer for the API, and a trade log is a table a user
opens in a spreadsheet.

### 15.16 A concurrency refusal is a `429`

`docs/07`'s error catalogue has no "busy" type. `429 rate-limited` with `Retry-After` is the row
that means "come back later", and the `detail` names which cap was hit. The per-user cap of 1 is
Prompt 15 §4's; the **global cap is not in the bundle at all** — `docs/03` §"Scaling plan" step 4
only says backtests get their own worker pool. Eight is a chosen default, and both are settings
(`DECILE_BACKTEST_USER_CONCURRENCY`, `DECILE_BACKTEST_GLOBAL_CONCURRENCY`).

### 15.17 Four routes `docs/07` does not list

`GET /backtests` (docs/08 §Routes names a `/backtests` page, and a page listing runs needs an
endpoint listing runs), `GET /backtests/{id}/holdings` (docs/08 §Backtests asks the results page
for "per-period holdings"), `GET /backtests/{id}/events` (Prompt 15 §4 requires SSE, which
postdates `docs/07`'s list), and `GET /backtests/{id}/download/{artefact}` (what the signed export
link redeems against).

### 15.18 The API is now a Celery **producer**

`docs/02` already locks "Jobs — Celery + Celery Beat". What is new is that `decile-api` depends on
the library directly: `POST /backtests` publishes `decile.backtest.run` by name, because
`decile-worker` depends on `decile-api` and the import cannot go the other way. See
`decile_api.queue`. A missing broker leaves the row `queued` and logs it rather than failing the
request — the run *was* recorded, and an operator can re-drive it.

### 15.19 Deleting a backtest does not delete its R2 artefacts

The objects are keyed by `public_id`, which is never reissued, so nothing can read them once the
row is gone. Deleting them inside the request would make a `DELETE` depend on an object store
being reachable. A sweeper belongs with the other retention jobs (`docs/04` §"Retention & size
estimates") and **does not exist yet**.

### 15.20 What is **not** built

* **No dividend schedule**, so `dividends: "cash"` and `dividends: "reinvest"` are refused end to
  end (§15.1). Only `ignore` runs — which is exact, because the stored series is a price return.
  Corrected at M45.9; this line had the two sets of policies the wrong way round since M39.
* **No Parquet export.** `docs/07` offers "CSV/Parquet"; only CSV is served (§15.15).
* **No artefact retention sweeper** (§15.19).
* **No Playwright coverage.** The engine, the loader, the job, the endpoints and the React
  components are all tested, but no browser test walks the config form through to a results page.
  The same gap the rebalance tracker has.
* **The 15-year performance criterion is measured on a synthetic market, not "the seeded
  dataset".** The seeded database is a single trading day of *results* (`docs/13`'s export); it
  has no price history, so no fifteen-year backtest can run against it. See the module docstring
  in `packages/core/tests/backtest_fixtures.py`.

---

## Prompt 16 — Performance, caching, and load hardening (2026-08-21)

Every judgement call taken while implementing Prompt 16 under the overnight working agreement.

### 16.1 The "as measured" column is in `benchmarks/AS-MEASURED.md`, not in `docs/11`

Prompt 16's first acceptance criterion asks for the measured numbers "written into docs/11 as an
'as measured' column". The overnight rules forbid editing anything under `docs/` except appending
to this file. The table is therefore rendered to `benchmarks/AS-MEASURED.md` by
`python -m benchmarks.report`, in the same row order and with docs/11's own wording in the Surface
and Target columns, ready to be pasted in. **This is an unmet acceptance criterion in the letter
and a met one in substance; somebody has to paste it.**

The rendered table carries a "Measured against" column docs/11 does not ask for. It is the honest
half: "4 ms" against docs/13's 271-row single-date export and "4 ms" in production are not the
same claim, and a number without its dataset invites the confusion.

### 16.2 `market_health_daily` and `index_snapshot_daily` became hypertables

docs/03 §"Scaling plan" step 3 asks for "Timescale continuous aggregates for market-health and
index history" and Prompt 16 §4 asks for them to be built. A continuous aggregate can only be
defined over a hypertable, and docs/04 declares both of these as plain tables. Migration 0008
converts them, on the same one-year chunk interval every other hypertable uses; `date` is already
the second column of both primary keys, which is what makes the conversion legal.

This is a **deviation from docs/04's DDL**, taken because docs/03 asks for something docs/04 does
not anticipate rather than because a plain table was inadequate.
`services/api/tests/test_migrations.py` asserts an exact set of hypertables and now names five
instead of three, split into `DOCUMENTED_HYPERTABLES` and `SCALING_HYPERTABLES` so the reason is
readable at the assertion. Reversing it is one migration.

The aggregates (`market_health_monthly`, `index_snapshot_monthly`) bucket monthly, exclude the
current bucket (`end_offset => 1 day`, for the same reason docs/06 §step 1 refuses a half-written
day), and are refreshed by a Timescale policy. **Nothing reads them yet.** docs/03 says the
scaling plan is followed "only when measured", and at one seeded date there is nothing to measure;
`test_timescale.py` asserts they agree with their source tables so that whoever wires them up
inherits a verified aggregate rather than an unverified one.

### 16.3 No pytest-benchmark, no k6, no Locust

Prompt 16 §1 suggests "pytest-benchmark + k6 or Locust". docs/02 §"The decision in one table"
locks "pytest + hypothesis" for the Python suite and CLAUDE.md house rule 1 requires a reason
before a dependency outside it.

* **pytest-benchmark**: Prompt 7 already hand-rolled the p95 harness these budgets need
  (`test_api_benchmark.percentile`). The library's calibration machinery targets microbenchmarks,
  not a request that talks to PostgreSQL and Redis, and a docs/11 budget is a threshold rather
  than a distribution — its statistics would not be what is asserted.
* **k6**: a Go binary. Nothing in this repository can install one.
* **Locust**: a gevent runtime and a web UI to do what `asyncio.gather` does in thirty lines
  against `httpx`, which the suite already depends on.

The load driver is `benchmarks/load_screens.py`, used from a pytest test over an ASGI transport
(so CI needs no server) and from `make loadtest` over real HTTP (so the number can include the
socket). Both are labelled in the report.

### 16.4 `ix_factor_daily_date` dropped; the composite rebuilt with `INCLUDE (instrument_id)`

CLAUDE.md's open items assigned this to Prompt 16: "`ix_factor_daily_date_marketcap_cr` is dead
weight today ... Prompt 16 should drop one or rebuild the other with `INCLUDE (instrument_id)`."
Migration 0008 does both. `(date)` is a strict prefix of `(date, marketcap_cr)`, so nothing the
narrow index served is lost; the nightly `compute_factors` step writes one index instead of two;
and docs/06 §step 3's decile bucketing can now read `(instrument_id, marketcap_cr)` for one date
index-only.

Consequence worth noting: **Prompt 6's fifth acceptance criterion now holds as written.** It asked
this suite to "assert with EXPLAIN that it uses the (date, marketcap_cr) index", which had been
impossible while the narrow index shadowed it;
`test_screener_performance.py::TestThePlan` now asserts it directly rather than accepting either
index.

### 16.5 Two indexes added, both found by an `enable_seqscan = off` probe

* `ix_index_member_daily_date_instrument_id` — the factsheet asks "which indices is *this*
  instrument in today" (docs/10a §4), the opposite direction from the screener's "who is in this
  index", and neither the primary key nor `(date, index_id)` puts `instrument_id` in a usable
  position.
* `ix_instrument_listings_page` — an expression index on
  `(coalesce(listed_on, DATE '0001-01-01') DESC, symbol ASC) WHERE is_active IS TRUE`, which is
  the listings register's exact sort key. Before it, the planner chose a sequential scan *even
  with sequential scans disabled*, which means no index applied at any table size.

The predicate is spelled `is_active IS TRUE` rather than `is_active` because the handler writes
`Instrument.is_active.is_(True)` and PostgreSQL's partial-index prover does not derive one form
from the other. A partial index whose predicate does not match to the letter is an index nothing
will ever use.

### 16.6 The plan gate is a change detector, not a ban on sequential scans

Prompt 16 §2 asks for "a CI check that fails if any hot query's plan **changes to** a sequential
scan on the seeded dataset". On 271 rows PostgreSQL is right to scan sequentially, so a blanket
ban would either fail permanently or have to be enforced with `enable_seqscan = off` — which
measures a plan production never runs. `services/api/tests/query_plan_baseline.json` records each
guarded relation's scan node; a relation that *was* index-scanned and is now sequentially scanned
fails, and a hot query with no baseline entry fails until it is recorded deliberately.

Beside it, `TestIndexAvailability` runs every hot query with `enable_seqscan = off`: a relation
still read sequentially there has no applicable index at any size. That is the assertion that
found `ix_instrument_listings_page`.

The limitation is stated in the module rather than left to be discovered: a plan captured against
271 rows says little about the plan against the ~8.5M docs/04 projects. What the gate catches is a
*lost* index, which is otherwise invisible until the table is big.

### 16.7 ETags are `W/"v{data_version}-{sha256(body)}"`

docs/06 §"Determinism guarantee" ties an answer to its `data_version`, so the version is the
strongest part of the validator we can offer — but one version covers every instrument, every
universe and every query string, so the digest says which answer within it. A publish changes the
first half for every route at once, which is what makes "revalidate on `data_version`" true at the
HTTP layer as well as in the Next data cache (`docs/11a` §6).

Weak, not strong: we control the body handed to the transport, not the encoding a proxy applies.
`private` and `Vary: Authorization` on every one, because these payloads are entitlement-filtered
and a shared cache must never hand one user's answer to another. Scope is the published analytics
reads only (`decile_api.http_cache.CACHEABLE_PREFIXES`) — never the CSV export, which is a
`StreamingResponse` whose whole point is that it is not buffered.

`Cache-Control: private, max-age=0, must-revalidate, stale-while-revalidate=60`. `must-revalidate`
because a screener that silently serves last week is worse than one that errors.

### 16.8 A per-process single-flight, not a distributed lock

The 50-concurrent load test failed its own criterion on a cold cache: p95 503 ms against 400 ms,
zero errors — fifty simultaneous misses on one key, each issuing the same statement.
`decile_api.screener.SingleFlight` collapses concurrent misses per process; the measured p95 is
now 304 ms with the same cold start.

**Per process, deliberately.** A cross-process lock means a Redis `SET NX` with a lease, a fencing
token and a story for what happens when the leader dies holding it. With one uvicorn worker per
core (docs/11 §"Cost envelope" sizes the box at 8 vCPU) the stampede falls from N callers to 8 —
a query per core, not a query per request. It is not a distributed lock and must not be relied on
as one. docs/06 §Caching's post-publish warm-up remains the more important half of the fix.

### 16.9 The dashboard's sparkline query is now date-bounded

`_sparklines` kept the newest 30 levels per index with a window function over *every* snapshot row
ever written. The cost of one dashboard page therefore grew with the age of the service rather
than with the size of the answer. It now floors at `as_of - SPARKLINE_WINDOW` (90 days, roughly
double the 42–46 calendar days that 30 NSE trading days span), which bounds the scan to the chunks
that can contain the answer. The margin is deliberate: a short window would silently shorten
sparklines after a holiday run.

### 16.10 Connection pool sizing

`pool_size=10`, `max_overflow=10`, `pool_timeout=5 s`, `pool_recycle=1800 s`, `pool_pre_ping` kept.
Sized for one uvicorn worker per core on the box docs/11 §"Cost envelope" names: 8 × 20 at
absolute peak, 8 × 10 in the steady state. Beyond roughly the server's core count, more concurrent
statements do not finish sooner — they finish together, later, all past budget — so the queueing
belongs in the application, where `pool_timeout` can refuse cleanly, rather than in the database,
where nothing can. The arithmetic is in `decile_api.db.create_engine`'s docstring.

**No pgbouncer.** docs/02 locks no connection proxy and none is needed at this size. If one is
introduced it must run in *transaction* pooling mode with `DECILE_DB_STATEMENT_CACHE_SIZE=0`;
asyncpg's prepared-statement cache is per server connection and transaction pooling breaks that
assumption. The setting exists so that is one environment variable rather than a code change.

### 16.11 Charts are dynamically imported; `Sparkline` is not

`EquityChart`, `DrawdownChart` and `BreadthHistory` load through `next/dynamic` with `ssr: false`
(a chart sizes itself against the viewport, so an SSR pass followed by a hydration re-render is a
layout shift, and docs/08 budgets CLS at < 0.1). `/backtests/[id]` fell from 172 kB to 158 kB of
first-load JS and `/market-health` from 133 kB to 119 kB.

`Sparkline` stays static: it renders once per *row* in the dashboard table and once per metric
card on the factsheet, and a dynamic import per row trades one shared chunk for hundreds of
loading states. docs/11's budget is about the route, not about every component on it.

`apps/web/src/components/market/breadth-history-lazy.tsx` exists because `/market-health` is a
Server Component and `ssr: false` is not available there.

### 16.12 What is **not** done

* **The "as measured" column is not in `docs/11`** (§16.1). It is generated and committed at
  `benchmarks/AS-MEASURED.md`.
* **The nightly pipeline end-to-end budget (< 45 min) is not measured.** Nine of docs/03's ten
  steps are network fetches and the suite is network-blocked; the tenth is measured on a synthetic
  panel with no database on either side of it. The row reads "not measured" in the table rather
  than being quietly dropped.
* **Nothing reads the continuous aggregates** (§16.2).
* **Every server-side benchmark is in-process over an ASGI transport** — no socket, no uvicorn
  worker pool, one event loop. They are floors. `make loadtest` against a running server is the
  closer measurement and has not been run against a deployment.
* **The dashboard and factsheet budgets are measured on 271 instruments and 117 indices**, not the
  ~2,300 and ~145 the documents describe. Only the CSV export builds a synthetic universe to reach
  the size docs/11 states.

---

## Prompt 17 — Observability, admin, and operations (2026-08-21)

Everything below was decided while building Prompt 17 and is **not** in the bundle. Where a
decision contradicts something a document says, it says so.

### 17.1 `docs/runbooks/` was created, and it is the one place under `docs/` this run wrote to

The overnight instruction is "never edit anything under `docs/` except appending to
`docs/DECISIONS.md`", with the reason attached: "the specs are the source of truth, not something
to adjust when the code disagrees with them." PROMPTS.md Prompt 17 §5 names its deliverable by
path — "Runbooks in `docs/runbooks/`: kite-token-expired.md, pipeline-failed.md,
bad-data-published.md ..., restore-from-backup.md, razorpay-webhook-replay.md".

Those two are in tension only if "edit" means "create a new file in a new subdirectory". They are
not, so the runbooks are at the path the prompt names. **No existing file under `docs/` was
touched.** The distinction against §16.1 is deliberate: Prompt 16 asked for the "as measured"
numbers to be written *into `docs/11`*, which is an edit to a specification and was correctly
refused; five new operational documents in a directory no specification occupies are not.

### 17.2 Three additions to `docs/04`: `app_user.is_staff`, `entitlement_override`, `admin_action`

`docs/09` §Observability puts the operator UI "behind staff auth" and the bundle never says what
staff *is*. `app_user.is_staff` is that answer: a boolean on the account, server-side, never set
by a self-service form. It is on the account rather than in a table of its own because a join on
every authenticated request to answer one bit is a worse shape, not a better one.

`entitlement_override` exists because Prompt 17 §4 asks for an "entitlement override" and the two
alternatives are both wrong. Writing `plan.features` changes what *every* account on that plan
gets. Inserting a synthetic `subscription` row makes the billing history — the thing GST invoices
are raised against — lie. So an override is its own row, with an author, a mandatory reason and an
expiry, applied by `decile_core.entitlements.apply_overrides` after the plan is resolved, inside
the one function every gated endpoint already calls.

`admin_action` is **not asked for by anything in the bundle.** Every action on the admin surface is
either privileged (granting an entitlement) or expensive (re-running a night, rewriting an
instrument's whole adjusted history), and none of them can be undone. A privileged, irreversible
action with no record of who took it is a hole that is only noticed after it matters. One
append-only row per action, written in the same transaction as the action.

### 17.3 A non-staff caller gets 404 from `/admin/*`, not 403

`docs/07`'s error catalogue has no `forbidden` type, so a 403 would have to be invented. More to
the point, a 403 confirms that the path exists and that the caller merely lacks a bit, which tells
an attacker exactly which endpoint is worth getting a session for. An *anonymous* caller still
gets 401, because "you are not signed in" is not a secret. `decile_api.auth.require_staff`.

### 17.4 `/admin/*` is in the OpenAPI document and in the generated TypeScript client

`docs/07` describes the product's API and says nothing about `/admin`. The routes are published
anyway, tagged `admin`, because `docs/02` rule 5 is "typed end to end — Pydantic models → OpenAPI
→ generated TS client. No hand-written fetch types", and a staff page is not an exemption from it.
`services/api/tests/test_api_artifacts.py` lists them so that a route which is served and not
written down still fails the build.

### 17.5 The run record is committed before the chain starts

`decile_worker.orchestrator` runs all ten steps in **one** transaction, for the reason its own
docstring gives: a run that dies between `apply_adjustments` and `compute_factors` would leave
adjusted prices beside stale factor rows. That is right for the data and wrong for the audit
trail — a `SIGKILL` rolls the transaction back and the `pipeline_run` row goes with it, so the
operator sees a night with no run at all, indistinguishable from a night Beat never fired.

So `decile_worker.ops.begin_run` writes and commits the run row on a session of its own *before*
the chain opens its transaction, and the orchestrator adopts it by id. A clean failure updates it
to `failed`; a killed worker leaves it in `running` forever, which is a distinct and detectable
state. The step rows still share the chain's fate, and that is unchanged: a partial step history
for a rolled-back night would describe work that did not happen.

### 17.6 Abandonment is detected by age, not by a heartbeat

`decile.ops.reap_abandoned_runs` fails any run still `running` past
`DECILE_PIPELINE_STALE_AFTER_MINUTES` (90) and raises `pipeline_abandoned`. A heartbeat would find
it in seconds instead of up to 105 minutes (90 + the 15-minute sweep). It would also need a second
connection held open for the whole run, writing a row the chain's own transaction is also writing
— two writers on one row, one of them outside the transaction that owns it. The stale window sits
above `docs/11`'s 45-minute end-to-end budget with room for a slow night, so a *healthy* long run
is never reaped. Detection latency is the price and it is worth paying.

### 17.7 Six alert rules, evaluated in two places

Four of `PROMPTS.md` §3's rules are facts this codebase learns directly — pipeline failure, gate
failure, Kite token expiry, and (added) an abandoned run. Those are raised in-process by
`decile_worker.alerts.dispatch`, so the alert carries the run id, the failing assertion and the
error text rather than a threshold crossing. Two of them — API error rate > 1% and queue backlog —
are rates over a window, which is what a time-series database is for; they are Prometheus rules in
`infra/prometheus/alerts.yml`. The publish deadline is in **both**, because `docs/11` names a
specific instant (20:15 IST) and because a worker that is itself down must not take the alert
about it down as well.

An alert always logs at ERROR with `alert=` set and always increments `decile_alerts_total`.
Beyond that it goes to Sentry, an ops email address and a JSON webhook, each only if configured.
**None configured is a valid deployment** — it is a laptop — and `dispatch` says so once per
process rather than failing.

### 17.8 Prometheus is a dependency `docs/02` does not lock

`docs/02` §Observability locks "OpenTelemetry → Grafana/Tempo/Loki" for traces and logs and names
no metrics backend. `PROMPTS.md` Prompt 17 §2 asks for "Prometheus metrics + Grafana dashboards"
by name, and Grafana is already the locked dashboard surface, so `prometheus-client` is added to
`decile-api`. It was already in `uv.lock` as a `flower` dependency; this makes the use explicit.

`/metrics` sits **outside** `/api/v1`: it is not part of `docs/07`'s surface, it carries no rate
limit (a fifteen-second scrape would eat the anonymous bucket in a minute), and it is not in the
OpenAPI document, so it never reaches the generated client. It is unauthenticated unless
`DECILE_METRICS_TOKEN` is set, which is correct on a private network and wrong on the public
internet — `docs/runbooks/` says so.

### 17.9 Pipeline metrics are read from the database at scrape time, not held in the API

Nine of `docs/03`'s ten steps run in a Celery worker that may not be the process being scraped,
and a worker that finished the run an hour ago may since have been restarted. So
`decile_api.metrics.refresh_pipeline_metrics` re-reads `pipeline_run` and `pipeline_run_step` on
each scrape and sets gauges from them. This is the difference between "the step-duration metric
disappeared" and "the pipeline has not run", which are very different pages at 3am. The worker
*also* records its own histogram, which is the one with a distribution in it.

### 17.10 Publish latency is measured from 15:30 IST

`docs/09` §Observability asks for "publish latency (EOD close → data live)" and does not define
"close". NSE's continuous session ends at 15:30 IST, so that is the zero — not the ~18:00–19:00
IST EOD-file settle window `docs/03` mentions, which is when the *inputs* arrive rather than when
the day ended. The 20:15 IST SLO in `docs/11` is therefore a 4h45m budget, which is the number the
Grafana panel is scaled to.

### 17.11 Sentry's own tracing is off

`traces_sample_rate=0.0`. `docs/02` sends traces to Tempo; two tracers sampling the same request
independently produce two disagreeing pictures of it. The active OpenTelemetry trace id is
attached to every Sentry event as the `otel.trace_id` tag instead, so an issue links to a trace.
Events are additionally scrubbed by the same redactor the logs use
(`decile_api.logging.redact_text`) before they leave the process, on top of `send_default_pii=False`.

### 17.12 The web app initialises Sentry server-side only

`@sentry/nextjs` is wired through `instrumentation.ts`'s `register()` and `onRequestError` hooks
and **not** through `withSentryConfig`, and there is no `instrumentation-client.ts`. The client
bundle is therefore byte-identical to what it was before Prompt 17, which matters because
`docs/11` budgets the screens route at 250 KB gzip and it is already at ~194 KB. The consequence
is stated plainly: **browser exceptions are not reported.** Only server components, route handlers
and server actions are.

### 17.13 The restore drill takes its own backup; it does not read a real one

`PROMPTS.md` §6 asks for "a monthly restore-drill CI job that provisions a scratch database from
the latest backup". The overnight rules forbid contacting real infrastructure, and CI has no R2
credentials, so `.github/workflows/restore-drill.yml` runs `infra/backup/pg_backup.sh` against a
freshly migrated and seeded database, restores that dump into a scratch database with
`infra/backup/restore.sh`, and runs `decile_api.integrity` against the result. That exercises the
backup script, the restore script and the assertions — everything except "the object in R2 is
readable", which only a deployment can prove. The workflow says so in a comment and the runbook
says so in prose.

### 17.14 What is **not** done

* **No runbook has been executed against staging.** There is no staging environment in this
  repository and the overnight rules forbid standing one up. Prompt 17's third acceptance
  criterion is therefore **not met**, and each runbook carries a "Verified against" line reading
  `NOT YET — written from the code, not from a real incident`.
* **Grafana dashboards are JSON files, not a provisioned Grafana.** `infra/grafana/` holds the
  dashboard definitions and a provisioning file. Nothing has rendered them.
* **The alert rules have never fired in anger.** `infra/prometheus/alerts.yml` is written against
  the metric names `decile_api.metrics` exposes, and a test asserts every metric an alert
  references is one this codebase actually publishes — but no Prometheus has evaluated them.
* **`/metrics` reports queue depth by `LLEN` on the queue name.** That is how Celery's Redis
  transport stores a queue, and it is transport-specific. A deployment that moves to RabbitMQ
  gets a silent zero, not an error.
* **Browser exceptions are unreported** (§17.12).
* **The worker's Prometheus endpoint is one port per process.** With a prefork pool, only the
  first child to bind serves; the rest are silent. `PROMETHEUS_MULTIPROC_DIR` is the supported
  answer and is not configured here.

### 17.15 The app shell now makes one extra `GET /me` per authenticated render

`apps/web/src/app/(app)/layout.tsx` reads `/me` so the user menu can render the `/admin` link
from `is_staff` — server truth, the same source every entitlement gate reads. It runs inside the
existing `Promise.all` beside `auth()` and `cookies()`, so the wall-clock cost is the *maximum* of
the three rather than their sum, and `fetchMe` short-circuits without a request when there is no
session cookie.

The measured budgets are unaffected: `docs/11`'s factsheet TTFB (< 300 ms) and LCP (< 1.8 s) are
asserted by `apps/web/e2e/performance.spec.ts`, which visits **signed out** — so the extra call is
not on the path it measures. It *is* on the path every signed-in page render takes, and nothing
measures that. If it becomes a problem the fix is to carry `is_staff` on the Auth.js session
rather than to drop the link, and the cost of that is staleness: a staff bit revoked mid-session
would keep rendering the link until the session refreshed. The API decides again on every
`/admin/*` request either way.

---

## Prompt 18 — Content, marketing, and legal pages (2026-08-21)

Every choice below was made under ambiguity — the specification is silent, or two parts of it
pull in different directions. Each records what was chosen and what it costs.

### 18.1 MDX is a new dependency, and `docs/02` does not name one

`docs/02-tech-stack-adr.md` locks the stack and says nothing about a content layer. It mentions
the blog exactly once, as a reason to keep SSR: *"Loses SSR/SEO for the public pages (pricing,
blog, instrument pages are exactly the kind of long-tail SEO surface that acquires users…)"*.

PROMPTS.md Prompt 18 §2 asks for "`/blog` with MDX posts and RSS", which names the format. Four
packages were added — `@next/mdx`, `@mdx-js/loader`, `@mdx-js/react`, `@types/mdx` — pinned to the
Next 15 line. They are **build-time only**: MDX compiles to React server components, so the client
bundle is unchanged and the 250 KB screens-route budget (Prompt 16) is untouched.

No remark or rehype plugins are configured. A plugin chain is another thing that can break a build,
and neither three posts nor four legal documents need one.

**RSS took no dependency.** `apps/web/src/app/(marketing)/blog/rss.xml/route.ts` writes RSS 2.0 by
hand — nine elements, `force-static`, RFC 822 dates anchored at midnight IST rather than UTC so a
post is not dated to the previous day for half the world.

### 18.2 Static generation and the strict CSP cannot both hold on the same route

This is the hardest trade in the module and it is a genuine conflict inside `docs/`.

* `docs/08` §Routes: "`/` | marketing landing (SSG)" and "`/pricing`, `/faq`, `/about`,
  `/blog/*`, legal | SSG".
* `docs/11` §Security: "Strict CSP (`default-src 'self'`)".
* PROMPTS.md Prompt 18, acceptance criterion 1: "All pages are statically generated…"

Prompt 12 implemented the strict CSP as a **per-request nonce** generated in `src/middleware.ts`.
A nonce cannot appear in a statically prerendered page: the HTML is one cached file served to
everybody, so the nonce in this request's header would not match the one baked into the file — and
under `strict-dynamic` that blocks the page's own bootstrap, not merely one inline script. Next
also inlines its RSC flight payload as `<script>self.__next_f.push(…)</script>`, which is
content-dependent and therefore cannot be hashed either.

The two are mutually exclusive per route. **The resolution is two policies, chosen by path:**

| Routes | `script-src` |
|---|---|
| `/`, `/faq`, `/about`, `/support`, `/blog/*`, `/december-2026-update`, the four legal pages | `'self' 'unsafe-inline'` |
| everything else | `'self' 'nonce-…' 'strict-dynamic'` |

Every other directive is identical — `default-src 'self'`, `object-src 'none'`, `base-uri 'self'`,
`form-action 'self'`, `frame-ancestors 'none'`, the same `connect-src`.

**Why this is acceptable on exactly those routes.** They render no session, accept no
user-generated content into the DOM, and read nothing from the request. There is no injection
source for `'unsafe-inline'` to amplify. The authenticated surface — where an injection would
actually be worth something — is unchanged.

**What it costs.** If a marketing page ever renders untrusted input (a comment form, a
search-term echo), the relaxation becomes a real XSS amplifier and this decision must be revisited.
`isStaticPublicPath` in `apps/web/src/lib/marketing/routes.ts` is the one list that decides, and
`src/lib/__tests__/marketing-content.test.ts` asserts what is and is not in it.

**A consequential refactor.** `apps/web/src/app/layout.tsx` used to read `headers()` for the nonce.
A `headers()` call in the *root* layout opts every route in the app into dynamic rendering, which
made the criterion unsatisfiable no matter what the middleware did. The nonce is now read by
`(app)/layout.tsx` and `(auth)/layout.tsx` — both already dynamic because they read the session
cookie — and `<Providers>` moved down with it. The root layout now reads nothing per request.

### 18.3 "All pages" is read as "all pages Prompt 18 delivers", not literally all pages

Taken literally, criterion 1 would require `/dashboard`, `/screens`, `/portfolios` and every
account page to be statically generated. They cannot be: they render one caller's data.
`docs/08` §Routes marks them "RSC", not SSG, and doing it would be a data-leak bug rather than an
optimisation.

The criterion is therefore read against `docs/08`'s own SSG row. **`/pricing` is the one page in
that row that is still dynamic**, and deliberately: Prompt 13 built it to render the caller's
current plan and entitlement state, which needs the session. Its prices already come from
`GET /plans`. Making it static would mean either dropping "you are on this plan" or moving it to
the client, and neither is worth a Lighthouse point.

`apps/web/e2e/static-generation.spec.ts` asserts the split against `.next/prerender-manifest.json`
— including, as a positive assertion, that `/pricing` and the three data routes are *not* static,
so a future change that makes one of them static is deliberate rather than silent.

### 18.4 The FAQ's question set is reconstructed, not observed

Prompt 18 §2 asks for "`/faq` with **the reference product's question set** answered for our
product". `docs/01` §1 records that the reference product has a `/faq` route and captures nothing
that is on it. Nothing else in the bundle does either.

Inventing twenty questions and attributing them to the reference product would be putting words in
a competitor's mouth in our own repository. Instead every question in
`apps/web/src/lib/marketing/faq.ts` is derived from something `docs/01` observed **directly** — the
universe list (§2.1), the sentinel conventions (§2.4–§2.6), the multi-factor ranking algorithm
quoted verbatim from the site's own documentation (§2.12), "historical data is available from
1 Nov 2024" (§2.13), the gated feature list and the SEBI disclaimer (§1) — plus the questions this
build's own open items make unavoidable.

Three answers say a thing does not work: the empty P/E column, the history start date, and the
zero risk-free rate in backtest Sharpe. A test asserts all three are still mentioned.

### 18.5 `POST /support` is a new endpoint `docs/07` does not describe

Prompt 18 §2 asks for "`/support` with a contact form". A form needs a destination, and a form with
no destination is a lie told in HTML.

`docs/07` defines no such endpoint. The alternatives were a `mailto:` link (not a form), a Next
server action calling a mail provider directly (puts the API key in the web app, which `docs/11`
§Security puts in the platform's secret store behind the service that already holds it), or a new
API endpoint. The endpoint won.

* Unauthenticated, because someone who cannot sign in is exactly the person who most needs support.
* **Not an open relay:** there is no recipient parameter. The message can only go to
  `DECILE_SUPPORT_EMAIL`, and the *subject* carries only a value from a closed `Literal` set,
  because a subject line is a header and free text there is an injection surface. A test submits a
  name containing `\r\nBcc:` and asserts the subject is clean.
* **Stores nothing.** `docs/04` has no support-ticket table and adding one would be a migration in
  a content module. The consequence is that a failed delivery loses the message — so, unlike the
  auth endpoints (which swallow send failures to avoid becoming a membership oracle), this one
  answers **500** rather than claiming a send that did not happen.
* Metered by the existing anonymous limiter (10/min per address, `docs/07` §Conventions). No
  separate bucket.

A validation failure answers **400 `invalid-screen-definition`**, not 422, because
`decile_api.app` renders every `RequestValidationError` through docs/07's one validation problem
type whatever the endpoint. The name reads oddly on a contact form; the shape is the documented
one, and changing the handler for one endpoint would be worse.

`Message` gained a `reply_to` field so the operator's copy replies to the sender rather than to the
no-reply address. Exactly one template sets it.

### 18.6 The banned-phrase lint is negation-aware, because a literal list cannot pass

Prompt 18's third acceptance criterion names four phrases: *"guaranteed returns", "buy now",
"recommendation", "advice"*. Two of the four appear in copy `docs/11` **requires** us to publish —
the `<Disclaimer/>` sentence is "…not investment **advice**, and no output is a
**recommendation** to buy or sell any security."

A substring ban would fail on the disclaimer itself, and the only way to keep it green would be to
exempt the very files the rule exists to police. So `src/lib/__tests__/copy-lint.test.ts` splits
the list in two:

* **Always banned**, negation or not: "guaranteed return", "assured return", "sure shot",
  "multibagger", "buy now", "we recommend", "will outperform", "beat the market", and nine more.
  There is no sentence containing these that this product should publish.
* **Negation-only**: "advice", "adviser", "advisory", "recommendation", "target price". Each
  occurrence must sit in a sentence containing a negator.

Two carve-outs, both narrow and both tested:

1. **A pointer to a registered adviser.** "Consider taking advice from a SEBI-registered investment
   adviser" is the opposite of a claim, and is the sentence a regulator would want. The clause must
   contain "SEBI-registered", which cannot describe us, because we are not.
2. **A question.** The FAQ's own heading is "Is this investment advice?", answered "No." The
   sentence must *end* in a question mark; the always-banned list ignores this carve-out entirely,
   so "Want guaranteed returns?" is still a failure.

Negation is scoped to the **sentence**, not to a character window. The disclaimer's §1 reads
"…is **not** registered … not as an investment **adviser** under the SEBI (Investment Advisers)
Regulations, 2013, not as a research analyst…", where the governing "not" is a hundred characters
and a line break from the third occurrence. No fixed window is both wide enough for that and
narrow enough to mean anything.

Two tests assert the lint *would* fail — one on an un-negated claim, one on a question-mark
exemption that should not apply — so a green run is not vacuous.

### 18.7 The scanned surface is the content routes, not the whole app

The copy lint scans `(marketing)`, `src/content`, `components/marketing`, `components/consent`,
`lib/marketing` and the disclaimer component. **Not** the screener's form labels, the API error
catalogue, or the admin surface: those are operational vocabulary, and a false positive from an
error string would make the useful part of the test noise. Widening the list is one line.

### 18.8 The landing page shows a real screen run, and shows nothing when it cannot

Prompt 18 §1 asks for "a live sample screen preview". It is a real `POST /screens/preview` of the
definition `docs/13` captured — the same one seeded as example screen `exmpl0000001` — revalidated
hourly and invalidated by `POST /api/revalidate` at the nightly publish. Preview rather than
`{id}/run` because a marketing page that wrote a `screen_run` audit row on every revalidation would
be filling an audit table with traffic.

**When the API does not answer, the slot says so.** The alternative — rendering the committed
271-row reference export as canned rows — would put real-looking, months-stale numbers on a
marketing page with nothing on screen to say they were not live. `docs/14` §Tone: credibility
"comes from showing its work".

### 18.9 The December 2026 announcement takes its price table as a prop

`docs/01` §1 records the reference product's `/december-2026-update` as a "Roadmap / pricing-change
announcement page", and Prompt 18 §2 asks for "an announcement page pattern like" it.

The prose is MDX. The **price table is not**: Prompt 13's fourth acceptance criterion ("No price or
entitlement is hard-coded in the web app; all read from the API") applies to an announcement
*about* prices at least as strongly as to the checkout page. MDX compiles to a component that takes
props, so the document contains `{props.priceTable}` and the route renders the table from
`GET /plans` (`price_inr` and `price_from_dec_2026`). An operator who reprices a plan and forgets
this page finds it already correct.

The pattern, stated so the next one is a copy: a dated MDX document at a **permanent,
self-describing URL** with a route of its own; the app shell's `AnnouncementBanner` points at it;
and it is never edited retroactively — corrections are appended and dated.

### 18.10 The legal drafts carry their warning in the source and never on the page

Prompt 18 §3: "Mark them clearly as DRAFTS REQUIRING LEGAL REVIEW at the top of each file in the
repo, **not on the rendered page**."

The marker is an MDX comment (`{/* … */}`), which compiles to nothing.
`src/lib/__tests__/legal-drafts.test.ts` asserts **both** halves — that every document opens with
it, and that no document renders it. The second is the one worth testing: a "DRAFT" watermark on a
live terms page is worse than no terms page, because it invites a customer to argue that nothing
was agreed.

A further test asserts every draft still contains `[BRACKETED]` placeholders. A legal document that
invented a GSTIN or a registered address would be worse than one that admits it does not know them,
because the invented one reads as authoritative.
`apps/web/src/content/legal/DRAFT-NOTICE.md` lists the eight decisions a reviewer must make; the
test counts them, so deleting one fails.

### 18.11 The consent banner defaults to essential-only, and the "analytics" category is empty

Prompt 18 §5. `apps/web/src/lib/consent/state.ts` treats *no stored choice* and *an unparseable
choice* identically: essential-only. A corrupt cookie must never decay into a permissive record.

The banner offers "Essential only" and "Allow all". Dismissing it without choosing leaves
essential-only. There is no pre-ticked box and no implied consent, which is what the DPDP Act's
"free, specific, informed and unambiguous" means in practice.

**The `analytics` category presently gates nothing**, because this product ships no analytics tag.
The banner says so rather than offering a toggle that controls nothing. If one is ever added it is
gated on that flag.

The banner is `position: fixed` and renders `null` until hydration, so it costs no layout shift —
Prompt 8's CLS criterion is measured across the whole app and a banner in the document flow would
break it everywhere at once. It uses `useSyncExternalStore` (via `useIsMounted`) rather than
`useState` + `useEffect`, which `react-hooks/set-state-in-effect` rejects.

### 18.12 What is not done, and what a human must decide

* **No lawyer has read any of the four legal drafts.** They are drafts written by engineers from
  `docs/11` and Prompt 18 §3. `DRAFT-NOTICE.md` §1–§8 is the list of what must be settled — the
  supplier's legal identity and GSTIN, the GST rate and SAC code, whether prices are GST-inclusive,
  the Forever plan's consumer-law treatment, the refund terms, the data-licensing paragraphs, the
  DPDP specifics, and the governing-law venue.
* **No grievance officer and no Data Protection Officer has been appointed.** Both are
  `[BRACKETED]` in the privacy policy, and the Consumer Protection (E-Commerce) Rules, 2020 require
  the first before taking payments.
* **The server-log retention period is `[RETENTION PERIOD — NOT YET SET]`.** Nothing in the
  codebase expires a log line.
* **The support form has never delivered a real email.** The suite drives a recording transport and
  the local stack points at mailpit. The Resend path is the same one the auth emails use and has
  the same standing: written from the documented API, never exercised against the live service.
* **No page has been checked by anything other than Lighthouse and axe.** The copy lint catches
  banned phrases; it cannot catch a claim phrased in words nobody thought to ban.

### 18.13 The landing page does not prefetch the instrument links in its sample table

Found by measurement, not by review. `/instruments/[symbol]` renders dynamically and calls the
API; Next prefetches every `<Link>` that enters the viewport, so the landing page's eight sample
rows fired eight server renders and eight API round-trips the moment it painted — on the one page
whose Lighthouse performance score is an acceptance criterion, for links most visitors never click.

The symptom was variance rather than a number: five standalone runs scored 96, 97, 97, 97, 99, and
one run at the end of a saturated ten-minute suite scored **94** and failed. With
`prefetch={false}` on those eight links, five consecutive runs score **97, 97, 97, 97, 97**. They
still prefetch on hover, which is when a visitor has shown they might click.

Nothing else on the page changed, and no other `<Link>` in the app was touched: inside the
application, prefetching a factsheet is the behaviour that makes the table feel instant.

---

## Prompt 19 — Test hardening and data-correctness audit (2026-08-21)

Prompt 19 adds no features. Every decision below is about what can honestly be *asserted* with the
data this repository contains, and what has to be recorded as unresolved instead.

### 19.1 The coverage gate is per package, and it refuses to run without a database

Prompt 19 §1 asks for ">= 90% on packages/core, >= 80% on services/api. Fail CI below those
thresholds." `pytest-cov`'s `--cov-fail-under` is one number over everything measured, which hides
the case that matters: `decile_core` sliding from 92% to 84% while `decile_worker` climbs, leaving
the total flat. So the gate is `tools/coverage_gate.py`, one threshold per package, with
`decile_providers` and `decile_worker` measured and printed but not gated — Prompt 19 names only
two, and an ungated number nobody can see is an ungated number that only falls.

The gate **exits 2 rather than passing** when `DECILE_TEST_DATABASE_URL` is unset. Most of
`services/api` is exercised by `db`-marked tests; without a database they skip and `decile_api`
measures far below 80% — not because coverage is worse but because the suite did not run. A gate
that reports a number meaning something different from CI's number is worse than no gate.

As measured on 2026-08-21 against a live PostgreSQL 16 + TimescaleDB and Redis:
`decile_core` **92.34%**, `decile_api` **81.70%**, `decile_providers` 89.51%,
`decile_worker` 76.91%. Both gated thresholds are met **with less than two points of headroom on
`services/api`**, which is the number to watch.

### 19.2 `compute_factors` gained an `apply_precision` flag, for one caller

Prompt 19 §3 requires the Polars engine and the pandas oracle to agree "to 4 decimal places".
`compute_factors` rounds to storage precision on the way out (CLAUDE.md house rule 8 — returns at
2 dp), so a 4-dp comparison against it is a comparison of two roundings and proves almost nothing.

The alternative to a flag was for the harness to re-implement the engine's pipeline minus the
rounding call, which is a copy of the thing under test. So `compute_factors(..., apply_precision=
False)` exists, defaults to `True`, and `packages/core/tests/test_factor_crossvalidation.py`
asserts that **no file under any `src/` tree mentions it** — a write path that skipped rounding
would break the CSV/API/UI tie that house rule 8 exists to protect. The same file also asserts the
stronger property the flag makes checkable: rounded, the two implementations produce the identical
stored row.

### 19.3 The cross-validation corpus is 25 real symbols on synthetic price paths

Prompt 19 §3 says "pick 25 real instruments". The symbols, names, series and closing prices on
2026-08-18 are real — they come from the docs/13 export by way of the Prompt 2 provider fixtures —
but **every bar before that date is a seeded random walk** (`tests/fixtures/providers/
PROVENANCE.md`). There is no real price history in this repository and the suite is network-blocked.

That does not weaken the harness: cross-validation asks whether two independent implementations
agree on the *same* input, not whether the input is the market. Three branches a random walk never
reaches are injected deliberately (`factor_corpus._inject_edge_cases`): the docs/05 §12 band
heuristic, the §13 turnover fallback, and locked days at fixed positions. What it cannot do is say
anything about whether docs/05 matches the reference product — that is §19.5's job.

`vol_avg_*` and `median_vol_12m` are compared at a relative 1e-12 rather than an absolute 1e-4,
because they are rupee sums running to 10^10 where a float64 ULP is already ~2e-6 and a 252-term
summation in two different orders cannot agree to 1e-4. In the units that matter that tolerance is
about a thousand times tighter than 4 dp, not looser.

### 19.4 Mutation testing is not mutmut, and the attempt is on the record

Prompt 19 §6 says "mutmut or cosmic-ray". mutmut 3.7 was installed and configured first. It works
by copying `source_paths` into a `mutants/` directory and running pytest from there; this
repository is a **uv workspace of four editable, src-layout packages**, so `decile_core` resolves
through `.venv/.../_editable_impl_decile_core.pth` — an absolute path to the real
`packages/core/src`. Under `mutmut run` the tests import the unmutated module, mutmut attributes no
test to any mutant, and it stops with "we could not find any test case for any mutant". Prepending
the mutant tree to `PYTHONPATH` does not help, because mutmut re-execs pytest itself. `cosmic-ray`
has the same shape of problem. The dependency was removed rather than left in place broken.

`tools/mutation.py` does what mutmut's model does in the way this layout supports: it copies
`decile_core` to a temporary directory, mutates one file in the copy per mutant, and runs a scoped
pytest with that directory first on `PYTHONPATH` so the copy shadows the editable install. **The
real source tree is never written to** — which matters for a tool normally run unattended. Six
operators, each chosen for a bug class docs/05 or docs/06 would actually suffer; docstrings are
excluded. The report is committed at `reconciliation/MUTANTS.md` and survivors with no written
justification are a non-zero exit.

### 19.5 The 12-1 momentum definition CANNOT be resolved, and candidate A is inconsistent

docs/05 §8 asks Prompt 19 to settle the skip-month definition "empirically". **It cannot be settled
from this bundle**, for a structural reason:

* the 93-column reference export carries **no skip-month column at all** — the reference product
  offers 12-1 and 12-2 as *sort keys* (docs/01 §3) and never exports the values, so all 271 rows
  contribute nothing to the question;
* the only published values anywhere in `docs/` are docs/05 §8's own CUPID figures, 608.37 and
  852.21, observed on 19 Aug 2026.

What the arithmetic on those two numbers does establish (`decile_core.reconcile.
skip_month_evidence`, asserted in `packages/core/tests/test_reconcile.py`):

**Candidate A — docs/05's primary, `P_{t-21} / P_{t-252}` — is inconsistent with the published
figure.** It is fully determined by CUPID's other published numbers that day:
`(1 + 753.00%) / (1 + 37.29%) - 1 = 521.31%`, against a published **608.37%**. The gap is 87.06
percentage points, 16.7% of the value. The 21-vs-22 and 247-vs-252 bar mismatches between §8's bar
offsets and §1's calendar windows are worth roughly six bars of a name compounding at ~0.86% a day
— about 5%, a quarter of the gap.

**Candidate B — `P_{t-21} / P_{t-273}` — is not confirmed.** It is under-determined: it implies a
13-month return of ~873% for CUPID, which nothing in the bundle contradicts and nothing supports.
"A is refuted" is not "B is correct", and shipping B on that basis would be exactly the confident
wrongness docs/05 warns about.

**Decision: the engine keeps shipping candidate A** — `SkipMonthDefinition.SKIP_END`, the
definition docs/05 §8 instructs us to implement — and the switch to B stays a constructor argument
(`decile_core.momentum.SkipMonthConfig`). Resolving it needs CUPID's real adjusted closes for ~294
trading days, which arrives with the first production backfill and not before.

**docs/05 §8 therefore still says "INFERRED, calibrate", and that is correct.** Prompt 19 asks to
"update docs/05 with the resolved definitions"; the honest update is that one of the two is now
*refuted* and neither is *resolved*. The overnight run may not edit `docs/`, so this section is the
update, and docs/05 §8 needs a hand-edit in the morning to point at it.

### 19.6 Circuit detection is likewise unresolvable, with a bound rather than a validation

docs/05 §12's rule has two branches and the snapshot can distinguish neither: telling them apart
needs, per bar, high / low / close_raw / previous close / the day's NSE band, and the export is one
day of counts. What the counts do say is a bound: **56 of 271 rows** record at least one hit in the
last year and the largest is 16 days out of 247. A rule that fired on every 20% up-day would
produce far more than that on a momentum screen, so the reference product's rule is at least as
strict as ours. That is a bound, not a validation. The engine keeps §12's rule as written, with
the published-band branch preferred and `CircuitMethod` recorded per row.

The one thing the report *can* prove about the published counts is that they are nested across
windows and bounded by the window length — 2,439 assertions, all clean.

### 19.7 Reconciliation tolerances are derived from storage precision, never picked

Every published column is rounded (docs/13 §4), so a relationship between two of them cannot hold
exactly. Each check therefore propagates the half-ULP of each input through its own formula, per
cell. The sharpe identity's tolerance is not "0.005" but
`0.005 + 0.005/(vol x 100) + |sharpe| x 5e-9/vol` — about 0.00525 for a 20%-volatility name. That
distinction is not cosmetic: a flat 0.005 flags **11 of 1,355** sharpe cells, every one of them
inside the derived tolerance, and docs/05 §3 itself records the maximum error as 0.0051.

Two checks needed their tolerance corrected during the build, both in the same direction —
initially too tight, and both fixed by deriving rather than guessing. `blend_ordering` now
reproduces docs/13 §2's figure exactly: **48 inversions, worst 0.0075**, all inside the two
half-ULPs that 2-dp component rounding allows.

`turnover_is_exchange_value` declines to answer below 30 rows rather than manufacturing a
disagreement: docs/13 §2 states its evidence as a mean and a standard deviation
(measured here as **0.999905 ± 0.008363** over 271 rows, against docs/13's "0.9999 ± 0.008"), and
`--symbols CUPID` is a sample of one.

### 19.8 The ten critical journeys are a registry, not ten new browser tests

Prompt 19 §5 asks for "a Playwright e2e suite covering the ten critical user journeys". Nine of
the ten were already walked end to end by the specs Prompts 8-15 delivered. Rewriting them in a new
file would roughly double the browser suite's wall clock — against Prompt 19's own "CI ... completes
in under 15 minutes" — and leave two copies to drift apart.

`apps/web/e2e/critical-journeys.spec.ts` therefore *names* the ten in one place, each with the spec
file and the exact test title that walks it, and a test in that file reads those spec files and
fails if a named test has been renamed or deleted. The tenth — running a backtest — had no coverage
before this module and is walked in full here.

**What the backtest journey can assert is limited, and the test says so.** `POST /backtests`
publishes to Celery and the browser suite starts no worker, so the run stays `queued`. Even with a
worker it would fail: the seeded database holds one trading day of *results* and no price history.
So the journey asserted is the one the repository can deliver — configure, queue, land on a page
that states the run's real state — and everything past "queued" waits on a backfill.

### 19.9 What is not done, and what a human must look at

* **docs/05 §8 and §12 still say INFERRED and both still need a hand-edit** to point at §19.5 and
  §19.6. The resolved-definitions update Prompt 19 asks for is "one candidate refuted, neither
  resolved", and it lives here because the overnight run may not edit `docs/`.
* **The Playwright suite was not executed as part of this module.** The ten-journey registry and
  the new backtest journey type-check (`tsc --noEmit` clean) but no browser ran; the browser suite
  needs `make up`, a `decile_e2e` database, a production `next build` and a chromium install, and
  the module's time budget went to the Python deliverables. **The backtest journey has therefore
  never passed.**
* **CI's wall clock is unmeasured, and until this module CI could not run at all.**
  `.github/workflows/ci.yml` **was not valid YAML** and had not been since Prompt 16: the step
  name `Client-JS budget (docs/11: screens route < 250 KB gzip)` is an unquoted scalar containing
  `: `, which YAML reads as a nested mapping. GitHub Actions refuses to run a file it cannot
  parse, so every job in it would have been skipped and nothing would have said so. Prompt 19 §5
  quotes "run against a seeded database in CI" and the acceptance criterion says "CI is green"; a
  workflow that never parsed is neither green nor red. It is fixed (one pair of quotes), and
  `services/api/tests/test_ci_workflows.py` now asserts every workflow parses and still runs each
  numbered prompt's gate. **No GitHub Actions run has ever happened**, so the under-15-minutes
  criterion has no measurement behind it at all. Locally, the Python suite with a database
  attached takes tens of minutes — it was never timed on an idle machine, because every
  measurement this module took was competing with the mutation harness.
* **`services/api/tests/test_load.py::test_p95_stays_under_four_hundred_milliseconds_with_no_errors`
  fails on this machine** with a database attached, and did so before Prompt 19 touched anything.
  It is a 50-concurrent in-process load benchmark; it is not part of `make test` (it is `db`-marked
  and skips without `DECILE_TEST_DATABASE_URL`). Not investigated — out of Prompt 19's scope, and
  recorded rather than hidden.

### 19.10 Mutation testing: 148/191 killed, 6 justified, 37 left standing in the screener

The score is **77.5%**, not the 100% an earlier run reported. That earlier 100% was false and the
harness's own control pass is what proved it — see §19.4. The honest breakdown:

**Twelve mutants were killed by writing tests**, each pointing at a guard nothing exercised. The
two worth knowing about beyond their own line:

* **`turnover_source` had no assertion anywhere in `packages/core`.** docs/05 §13 does not only
  say which field to prefer, it says *"record which was used"* — the provenance column exists so a
  liquidity figure can be traced to its source. `vol_day_val` was covered by the cross-validation;
  the column beside it was not. Both mutants on its expression survived every other test.
* **Wilder's seeding could not be observed by any existing test.** docs/05 §5 is unusually
  specific about the seed ("a simple mean over the first N observations"), and
  `decile_core.factors` carries a paragraph on why Polars' `ewm_mean` cannot be substituted. Every
  test ran 765 bars against windows of at most 247, where the seed row is hundreds of steps behind
  the row being read. Two new cases — a history of exactly `N + 1` bars, and a calendar whose
  1-month window resolves to a single trading day — put the seed row *on* the bar under
  inspection, which is the only arrangement in which the off-by-one is visible at all.

**Six survivors are equivalent mutants** and are justified in
`reconciliation/mutant-justifications.json`: two `slots=True` dataclass flags, the
`ZERO_VOLATILITY_EPSILON` strict-vs-non-strict boundary (they differ for exactly one representable
float), an RSI empty-frame guard made redundant by the `len(dates) < 2` guard eight lines below
it, and the two `np.where(changes > 0, ...)` comparisons whose branches both yield 0.0 at the
boundary.

**37 survivors are unjustified, 34 of them in `screener.py`, and Prompt 19 §6 is therefore not
fully met.** `make mutants` exits non-zero and names each one. They are not known bugs; they are
lines `test_screener.py` executes without asserting anything about — the three row-limit constants
(`top_50`, `top_100`, `MAX_RESULT_ROWS = 4000`), the P/E range filter's inclusivity at both ends,
two branches of `validate_definition`, the 1-based filter position in error messages, nine
dataclass flags, and some mask comparisons in the top-risk exclusion clauses. Writing those tests
is straightforward and was not done; recording the gap precisely is more useful than a justification
file full of "probably fine".

### 19.11 One incidental find: `TOP_RISK_FLAG_PERCENTILE` is declared twice

`decile_core.universes` and `decile_core.factors` each define it as `0.10`, and
`decile_worker.tasks.factors` imports the second. Nothing noticed if they drifted, which is why
mutating one of them survived. A test now asserts they agree — a patch over a duplication that
should simply be removed, by having one module import the other. Not done here: it changes a
public name's home, which is a Prompt 5 decision rather than a Prompt 19 one.

---

## Prompt 20 — Public API, alerts, and API keys (2026-08-21)

Prompt 20 opens with "Read docs/07 and docs/11 'Compliance' first — especially the data-licensing
constraint." That constraint decides the shape of the whole module, so it is §20.1 and everything
else hangs off it.

### 20.1 The public API has two locks, and one of them is a source constant

docs/11 §"Compliance & legal (India)":

> **Data licensing:** broker-sourced market data is licensed for the licensee's own use. Serve
> derived analytics; do not expose a raw-bar API to third parties without written clearance. Get a
> written data-redistribution opinion before enabling the public API tier.

Prompt 20 §2 asks for the feature to be "behind a flag that stays OFF until the data-redistribution
review in docs/11 is signed off; make that dependency explicit in the code and the admin UI."

A single environment variable would satisfy the letter and not the intent: an operator with shell
access could turn on data redistribution at 2am with no reviewer, no diff and no record. So there
are two:

1. `DECILE_PUBLIC_API_ENABLED` — a setting, `False` in `Settings`, in `.env.example`, and in
   `infra/docker/compose.yml`. This is the operational off switch.
2. `decile_core.public_api.DATA_REDISTRIBUTION_REVIEW.signed_off` — a **source constant**, `False`,
   carrying docs/11's sentence verbatim plus empty `opinion_reference` / `signed_off_by` /
   `signed_off_on` fields. This is the compliance gate.

`decile_api.routers.public.is_enabled` reads both; `create_app` mounts the router only if both are
open. When shut, there is no handler, nothing in the OpenAPI document and nothing in the generated
TypeScript client — a stronger statement than a route that answers 403. A caller gets a plain 404,
because "we do not have a public tier" and "we have one and it is switched off" are different
statements and only the first is currently true.

`packages/core/tests/test_public_api_policy.py::test_the_data_redistribution_review_is_not_signed_off`
fails the build if anyone flips it. **Turning the public API on means editing a Python constant and
deleting a test assertion, in a reviewable commit.** That is the closest thing a codebase has to a
signature.

The admin surface renders the same object: `GET /api/v1/admin/public-api` and
`/admin/public-api` in the web app both state the requirement and that it is outstanding.

### 20.2 "Never raw vendor bars" is enforced as "no field denominated in rupees per share"

"Derived analytics, never raw vendor bars" needs an operational test, because every derived number
is downstream of a bar. The rule in `decile_core.public_api.is_public_column` is:

> a public field may not have `FactorUnit.PRICE`, and may not be named in `RAW_BAR_FIELDS`.

That withholds `close`, `close_raw`, `high_1y`, `high_ath` and the four moving averages, and serves
returns, sharpe returns, RSI, volatility, beta, marketcap, P/E, away-from-high percentages, circuit
counts, median traded value and the series code. Every column of docs/01 §4's picker lands in
exactly one of the two lists, asserted, so a column added later cannot become public by nobody
thinking about it.

The moving averages are the interesting exclusion: `ma_20` is a *derived* statistic by any ordinary
reading, and it was excluded anyway. See §20.4.

The public screen endpoint additionally refuses to run a screen whose **sorting factor** is a
withheld column, because docs/06 always projects `sorting_factor` alongside the requested columns —
a screen sorted by `close_raw` would put an exchange print through a door the column whitelist does
not cover.

### 20.3 An API key is verified from the row on every request, with no cache

Prompt 20's first acceptance criterion is "a revoked key is rejected within one second (no
cached-auth window)". `decile_api.api_keys.authenticate` therefore reads `api_key` on every request:
one unique-index probe on the clear-text `prefix` plus a constant-time digest comparison, cheaper
than the JWT verification every other authenticated route already pays. There is no TTL to tune and
no invalidation to get wrong.

A key is `dk_<12 hex prefix>_<43 url-safe chars>`. The prefix is stored in the clear because it is
what the lookup indexes on and what the UI displays; the secret is SHA-256 at rest, like every other
high-entropy token in this codebase (`decile_api.security`), and exists in plaintext exactly once —
in the response that created it.

### 20.4 The residual risk the whitelist does not close

`ma_20(t)*20 − ma_20(t−1)*20 = close(t) − close(t−20)`. A caller who sweeps `as_of` across every
trading day can difference a moving-average series and recover exact daily closes. That is why the
moving averages are withheld despite being derived: the whitelist is drawn at "no per-share price,
and nothing one subtraction away from one".

It is still not airtight. `away_high_1y` is a percentage distance from a rolling high; a determined
caller with enough dates and enough algebra can extract information about the price path from
several of the served series jointly. **No analysis has been done to bound what is recoverable.**
The mitigations that exist are the day-granularity cache header, the per-key rate limit, the
terms-of-use clause that prohibits reconstruction, and — decisively — the fact that the feature
ships **off**. A reviewer should treat the whitelist as a starting position for the legal opinion,
not as a solved problem.

### 20.5 Rotation revokes the old key immediately, with no grace window

An overlap would be friendlier to an integration mid-deploy. It is also indistinguishable, from
outside, from a key that was not really rotated — the wrong default for a credential whose whole
rotation story exists because it may have leaked. An owner who wants an overlap creates a second
key, deploys it, then revokes the first: two explicit steps rather than one implicit window.

### 20.6 Usage is counted with a write on the request path

`api_key_usage_daily` is one `INSERT … ON CONFLICT DO UPDATE` per accepted request (plus a
`last_used_at` touch at most once a minute). That is a write per read, which is the honest cost of
the dashboard Prompt 20 §1 asks for at this scale. A Redis counter flushed periodically would be
cheaper and would lose the tail on a restart. **If the public API ever carries real volume this is
the first thing to move.**

### 20.7 New endpoints reuse docs/07's single 400

docs/07's error catalogue has exactly one 400 (`invalid-screen-definition`) and no general
bad-request type. `routers/portfolios.py` already reuses it for a malformed upload; `/keys`,
`/alerts` and `/webhook-endpoints` do the same, with a precise `detail` and an `errors[]` entry. A
client handling an undocumented `type` is worse off than one handling a documented type whose
`detail` says what went wrong. A scope a key does not carry answers **402**, not 403: docs/07 has no
`forbidden`, and "your credential does not include this" is what `payment-required` already means
on this service.

### 20.8 The interactive reference is Redoc from a CDN, with no Subresource Integrity hash

docs/02 locks the stack and names no documentation renderer. Redoc is FastAPI's own built-in choice
and needs no new dependency — a single `<script>` tag — and, decisively, it renders the
`x-codeSamples` vendor extension natively. That puts the curl / Python / TypeScript examples Prompt
20 §5 asks for **inside the OpenAPI document**, where they are machine-readable, versioned with the
spec and asserted by a test, rather than in hand-written HTML beside it.

The script URL is pinned by version in `DECILE_REDOC_SCRIPT_URL` and an air-gapped deployment can
repoint it at a self-hosted copy. There is **no `integrity` attribute**: computing an SRI hash means
fetching the file, and this repository's suite is network-blocked, so any hash committed here would
be one nobody had verified. Add one before the reference is served publicly.

The public document is a *filtered copy* of the service's single OpenAPI document rather than a
separate FastAPI sub-application, because a sub-app has its own dependency graph and would fall
outside the test harness's session override (`services/api/tests/api_helpers.running_app`).

### 20.9 The webhook sender does no SSRF protection

`routers/webhook_endpoints._check_url` refuses anything that is not an absolute `http(s)` URL with a
host, and refuses plain `http` in production. It does **not** resolve the hostname, and does not
block loopback, link-local or RFC 1918 destinations. An authenticated user can therefore point an
endpoint at `http://169.254.169.254/…` and have the server POST a signed JSON body to it.

The body carries only symbols, ranks and a screen name, and the response is never returned to the
user — so this is a blind request, not an exfiltration channel — but it is still a real
server-side-request-forgery surface. Doing it properly means resolving the host at *attempt* time
and re-checking after redirects, which is a piece of machinery worth writing deliberately. **Not
written. The feature's unreleased state is what is containing it.**

### 20.10 A public screen response is capped at 500 rows

The product API allows 4,000 (docs/03 §"Request path"). A public tier is for looking things up; a
caller who wants the whole universe every night is asking for the dataset, which is exactly what
docs/11's licence does not permit us to serve.

### 20.11 The public API does not use the screen cache

`run_screen` returns cached *bytes* — the product API's full payload. A public response has to be
assembled field by field from the whitelist, and reading a cached blob and filtering it afterwards
is the shape of bug that leaks a column the day someone adds one. So the public endpoint calls
`execute_screen` directly and builds the payload from the allowed keys. The cost is that public
screen reads always hit PostgreSQL; the `Cache-Control: max-age=86400` header is what is expected to
absorb the traffic.

### 20.12 A webhook signing secret is derived, not stored

An HMAC needs the secret in the clear at signing time, so "hash it at rest" is not available. The
alternatives were to encrypt it (a key to manage, a decryption per delivery) or to derive it:

    secret = "whsec_" + HMAC-SHA256(master, f"{endpoint.public_id}:{secret_version}")[:32 hex]

Rotation is `secret_version += 1`; nothing secret is written to a row; a database dump yields no
signing key; and the value can be shown to its owner again without us keeping a decryptable copy.
The master is `DECILE_WEBHOOK_SIGNING_SECRET`, falling back to `DECILE_JWT_SECRET` (which production
already requires). A deployment with neither cannot create an endpoint — creation answers 503 rather
than signing with an empty key.

The same derivation produces the **unsubscribe token** in every alert email. A random token would
exist in plaintext only in the response that created the alert, and every email afterwards would
have nothing to put in the link. Its SHA-256 is still stored (`screen_alert.unsubscribe_token_hash`,
unique) so the endpoint resolves a bare token in one indexed probe.

The signature format is Stripe's — `X-Decile-Signature: t=<unix>,v1=<hex>` over `"<t>.<body>"` —
because it is the one a receiver is most likely to already have code for, and the timestamp inside
the signed payload is what makes a captured delivery un-replayable.

### 20.13 A 4xx is permanent; the backoff lives on the row, not in Celery

`webhook_delivery` carries `attempts` and `next_attempt_at`; `decile.alerts.sweep_webhooks` runs
every two minutes and attempts whatever is due. A retry held inside a Celery task is lost when the
worker dies, and a webhook a receiver never got and nobody remembers to resend is worse than a late
one.

A 2xx is success. A 4xx that is not 408, 425 or 429 is **permanent** — the receiver understood and
refused, and retrying a 401 twenty times is how a misconfigured integration becomes a self-inflicted
denial of service. Everything else retries at 30s, 2m, 8m, 32m, 2h8m. Twenty consecutive failures
disable the endpoint.

### 20.14 "Digest preference" is per alert, not per account

Prompt 20 §3 says "Include an unsubscribe link and a digest preference" and does not say what a
digest is a preference *over*. `screen_alert.digest` means "fold this screen into one combined email
covering every digest alert on this account"; alerts with it off get their own message. Per alert
rather than per account so a user can have one screen shout and five whisper — and because a
per-account preference would need a column on `app_user`, which docs/04 defines and this build may
not edit.

### 20.15 The alert dispatch runs the screen itself when the night has no run

`screen_run` is written when *a user* runs a screen. The nightly publish warms the Redis cache and
records no run. So "daily after publish" would have meant "no run for that date" every night for a
subscriber who did not happen to open the page. `decile_api.alerts.ensure_run` evaluates the screen
for the published date and writes the row through the same upsert the API route uses
(`decile_api.screener.record_run`, extracted from `routers/screens.py` for this reason), so the row
an alert diffs is the row a user's own run would have produced.

Two consequences: a nightly dispatch does real query work per subscribed screen, and an alert diffs
runs of the **same `definition_hash`** only — a user who edited their screen yesterday gets a
`definition_changed` skip rather than an email claiming the whole universe entered and exited.

### 20.16 Six tables that docs/04 does not define

`api_key`, `api_key_usage_daily`, `screen_alert`, `screen_alert_delivery`, `webhook_endpoint`,
`webhook_delivery`. Migration `0010_public_api_alerts_webhooks`. The reasoning per table is on
`decile_core.models.integrations`; the same precedent as docs/04c's auth tables and §13/§14/§17's
additions here. `screen_alert_delivery` is unique on `(alert_id, as_of)` and `webhook_delivery` on
`(endpoint_id, idempotency_key)`, which is what makes a re-run of a night's dispatch send nothing
(CLAUDE.md house rule 7).

### 20.17 Ten keys and ten webhook endpoints per account

Not in the bundle. Enough for one credential per environment per integration, small enough that a
compromised session cannot quietly mint a thousand.

### 20.18 `api_access` stays false for every plan

`decile_core.entitlements.API_ACCESS_AVAILABLE` is still `False`, so no plan grants docs/07's
`api_access` entitlement, and `GET /me` still reports it as false for everyone. That is deliberate
and consistent with §20.1: the public tier does not exist yet, so no plan can include it. **The key
lifecycle is not gated on it** — an account can create keys today, and they authenticate nothing
until the public API is mounted. That is the honest arrangement for a feature whose credentials must
be issuable and testable before the surface they open is legal to serve.

---

## Prompt 21 — The real backfill (2026-08-21)

Taken with a human awake, not overnight. These are decisions about how the first real dataset was
loaded, and every one of them is a departure from something `docs/09` says.

### 21.1 Bars are ingested from the NSE bhavcopy, not from Kite

`docs/09` §"Provider ports" is explicit: `KiteProvider` implements `BarsProvider.daily_bars`, and
`bhavcopy` belongs to `NSEProvider` as a `ReferenceProvider` method — "incl. circuit bands,
series". `docs/09` §Backfill sizes the bar backfill as "~2,300 instruments × 15 years", chunked per
instrument and resumed from `ingest_cursor`. `decile_worker.backfill` implements exactly that and
**is unchanged by this work**.

`decile_worker.bhavcopy_backfill` is a second, additional path that reads the same bars out of the
daily bhavcopy. **This is a deviation from `docs/09` and it is not a small one** — it changes which
provider owns the most important table in the system.

Two reasons, and the second is the one that matters.

**It was necessary.** `DECILE_KITE_API_KEY` and `DECILE_KITE_API_SECRET` are empty and
`DECILE_KITE_TOKEN_ENCRYPTION_KEY` is unset, so `make doctor` reports Kite DOWN and
`decile_worker.backfill` raises "no provider offers daily_bars" before it fetches anything. With no
credentials there is no Kite path to take.

**It is better, for this engine.** The bhavcopy carries `turnover` and both circuit bands; Kite
carries neither.

* `docs/05` §13 says to use exchange turnover for `vol_day_val` and to "only fall back to close ×
  volume when turnover is unavailable, and record which was used". Ingesting via Kite makes that
  fallback unconditional — the *documented* preference is unreachable on the *documented* provider.
  Via the bhavcopy it is the primary path, and `ohlcv_daily.turnover` is populated for every row.
* `docs/05` §12's circuit detection is marked INFERRED precisely because no band is published to
  compare against. The bhavcopy publishes `upper_circuit` and `lower_circuit` per row. That does
  not by itself resolve §12 — `docs/DECISIONS.md` §19.6 shows the export cannot arbitrate the rule
  — but it replaces a guessed input with an observed one.

`ohlcv_daily` needed no migration: `source` already admits `'nse'`, and `turnover`,
`upper_circuit` and `lower_circuit` are already columns. The schema anticipated this; only the
ingest path did not.

### 21.2 The bhavcopy cannot serve `docs/09`'s fifteen years, and the archive says so

The NSE archive serves the UDiFF layout only. Measured: 2011-01-05, 2015-06-10, 2018-03-14 and
2021-09-15 all return **404**; 2023-07-03 returns 404; **2024-01-02 returns 2,658 rows**. So the
earliest bhavcopy is in the first days of 2024 and `docs/09` §Backfill's `--from 2011-01-01` cannot
be honoured from this source at any price.

Consequences, stated rather than discovered later:

* The **fifteen-year backtest remains unrunnable**, and for a new reason. It was previously blocked
  because `ohlcv_daily` was empty; it is now blocked because the history is ~2.6 years deep.
* Anything depending on `high_all_time` is **wrong on this dataset**, not merely absent:
  `decile_core.factors._with_price_levels` computes it as a `cum_max` over the supplied history, so
  on a 2024-start series it is a 2024-onward maximum wearing an all-time label. This is why
  `DECILE_PARITY_BARS_ARE_FULL_HISTORY` exists and why it is **not** set for this dataset.
* `docs/01` §2.13's `DATA_START_DATE` is 2024-11-01, so the *product's* served window is covered
  with ten months to spare. It is deep history, not the product, that this source cannot supply.

When Kite credentials exist the right answer is **both**: `decile_worker.backfill` for depth,
`decile_worker.bhavcopy_backfill` for the recent window's turnover and bands.

### 21.3 Resumability is per-day upsert, not `ingest_cursor`

`ingest_cursor`'s primary key is `(kind, instrument_id, window_start)` — it models per-instrument
chunks, which is the shape Kite's per-instrument API forces. A bhavcopy backfill is a date loop
over one file per day, so it has no instrument dimension to key on, and inventing a sentinel
`instrument_id` would put rows in that table meaning something different from every other row in
it.

Instead this follows the model `decile_worker.reference_backfill` already uses and documents: each
day commits in its own transaction and every write is an upsert, so an interrupted run is resumed
by re-running the same range. That is cheaper here than it looks, because `docs/09` §"NSE
specifics" requires every file to be archived before it is parsed — "Never re-fetch to re-parse:
the archive is the reproducibility record" — so a resumed run reads the archive from disk.

### 21.4 The synthetic fixture bars were deleted before the real load

`ohlcv_daily` held 30,600 rows for 40 instruments across 765 dates — the Prompt 2 fixture's seeded
random walks, which `tests/fixtures/providers/PROVENANCE.md` describes as "real symbols … every bar
before 2026-08-18 is a seeded random walk". They carried `source = 'nse'`, so **nothing in the
schema distinguished them from real bars**, and CUPID in particular had 765 bars of which 758 were
invented.

Leaving them would have mixed invented history into every path-dependent factor and produced a
parity diff that looked like an engine defect. They were deleted before the real ingest rather than
overwritten, because the fixture's 40 instruments are a subset of the real universe and an
overwrite would have left the non-overlapping dates behind. `make seed` regenerates them.

**This is worth a schema change that has not been made:** `source` cannot currently express
"synthetic". A `fixture` value in the `ohlcv_daily_source` CHECK would have made the contamination
visible instead of requiring someone to notice that 765 distinct dates had appeared from a
seven-day ingest.

### 21.5 `corporate_action` cannot represent two announcements sharing a key

`docs/04` keys `corporate_action` on `(instrument_id, action_type, ex_date)`. NSE published two on
the first real backfill: SIYSIL's "Scheme Of Arrangement - Bonus Ncrps 3:1" and "... 4:1", both
`bonus`, both ex 2026-08-21. PostgreSQL refuses one `ON CONFLICT DO UPDATE` whose VALUES carry the
same key twice, so **that single announcement failed the entire fetch** — no action for any
instrument was written, for any date.

`run_fetch_corporate_actions` now collapses to the last occurrence, which is what sequential
upserts would have produced, and names every collision in the step's `duplicate_keys` note. That is
a lossy answer to a schema question and it is deliberately loud rather than silent. The real fix is
a key that admits both — a surrogate id, or `subject` in the key — and that is a `docs/04` change.

### 21.6 `factor_daily`'s upsert chunk was sized in rows, not bind parameters

`UPSERT_CHUNK = 2000` rows against a ~70-column table is 140,000 bound parameters, and PostgreSQL's
wire protocol caps a statement at 32,767. Invisible against the 40-instrument fixture; fatal at the
2,540 instruments a real NSE day carries. The row count is now derived from the payload's width.

### 21.7 THE RETURN WINDOW IS OFF BY ONE BAR, AND THE EXPORT SAYS SO

**This is the substantive finding of the first real parity run, and it is a `docs/05` problem.**

`docs/05` §1 gives `ret_N = (P_t / P_{t-N} - 1) x 100`, and `decile_core.factors._with_window_factors`
implements it literally — `pl.col("close").shift(n)` with `n = window.length`, commented "P_{t-N} is
the bar before the window starts."

Measured against all 271 rows of the reference export, on real NSE bars whose as-of OHLC reproduces
the export exactly, the reference product's base is **one bar later**:

| column | our shift | reference's shift | exact cells at the reference's shift |
|---|---|---|---|
| `absolute_return_one_month`    |  22 |  21 | 265 / 271 |
| `absolute_return_three_months` |  65 |  63 | 260 / 270 |
| `absolute_return_six_months`   | 122 | 120 | 256 / 270 |
| `absolute_return_nine_months`  | 185 | 183 | 250 / 269 |
| `absolute_return_one_year`     | 247 | 245 | 240 / 268 |

At our shift the exact-match count is **0, 1, 0, 0, 0**. At the reference's it is 90-98%. CUPID is
the worked example: published `ret_1m` 37.03 is `284.03 / 207.27 - 1` where 207.27 is the close 21
bars back; 22 bars back is 214.78 and gives 32.24, which is what we currently store.

So the reference counts a window of N bars **inclusive of both endpoints** — base `P_{t-(N-1)}` —
while `docs/05` §1 as written counts N bars *before* t. Everything reading those windows inherits
it: `sharpe_N` (a ratio of two of them), `vol_N`, `rsi_N`, `high_1y` and `away_from_high_1y` all
fail at 271/271 today for this one reason.

**The engine has not been changed.** `docs/05` is the source of truth and CLAUDE.md's house rule is
to say so out loud rather than deviate quietly. This needs a hand-edit to `docs/05` §1 fixing the
definition, and then a one-line change in `factors.py` — in that order.

Two caveats on the residual, so nobody reads the 90-98% as "the rest is noise":

* The exact-match rate **falls with the horizon** (98% at 1m to 90% at 12m), which is the shape
  missing corporate actions produce: a longer window spans more unadjusted events. See §21.8.
* 9m and 12m sit at shift `length - 2` where 1m/3m/6m sit at `length - 1`, so there is a further
  one-day calendar discrepancy at the long end that this table does not explain and §21.9 records.

### 21.8 NSE serves only a recent corporate-actions window, so the deep series are unadjusted

`NSEProvider.corporate_actions(since)` returned **19 records for `since = 2024-01-01`** — the same
19 it returns for `since = 2026-07-01`. The endpoint serves a current window and ignores how far
back the caller asks. Only **4** of those have an ex-date on or before the 2026-08-18 as-of date.

So `ohlcv_daily.close` is adjusted for four actions across two and a half years. Any split or bonus
in between is **not** applied, and every factor whose window spans one is wrong by the ratio. This
is the most likely explanation for the residual in §21.7 and it bounds what this dataset can prove.
A historical corporate-actions source is now the single highest-value missing input.

### 21.9 `apply_adjustments` applies actions with a *future* ex-date — a look-ahead

Found by the parity harness's pre-flight, not by review. On the first run, 8 of the 271 as-of bars
disagreed with the export by exactly a dividend amount:

```
ZENTEC.close     1981.4 vs 1982.4   delta 1.00   ZENTEC dividend Re 1,  ex 2026-08-21
JINDALSTEL.close 1099.5 vs 1101.5   delta 2.00   JINDALSTEL dividend,   ex 2026-08-21
```

The as-of date is **2026-08-18**. `reprocess_instrument` adjusts an instrument's whole series for
every action in `corporate_action`, with no upper bound on `ex_date`, so an action announced for
three days *after* the as-of date rewrote that day's adjusted close. CLAUDE.md house rule 5 is "No
look-ahead, ever", and this is one.

It is not merely a parity nuisance. A backtest as of any past date reads a series adjusted for every
action that has happened *since*, which is the same class of defect as `docs/DECISIONS.md` §15's
`liquidation_day`. The dataset here was made point-in-time by deleting the future-dated actions
before adjusting; **the code was not changed**, because the right fix is a decision about whether
`ohlcv_daily.close` is a single "as of today" series or must be resolved per as-of date.

### 21.10 Adjusting `open`/`high`/`low` is destructive: there is no raw counterpart

`docs/04` gives `ohlcv_daily` a `close_raw` beside `close`, and **no `open_raw`, `high_raw` or
`low_raw`** — but `reprocess_instrument` rewrites all four. So `close` can always be recovered and
the other three cannot: resetting `close = close_raw, adj_factor = 1` (the obvious way to undo a bad
adjustment) leaves `open`, `high` and `low` adjusted, with the factor that would have un-adjusted
them already gone.

Recovery here was only possible because `docs/09` §"NSE specifics" requires the raw file to be
archived before it is parsed — re-running the ingest from `.archive` restored them with no network
call. That is the archive earning its keep exactly as documented. It is still a schema gap: either
store the three raw columns, or never rewrite the three adjusted ones.
