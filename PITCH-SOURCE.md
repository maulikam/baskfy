# Baskfy — complete product source document

**Purpose:** the single reference to build a pitch deck from. Everything the product is, does,
and does not do yet. Written 26 Aug 2026 from the codebase and the live database, not from
memory — every number in §13 was measured, and §12 is the honest list of what is not finished.

**Use it like this:** §1–§3 are the story slides. §4–§9 are the product slides (§8 is the one
layer still in design — pitch it as roadmap, never as shipped). §10–§11 are the "why this is
hard to copy" slides. §12 is what you must not overclaim. §13 is the numbers page.

---

## 1. The one-liner

**Baskfy turns a stock screen into a portfolio you can actually hold — and then keeps it
honest.** It ranks the whole Indian market on momentum, builds the top slice into a basket, and
reconciles that basket against the shares really sitting in your demat account.

Three sentences, if you have room for three:

> Indian retail investors can buy a model portfolio, or they can screen stocks, or they can track
> what they own. Nobody joins those three together, so the model's returns and your returns are
> different numbers and nobody tells you why.
> Baskfy is one ledger underneath all three.

## 2. The problem

**The gap is between the model and the money.**

- A model portfolio publishes *its* return. What you earned is different — you bought on a
  different day, at a different price, and you did not apply every rebalance. Existing products
  show you the model's number and let you assume it is yours.
- A screener gives you a ranked list. Turning that list into a position is manual, and nothing
  connects the list you screened to the shares you now hold.
- A portfolio tracker knows what you hold but not *why* — no thesis, no rule, no rebalance.
- Holdings are scattered across brokers, and nothing groups them by intent. "Which of these is my
  long-term money and which is the momentum experiment?" has no answer in any tool.

**The consequence:** every performance number an Indian retail investor sees is either the
model's (not theirs), or unattributed (theirs, but meaningless). Baskfy's whole design is a
refusal to print either one.

## 3. What Baskfy is — three products on one ledger

| | What it does | Who it serves |
|---|---|---|
| **The screener** | Ranks the whole NSE universe on 64 momentum factors, point-in-time, with backtests | The self-directed investor who wants the rule, not the recommendation |
| **The basket layer** | Curated model portfolios with versions, rebalances, drift, fees and subscriptions | The investor who wants a thesis run for them |
| **The portfolio ledger** | Reconciles real broker holdings into named portfolios that are independently measurable | Everyone — it is the layer the other two report against |
| **The fundamentals timeline** *(in design — §8)* | One AI-read timeline per company of every filing, result and transcript, with citations back to source | The investor who wants to know what *changed*, not just what moved |

The third is the differentiator and it is the newest shipped layer. §7 covers it in full. The
fourth is designed but not built — §8 covers it, and §12 says exactly how to talk about it.

**What Baskfy never does:** hold your money, hold your securities, or place an order from the web
app. Shares stay in your demat. Every order is one you place at your own broker. This is a
product constraint, not a limitation — see §10.

## 4. Feature inventory — the screener and data plant

### 4.1 The factor engine

- **64 factors** across five families, all computed nightly, all point-in-time:
  - **Absolute returns** — 1M / 3M / 6M / 9M / 12M, plus 11 blended averages
    (`avg_return_12_9_6_3_1`, `avg_return_12_6_3`, …)
  - **Sharpe returns** — the same five windows plus 12 blends, risk-adjusted
  - **RSI** — five windows plus 11 blends
  - **Beta-adjusted** — `sharpe_div_beta_12m`, `abs_div_beta_12m` and blends
  - **Structure** — `vol_12m`, `beta_12m`, `ret_12m_minus_1m` (skip-a-month momentum),
    `away_high_ath`, `away_high_1y`, `pe`, `marketcap_cr`
- **14 selectable universes** — NIFTY 50, Next 50, 100, 200, 500, Total Market, LargeMid 250,
  Midcap 150, Smallcap 250, Microcap 250, MidSmall 400, All NSE Listed, F&O, All ETFs
- **Decile bucketing** — rank the universe by market cap, apply filters only within D1–D6
- **Filters** — return floors, median traded value, series (EQ/BE/BZ), P/E ranges, circuit
  history, top-beta and top-volatility exclusion flags computed per universe nightly

### 4.2 Screens

- Save, name and re-run screens; 5 on the free tier, 50 on paid
- **Historical ranks** — run any screen as of any past date, with point-in-time index membership
  and point-in-time factor rows (no look-ahead)
- **Custom result columns** from a 93-column picker
- **CSV export** of any screen
- **Screen alerts** — nightly diff of a saved screen, emailed; entries, exits, rank moves
- **Screen runs** are audited: every run writes a row with its data version

### 4.3 Backtests

- Point-in-time backtest engine over any saved screen
- Rank-buffer rebalance rule (a stock leaves only when it falls past a buffer, not on the first
  tick below the line — reduces churn)
- Sharpe computed against **OECD IR3TIB** risk-free curve, not a hard-coded rate
- Cost model, turnover, drawdown, rolling returns
- Every backtest states its assumptions on the page, including which are survivorship-biased

### 4.4 Market surfaces

- **Market today** — index dashboard, 176 indices with PE/PB/dividend yield
- **Market mood** — breadth (% above 20DMA) across 12 universes, computed nightly
- **Regime** — Wasserstein-distance regime classification per instrument
- **Listings** — the full NSE register: 3,524 rows, series, ISIN, listing date, face value
- **Instrument factsheets** — per-stock: price, MAs, returns, Sharpe, RSI, beta, market cap, P/E,
  corporate actions, rank history, universe memberships, pros/cons

### 4.5 The nightly pipeline

Ten steps, idempotent, with a hard quality gate that refuses to publish a bad day:

1. `refresh_instruments` — Kite dump + NSE listings register
2. `fetch_daily_bars` — Kite historical, rate-limited to 3 req/s
3. `fetch_corporate_actions` — NSE announcements, parsed from free text
4. `apply_adjustments` — splits and bonuses applied to the adjusted price series
5. `refresh_index_membership` — point-in-time constituent files
6. `refresh_index_snapshots` — index PE/PB/DivYield, **plus equity fundamentals** (market cap,
   P/E) from NSE's quote API
7. `compute_factors` — all 64, per instrument, per day
8. `compute_market_health` — breadth per universe
9. `data_quality_gate` — a **hard blocker**: row-count assertions, staleness, coverage
10. `publish` — assigns a data version; nothing is servable until this runs

Plus: EOD basket metrics, curated batch sync, SIP reminders, rebalance notifications, dividend
accrual, drift repair, account purge, and the new **nightly EOD NAV job** (§7).

## 5. Feature inventory — the basket layer

### 5.1 Baskets

- **Curated baskets** — named model portfolios: constituents, target weights, thesis, rationale,
  rebalance frequency, benchmark
- **Versions** — every rebalance is an immutable version with a diff; history is never rewritten
- **Sources** — a basket can be run by the engine (from a screen/strategy) or published by a
  third-party manager
- **Computed metrics** — minimum investable amount (smallest whole-share combination
  approximating the weights), volatility band, returns since inception, CAGR, drawdown
- **Collections** — editorial shelves grouping baskets by theme
- **Compare** — side-by-side basket comparison
- **Watchlist** — bookmark a basket; tracks "moved X% since you watchlisted it"
- **Create-a-basket workspace** — turn any saved screen into a basket: pick a screen (starter
  templates on first login, or one you built), size it, save. A manual "pick stocks yourself"
  path exists alongside. Nothing in the flow can execute.

### 5.2 Managers

- Manager profiles with SEBI registration type and number, methodology, disclosures
- Onboarding lifecycle: DRAFT → SUBMITTED → APPROVED → REJECTED / SUSPENDED
- Revenue-share table (dark — see §12)
- Manager console for publishing versions

### 5.3 Investing (no execution)

- **Mark-as-invested** — the user confirms they invested at their broker; Baskfy creates the
  investment record. There is no web execute route, by design.
- **Order plans** — invest / apply-rebalance / partial-exit / exit each produce an *order plan*
  the user takes to their broker. A plan is not an order.
- **SIP plans and reminders** — scheduled reminders, never auto-orders
- **Drift detection and repair** — how far the real holding has drifted from target weights
- **Dividends** — accrued per holding
- **Fee ledger** — 1.5% platform fee capped at ₹100 per buy, ₹10 per SIP instalment, plus 18%
  GST. Rebalance, customize, partial exit and full exit are **zero-fee**. Fees are *computed*
  today; collection is dark.
- **Pending actions** — a carousel of what needs a decision, each deep-linking to its resolution
- **Costs page** — every fee, every charge, per investment

## 6. Feature inventory — accounts, billing, platform

- **Home** — the signed-in landing surface: net-worth header, pending-actions carousel, an
  updates strip, live-computed trending baskets and collection shelves. Every module degrades
  to an honest empty state on its own, so an unreachable API costs one module, not the page.
- **Auth** — email + OTP and password, Argon2id, opaque tokens, refresh rotation, lockout
- **Entitlements** — one server-side service; four tiers (anonymous / free / registered / paid)
- **Plans** — Monthly ₹500, Yearly ₹3,999, Forever ₹14,999 (rising to ₹899 / ₹5,999 / ₹19,999
  from Dec 2026). "Forever" carries a required disclosure that it means the lifetime of the
  service.
- **Billing** — Razorpay checkout, webhook handling, GST-compliant invoices with a sequential
  invoice counter, SAC code, and the rate each invoice was raised at recorded on the row
- **API keys** — issuance, scoping, per-day usage metering (the public API itself is **off** —
  see §12)
- **Webhooks** — endpoints, event delivery, retries, signature
- **Admin** — user admin, pipeline console, public-API console, admin action audit
- **Account deletion** — a real purge that keeps the invoice trail and unlinks it
- **Consent records**, **support**, **blog**, **FAQ**, four legal pages

## 7. The portfolio ledger — the newest and most differentiated layer

This is the part no competitor has, and it is worth its own slide.

### 7.1 The idea

> Baskfy's core is an **allocation ledger** that reconciles real broker holdings into
> user-defined, independently measurable portfolio groups. The page is thin once the ledger
> exists.

### 7.2 The rules that make it trustworthy

- **A holding belongs to exactly one capital portfolio.** Enforced by the database, not by
  application code — a partial unique index that bulk loads and even
  `SET session_replication_role = replica` cannot bypass.
- **Capital portfolios + Unallocated = net worth, to the paisa.** Not asserted after the fact:
  the parts and the whole are the same walk over the same list, so they cannot drift.
- **Monitoring views** — overlapping lenses ("all defence stocks", "bought in 2026") that never
  enter a total and are labelled everywhere they appear.
- **Every return number states what it is** — TWR since subscribed, TWR since go-live, TWR since
  created, "since grouped", XIRR since purchase — with its start date. There is no unlabelled
  percentage anywhere in the product.
- **Model and actual are never one figure.** Enforced by the type system: the return type defines
  no arithmetic, so they cannot be added.
- **A detected sell either auto-attributes or asks.** It never silently alters a return series.
  Unanswered questions *freeze* the affected holding's contribution rather than guessing.
- **A split or bonus produces exactly zero P&L**, and updates the holding everywhere it appears
  atomically.

### 7.3 The surfaces

- **Overview** — one screen: hero metrics (value, today's P&L, total P&L, XIRR, invested),
  combined EOD NAV chart with benchmark overlay and drawdown, needs-attention ribbon, portfolio
  table with a right-side inspector drawer, and the Unallocated section as the centrepiece
- **Holdings** — the flat broker-level truth: every share, where it sits, what it is allocated to
- **Activity** — buys, sells, cash assignments, dividends, corporate actions, rebalances,
  reconciliation history
- **Detail page** — summary, NAV performance, holdings with weight and contribution, activity,
  and a source panel that differs by where the portfolio came from
- **Sleeves** — split a portfolio's capital across sleeves — a manager's basket, a rule you
  wrote, holdings you run by hand — as amounts and weights only; the planner answers "how much
  goes where", and no control on it can place anything
- **Rebalance tracker** — which symbols changed between basket versions, and what you have and
  have not yet applied
- **Onboarding** — connect broker → everything lands in Unallocated → the product actively
  suggests groupings by sector, by purchase era, and by overlap with a subscribed basket.
  **Getting from 40 unallocated holdings to 4 named portfolios is the activation moment.**

### 7.4 The engine underneath

- **Cash ledger** — external deposits land in Unallocated; assigning cash to a portfolio is the
  XIRR event; buying a stock inside a portfolio is not. This is what keeps per-portfolio XIRR
  mathematically pure.
- **EOD NAV series** — a nightly job writes one official value per portfolio per day, like a fund
  NAV. It powers the chart, per-day P&L, contribution and drawdown.
- **Reconciliation inbox** — "we detected a sell of 100 HDFC Bank — which portfolio?"
- **CAS import** — parses CDSL and NSDL consolidated account statements to backfill purchase
  prices and dates, which unlocks true XIRR for holdings that predate the connection
- **Broker sync** — diffs broker holdings against the ledger and raises reconciliation items
- **Broker connect catalog** — a connect page listing ten Indian brokers; live OAuth connect is
  wired for Zerodha first (connect + holdings sync only — the web app still never places an
  order), with CSV import as the parallel path for every broker

## 8. The fundamentals timeline — the layer in design (do not pitch as shipped)

Momentum answers "what is moving". The fundamentals timeline answers "what changed inside the
company" — the same product philosophy, applied to disclosures instead of prices.

### 8.1 The idea

> Baskfy brings every annual report, quarterly result and exchange filing into one intelligent
> company timeline. Its AI reads documents, presentations, transcripts and earnings-call media
> to identify key financial changes, risks and management guidance — with citations back to the
> original source. Users understand what changed in every NSE/BSE-listed company without
> manually searching through hundreds of disclosures.

### 8.2 What it will do

- **One timeline per company** — annual reports, quarterly results, investor presentations,
  earnings-call transcripts and audio, and exchange filings (announcements, ratings, pledges,
  insider trades), in the order they happened
- **An AI reading layer** — extracts the key financial changes, new risks and management
  guidance from each document, and every extracted claim carries a citation back to the exact
  source passage. Same house rule as the rest of the product: no unlabelled numbers, no
  uncited claims.
- **Attached where it is useful** — the instrument factsheet (§4.4) gains a "what changed"
  panel; a basket page can surface filing events for its constituents; screen alerts can carry
  disclosure events alongside rank moves
- **Archive-then-parse applies here too** (§11) — every filing is archived raw before it is
  parsed, so every AI-extracted claim is reproducible against the original document
- **NSE and BSE** — this layer is scoped to both exchanges, Baskfy's first step beyond NSE

### 8.3 Why it belongs in Baskfy

- The ledger says what you hold and what it returned; the timeline says what happened to it.
  Together they close the last gap in §2: not just "your return, labelled" but "and here is
  what changed while you held it".
- The citation rule is the moat here as well. Summarisation products exist; a reader whose
  every sentence is auditable back to the filing is the same "accounting that refuses to lie"
  position (§10), applied to text.

### 8.4 Status — be precise in the deck

**Designed, not built.** As of 26 Aug 2026 nothing of this layer exists in the codebase: no
filings ingestion, no document store, no AI pipeline, no timeline surface. It is a roadmap
slide, not a product slide — put it in the "next" column, and attach no number to it.

## 9. The execution desk (operator-only, real money)

A separate, already-deployed product: a personal momentum desk that places **real orders in a
real Zerodha account** on a real hostname. It is the proof the strategy works and the source of
the track record. It is not multi-tenant and is not part of the consumer product.

Seven non-negotiables, each of which is a scar from a real incident:

1. **Never auto-execute.** Orders fire only from an explicit confirm with a plan ID that expires
   in 30 minutes.
2. Holdings quantity must include T1 and collateral quantity (missing this once caused a
   103-share position error).
3. Pledged shares sell directly on Zerodha; the plan flags them as information only.
4. **Every buy gets a GTT stop the same session**, vol-scaled 8–12%.
5. CNC-only. Intraday and options are gated off inside the gateway, not at the call site.
6. All order flow passes one gateway: guards → risk → rate limits → journal → broker. Sovereign
   gold bonds and G-secs are blocked at the lowest layer, before any network call.
7. Filter-rejected stocks are never bought.

Plus a **regime overlay** (R1–R4 exposure tiers with hysteresis and staleness refusals, currently
in observe mode) and 26 analytics modules: EOD snapshots, NAV/index, PRI/TRI benchmarks, breadth,
tax lots, corporate actions, tradebook import, reconciliation.

The web app carries **read-only mirrors** of the desk — performance, tradebook, reconciliation
and the most recent rebalance plan in full, down to what filled and at what price — with no
execute button and no route behind one. The desk console remains the only place an order can be
placed.

## 10. Why this is hard to copy

1. **The accounting is the moat, not the UI.** Anyone can render a portfolio card. Very few will
   build a ledger where the parts provably sum to the whole, where a sell that cannot be
   attributed freezes a number instead of guessing it, and where the database physically refuses
   a double allocation.
2. **Point-in-time correctness.** No look-ahead is a house rule enforced by tests, not by
   discipline. Historical ranks and backtests use point-in-time index membership *and*
   point-in-time factor rows.
3. **Honesty as a feature.** Every return states what it is and since when. Model and actual
   cannot be blended. A missing number shows an em dash and a reason, never a zero. This is
   unusual enough in Indian fintech to be a positioning statement.
4. **A working execution desk behind it.** The strategy is not a backtest — it has been running
   real money through a hardened order gateway with GTT stops and instrument guards.
5. **Depth of data.** 3.5 million bars, 685k factor rows, 148k index snapshots, 10,481
   instruments, all rebuildable from a raw-file archive that is fetched once and parsed from
   record.

## 11. Technology

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic (24 migrations), Celery + Beat
- **Data:** PostgreSQL + TimescaleDB, Polars for the factor engine, Redis
- **Frontend:** Next.js 15 App Router, React, Tailwind v4, visx charts, generated TypeScript
  client from the OpenAPI document
- **Providers:** Kite Connect (bars, instruments, holdings), NSE public files (listings, indices,
  corporate actions, fundamentals) with archive-then-parse discipline — every file is archived
  before it is parsed, so any number is reproducible without refetching
- **Deployment target:** AWS Mumbai
- **Architecture laws:**
  1. The pure domain package touches nothing — no database, no network, no clock. It is all
     value-in, value-out, which is why it can be tested exhaustively.
  2. One execution package is the only path to an order.

## 12. What is NOT done — do not overclaim these

**Be candid about this list in a pitch. It is short, and every item is either external or named.**

| | Status |
|---|---|
| **Paid multi-tenant launch** | Blocked on counsel item C3 (whether posture B needs RA registration / broker empanelment). Engineering for it is built; the flags are off. |
| **Public sign-up** | Dark behind a flag |
| **Fee collection** | Fees are *computed* and journalled; no money is collected. Dark behind a flag. |
| **Public market-data API** | Deliberately off — a source constant plus a flag — pending a data-redistribution opinion |
| **Web execute** | Never. Not a gap; a product law. |
| **Fundamentals timeline (§8)** | Designed, not built. No filings ingestion, no AI pipeline, no timeline surface exists yet. Roadmap slide only — attach no number to it. |
| **Broker coverage** | Only Zerodha/Kite credentials exist. Eight other brokers need credentials; Groww has no public API. Consolidated holdings across brokers is built and proven against fixtures but has never run against a live non-Zerodha account. |
| **Live broker fetch** | The holdings sync has never made a live call. The whole chain is proven against a fixture provider. |
| **Intraday** | Deliberately deferred. v1 is end-of-day only, presented like a fund NAV. |
| **Catalogue content** | **Six published baskets** (one engine scan + five cut from screens), across four collections. Enough to demo; not yet enough to browse. This is the biggest gap between "built" and "compelling", and it is a content problem rather than a code one. |
| **Data depth** | Bars start 2017-01-02, not 2011. The deep backfill is written and resumable but has not been run. |
| **Legal pages** | Four pages are live and render placeholders like `[SUPPLIER LEGAL NAME]`. No lawyer has read them. `docs/COUNSEL-BRIEF.md` is ready to send. |
| **Historical breadth** | Survivorship-biased, and the page says so. The fix needs NSE index-change announcements. |
| **Browser coverage** | 21 end-to-end specs exist; only a handful have ever run in a browser. |

## 13. Numbers for the slides — all measured 26 Aug 2026

**Data**
- 10,481 instruments · 3,534,860 daily bars · 685,636 factor rows
- 148,212 index snapshots · 3,696 breadth rows · 289 corporate actions
- Coverage 2017-01-02 → 2026-08-21 · ~2,540 stocks trading on a given day

**Product surface**
- 142 API endpoints · 74 web pages · 69 database tables · 24 migrations
- 64 factors · 14 universes · 93 exportable columns · 12 market-health universes
- 6 published baskets · 103 constituents · 4 collections · 176 indices tracked

**Engineering**
- ~85,000 lines of Python (excluding tests) · ~46,000 lines of TypeScript/TSX
- 177 Python test files · 102 web test files · 21 end-to-end specs · 50 desk test files
- **2,415 Python tests passing** (core + worker) · **1,869 web tests passing**
- 63 pure domain modules with zero I/O

**Commercial**
- Plans: ₹500/mo · ₹3,999/yr · ₹14,999 forever (₹899 / ₹5,999 / ₹19,999 from Dec 2026)
- Basket fee model: 1.5% capped at ₹100/buy, ₹10/SIP instalment, +18% GST; rebalance, customize
  and exit are free

## 14. Regulatory posture (India / SEBI)

- Baskfy is **not SEBI-registered**. The product never uses "managed", "managed portfolio",
  "advisory" or "PMS" for third-party content. The sanctioned phrasing is **"Subscribed model by
  {publisher}"** / "Published by" / "Curated by".
- Posture **B** is written and recorded: publish baskets, users execute at their own broker,
  no discretionary management.
- **Three counsel questions remain open** and are documented: algo-ID / exchange registration
  when supplying order plans to a third-party broker app; research vs advice for ranked baskets;
  whether posture B requires RA registration or broker empanelment.
- Disclaimers are components rendered on every page, not a footer afterthought.
- Compliance copy is enforced in code: a copy-lint test with a banned-phrase list fails the
  build if any page claims or implies investment advice or promises an outcome.
- A grievance officer must be appointed for DPDP Act compliance before public launch.

## 15. The story arc for a deck

1. **Hook** — "Your model portfolio returned 22%. What did *you* return? Nobody can tell you."
2. **Problem** — the gap between the model and the money (§2)
3. **Insight** — the missing layer is an allocation ledger, not another dashboard
4. **Product** — three products on one ledger (§3), with the Overview screen as the hero image
5. **Demo moment** — 40 unallocated holdings → 4 named portfolios, and every number labelled
6. **Why us** — a working execution desk, point-in-time data, and accounting that refuses to lie
   (§10)
7. **Business model** — subscriptions plus basket fees (§13)
8. **Traction/status** — what is built, honestly (§13), and what is next (§12) — the
   fundamentals timeline (§8) is the roadmap headline
9. **Ask** — the three things that unblock launch: catalogue content, counsel sign-off, broker
   credentials

## 16. The three things that actually unblock launch

Everything else is engineering that is done or scheduled.

1. **Catalogue content.** Six baskets is a demo, not a marketplace. This is a content and
   manager-acquisition problem, not a code problem.
2. **Counsel sign-off on C3** and the seven business facts the legal pages need (legal name,
   address, GSTIN, grievance officer, venue, hosting/email providers, GST-inclusive pricing).
3. **Broker credentials** beyond Zerodha, so consolidated holdings works for real users.
