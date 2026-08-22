# smallcase — Complete Product Understanding & Replication Spec

> Compiled 22 Aug 2026 from a live, logged-in walkthrough of https://www.smallcase.com (web app) plus public documentation. Written as a build-ready reference: hand this document to a coding agent/CLI to replicate the product inside an existing project.

---

## 1. What the product is (one paragraph)

smallcase is an Indian investment platform that sells **model portfolios ("smallcases")** — curated, weighted baskets of exchange-listed stocks/ETFs (and now mutual funds) built by **SEBI-registered managers**. The platform itself never holds money or securities: users connect their **existing broker account** (Zerodha, Groww, Upstox, HDFC Securities, ICICI Securities, etc.), and every buy/sell is executed as real orders in the user's own demat account via a broker OAuth session. smallcase monetizes through **flat transaction fees**, **manager subscription fees** (revenue-shared), and adjacent products (loans against securities, FDs, MFs, US equities via Tickertape). The parent company is **CASE Platforms** (formerly smallcase Technologies).

## 2. Core domain concepts (glossary)

| Concept | Definition / behavior observed |
|---|---|
| **smallcase** | A named basket: list of constituents + target weights + metadata (theme, description, rationale) + rebalance schedule, managed by a manager. Types: `IN Stock smallcase` (stocks/ETFs), `Mutual Fund smallcase`, US Stock portfolios ("coming soon"). |
| **Constituent** | One stock/ETF/MF in a basket, with target weight (%). Grouped by segment (e.g., Equity/Gold; Largecap/Midcap/Smallcap/Debt). |
| **Manager** | SEBI-registered Research Analyst (RA) or Investment Advisor firm (e.g., Windmill Capital — in-house, manages 76 smallcases; Niveshaay; Weekend Investing). Has profile page, SEBI reg no., strategies, FAQs, address/CIN, disclosures. |
| **Min. Amount** | Minimum lump sum needed to buy the basket at current prices in prescribed weights (derived from constituent prices — smallest purchasable whole-share combination approximating weights). Displayed on every card, e.g. ₹402 (ETF basket) to ₹1,14,608 (30-stock midcap basket). |
| **Free Access vs Fee Based** | Free (`Free Access` badge) — anyone can see constituents and invest. Fee-based (`Subscribers Only` badge) — constituents, portfolio report and rebalance rationale are locked until the user buys the manager's subscription plan. |
| **Subscription** | Per-smallcase (or per-manager) recurring plan set by the manager, e.g. ₹8,300/6 months, ₹12,600/year, ₹9,999/year. Auto-renews; user can still hold investments if lapsed but loses rebalance updates. |
| **Rebalance** | Manager publishes a new constituents/weights version on a schedule (Weekly / Monthly / Quarterly / Annual / On need basis). Users are notified and must **apply** the update themselves (one-click diff order via broker). Applying is optional. |
| **Volatility label** | Low / Medium / High, computed from std. deviation of constituents. Shown as colored gauge chip. |
| **CAGR / Returns** | Cards show contextual metric (5Y CAGR, or "2M returns" for young baskets). Detail page shows live performance chart with 1M/1Y/3Y/5Y/MAX ranges, an absolute % since inception, SIP-mode toggle, and index comparison ("Compare with"). |
| **Watchlist** | Bookmarked smallcases; tracks "Moved by X% since watchlisted on {date}". |
| **Investment score** | App-only gamified portfolio health score. |
| **Exit** | Sell partially or whole; no lock-in, no exit load on platform side. |

## 3. Actors & ecosystem

- **Investor** — owns broker + demat account; all holdings sit in their demat, not with smallcase.
- **Manager (creator)** — publishes/rebalances baskets, sets subscription pricing, does investor communication (updates feed, YouTube Q&A, portfolio reports, investor letters).
- **Broker** — executes orders, is the OAuth identity provider for order placement, source of holdings/funds data. smallcase distributes through **broker-branded versions** too (e.g., "buying smallcases with Zerodha Kite").
- **Platform (smallcase/CASE Platforms)** — discovery, portfolio accounting/tracking layer, rebalance distribution, billing, notifications.
- **Lending partners** — RBI-regulated lenders behind Loan-against-MF/Stocks (via group co. Essential Investment Managers Pvt Ltd).
- **Gateway partners/developers** — third-party apps embedding smallcase transactions via SDK/APIs (developers.gateway.smallcase.com).

## 4. Architecture model (the single most important design fact)

1. **Custody-less design.** smallcase stores only *models* and *user's investment ledger*. Actual positions live in the user's demat via the broker. This removes licensing burden for custody and makes the broker the source of truth for execution.
2. **Two-tier auth.**
   - Tier 1: smallcase account login (mobile OTP / email; long-lived session) → browse, watchlist, subscriptions, view investments.
   - Tier 2: **broker session** (e.g., "Login to Continue — place your orders on smallcase by logging in with your Zerodha account → Continue with Zerodha", external OAuth popup). Required for *anything order-shaped*: invest, invest more, SIP order, apply rebalance, view/manage orders, exit, manage constituents, and even viewing the rebalance order screen. Broker tokens expire (typically daily), so the app is built to re-prompt for broker login lazily.
3. **Order model.** An "order" on smallcase is a *batch* of per-security market orders sized to match target weights for the invested amount. States tracked per batch (placed/filled/partial). Fills land in demat; smallcase reconciles.
4. **Drift/repair.** If the user sells constituent stocks directly on the broker, smallcase detects mismatch and raises a "Incorrect smallcase holdings — stocks were sold directly on broker, rebalances may be affected → Fix now" pending action (repair flow re-syncs the internal ledger with actual demat holdings).
5. **Market-hours gating.** Rebalance/orders only placeable during market hours; otherwise modal: "Market is Closed — you can place a Rebalance order when the market reopens on {date} between 9:15 AM and 3:30 PM", with **Notify me when market opens** CTA.
6. **Multi-asset shells.** Same account aggregates: smallcases, Mutual Funds (separate MF account setup flow), Fixed Deposits, IN Stocks, US equities (via Tickertape, IFSC/GIFT City), credit products.

## 5. Information architecture (observed URLs)

```
/home                     — logged-in dashboard
/search                   — product switcher + search + trending (entry to explore)
/explore/smallcases?...   — filterable catalog (query-param-driven)
/smallcase/{slug}-{SCID}  — detail (Overview tab)
/smallcase/{slug}-{SCID}/constituents — constituents & rebalance timeline tab
/collection/{id}          — curated collection pages
/manager/{slug}           — manager profile
/investments              — portfolio dashboard (tabs: smallcases | Mutual Funds | FDs | IN Stocks)
/details/{investmentId}   — one invested smallcase (Mongo-style ObjectId)
/orders/{investmentId}    — order history for an investment (broker-gated)
/watchlist                — watchlist
/subscriptions            — active subscriptions management
/fees                     — platform fee ledger
/account                  — profile, linked accounts, activity, support
/credit                   — loans against MF/stocks
/more                     — misc hub (Create, loans, watchlist, subscriptions, US equities)
/create                   — custom smallcase builder
```
Bottom-tab navigation (5 tabs): **Home, Search, Credit, Investments, More**; top bar has back button, page title, profile/account icon, notification bell (badge count).

## 6. Screen-by-screen detail

### 6.1 Home (`/home`)
- Ticker strip: NIFTY & SENSEX values + day %.
- **Current Value** headline (whole portfolio, e.g. ₹2.53Cr, +1.04%) with privacy eye-toggle ("Hide values") and chevron → /investments.
- **Pending actions** carousel: e.g. "Incorrect smallcase holdings … Fix now", "Pending rebalances — {names}, +N more → See rebalances", ending card "That's all — no other actions need your attention". Dismissible (X).
- Promo banners carousel (investors' radar, thematic collections).
- **Based on your interests** — watchlist-driven recommendations: card shows name, manager, "Moved by %", CAGR, Subscribe/Invest CTA, "Since watchlisted on {date}".
- **Trending** — ranked lists with audience tabs (All | IN Stock smallcases | Mutual Fund smallcases | IN Stocks | Mutual Funds) and ranked sub-lists: Most Invested Fee-based, Most Invested (MF), Most Inflows, Budget-friendly Plans, New & Popular, Popular Mutual Funds, Low-cost Index Funds, Popular Stocks, Most Watched. Each row: rank, name, one contextual metric (1M/5M returns, 5Y CAGR…).
- Product cross-sell tiles: US Stock Portfolios (Coming soon), MF Portfolios (NEW), IN Stock Portfolios; Other products: Fixed Deposits, Mutual Funds, IN Stocks.
- **Take your pick** — collections grid (Most Subscribed, Park Your Savings, Popular ETFs, Fixed Income Mutual Funds, India's Growth Engines, High Dividend Blue Chips, Oil & Energy, Goal-based MF smallcases…).
- **Making smalltalk** — editorial/blog cards.
- App-download banner (persistent bottom sheet, dismissible) + app-only features upsell modals ("Forecast your net worth, invest in FDs, get loan against stocks…").

### 6.2 Search hub (`/search`)
- Search input ("Search for smallcases").
- Product cards: US Stock Portfolios (Coming soon) / MF Portfolios (NEW) / IN Stock Portfolios; Other products chips: Fixed Deposits, Mutual Funds, IN Stocks.
- **Popular categories & filters** deep-link chips: Highest 1Y return portfolios, Low cost funds, High dividend stocks, Recently rebalanced portfolios, Top debt funds, Largecap stocks, "Let AI find your next investment", Trending themes, Park short-term cash, Deep value, Target-date, ESG. ("Explore more ideas")
- Reuses Trending + collections + editorial sections from Home.

### 6.3 Explore/catalog (`/explore/smallcases`)
- URL is fully query-param-driven, e.g.:
  `?filters.CONSTITUENTS_FILTER=ETI&filters.LOWEST_SUBSCRIPTION_PRICE_FILTER=ALL&filters.OTHER_FILTER[]=INCLUDE_NEW_SMALLCASES&filters.SC_SUBSCRIPTION_TYPE_FILTER=ALL&sortBy=POPULARITY_SORT`
- Quick filter chips: Filters, Sort by, Under 25k, Under 5k, Free Access, Fee Based.
- **Filters panel** (full-screen dialog, "N filters selected" + Apply + Clear all):
  - Volatility: Low / Medium / High ("based on std. deviation of constituents").
  - Category (tree with checkboxes):
    - Thematic → AI & Related Infra, Cleantech, Consumption, Corporate Governance, Defence Tech, Digital Economy & Infra, Electric Vehicles, ESG/Ethical Investing, Exports, Global Country Allocation, Global Megatrends, Government Schemes & Policy, Innovation & R&D, Macro Trends, Mobility Tech, Monopolies, New Age Tech, Ownership & Shareholding, Private Banking, Sector Rotation, Special Situation/Turnaround, Wealth Management, Others
    - Asset Allocation, Single Factor, Fundamental, Mixed Factors, Sector Tracker, Target Date, Quantitative, Smart Beta (several have their own sub-lists).
  - Subscription Type: Show all / Free access / Fee based.
  - Subscription Fee ("charged by the smallcase manager"): Any / Under ₹500/mo / Under ₹1500/mo / Above ₹1500/mo.
  - Minimum Investment Amount: Any / Under ₹5,000 / Under ₹25,000 / Under ₹50,000.
  - Constituents: All / IN Stocks & ETFs / Mutual Funds.
  - Rebalancing Frequency: Weekly / Monthly / Quarterly / Annual / On Need Basis / Others.
  - Include new smallcases (launched in last 1 year) — on by default.
- **Sort by** dialog: Popularity / Recently Rebalanced / Minimum Amount / Returns: High to Low with period sub-select (1M, 6M, 1Y, 3Y, 5Y); Reset + Confirm.
- **Card anatomy**: icon, name, type line ("IN Stock smallcase"), access badge (`Free Access`) or price line ("Starts from ₹1,050 / mo"), 1-line pitch, Min. Amount, headline return metric (5Y CAGR etc.), volatility chip, bookmark (watchlist) toggle.

### 6.4 smallcase detail (`/smallcase/{slug}-{SCID}`)
Header block: icon, name, "by {Manager}", Min. Amount, CAGR, volatility chip, type badge, access badge (Free Access / Subscribers Only / Subscribed).

Tabs: **Overview | Stocks & ETFs (or "ETFs & Weights") | Updates** (dot indicates unread update).

**Overview tab**
- Thesis/description with Read more expander; optional manager video bubble.
- **Performance**: absolute return since inception (e.g. 208.02%, 770.73%), area chart, range pills 1M/1Y/3Y/5Y/MAX, **SIP toggle** (SIP-mode performance), **Compare with** dropdown (benchmark/index overlay).
- **About the Manager** card: name, "Manages N smallcases", SEBI reg no., blurb, strategy tags (+N more), View manager details, dashed **Disclosures** link.
- **Understand smallcase costs and returns → View Costs & Returns** (see 6.6).
- Regulatory footer: "Performance data is not verified by PaRRVA… validated by an independent CA, in line with SEBI guidelines… not an advertisement" + Learn more.
- Sticky footer CTA: free → "Get free access forever / See more benefits / **Invest now**"; fee-based → "Get access for ₹8300/6m / See all plans & benefits / **Subscribe Now**"; subscribed+invested → Start SIP / Invest More.

**Constituents tab**
- **Rebalance timeline** (vertical): "Updated {Quarterly|on need basis|…} by {Manager}"; entries: next review date (or "Need basis"), then past events "Constituents updated" with +N/−N chips, "No change", collapsed span "1 Apr 2019 – 6 Oct 2025 +27 Rebalances", genesis "smallcase went Live {date}". Export/download icon. "Understand how rebalancing works → Watch Video".
- **Portfolio report** promo: free → "Get portfolio report"; fee-based → locked ("Subscribers only — why each constituent was picked, methodology, latest rebalance updates → Unlock portfolio report").
- **Holdings Distribution**: segment bars (e.g. Largecap 70% / Gold 30%; or Largecap 7% / Midcap 11.5% / Smallcap 76.5% / Debt 5%).
- **Constituents table**: grouped by segment with group weight subtotal; rows = instrument name + weight %. *Fee-based & unsubscribed → table replaced by lock: "Subscribe to see stocks/ETFs in this smallcase".*
- Free baskets: "You can also edit constituents and weights before investing. **Customize smallcase**".

**Updates tab** — manager posts/rebalance notes feed (weekly updates etc.).

### 6.5 Plans & benefits modal (fee-based)
"Get access to constituents & rebalance updates for {name}". Benefits list (e.g. Benefit from popular investing trends; Timely rebalance with rationale; Live YouTube Q&A). **Pick a plan** radio list (e.g. ₹8,300/6 months, ₹12,600/1 year) → Subscribe Now (→ payment; renewal auto).

### 6.6 Costs & Returns page ("Cost Adjusted Returns")
Per-smallcase cost card: Subscription Fee (₹12,600/y), Lump Sum ("Up to ₹100*/txn"), SIPs ("Up to ₹10*/txn"), Exit Load (NONE); footnote "*or 1.5% of investment amount, whichever is lower". (Also hosts a cost-adjusted-returns calculator further down.)

### 6.7 Manager profile (`/manager/{slug}`)
Name, SEBI reg no., firm blurb (Read more), strategy tags, "Managed by" people cards (experience, bio), trending strapline, **Popular smallcases by {manager}** cards (with "Starts from ₹X for 1y", min amount, CAGR, volatility) + "See all N smallcases", subscription scope note ("you need to subscribe to each fee-based smallcase separately"), benefits list (1-click orders, quarterly rebalance updates, SIPs), FAQs, registered address, CIN, SEBI disclaimer ("Registration… does not guarantee performance or assure returns").

### 6.8 Investments dashboard (`/investments`)
- **Net Worth** header (e.g. ₹2,53,50,509.07, +32.09% all time) + eye toggle.
- Alert banner (holdings mismatch → Fix now).
- Asset tabs: **smallcases | Mutual Funds | Fixed Deposits | IN Stocks**.
- smallcases tab: summary (count, current value ₹92.71L, daily change %; toggle Current Value ⇄ Daily Change); grouped list "IN Stock smallcases (8)", "Mutual Fund smallcases (0)" with empty-state (Explore CTA).
- Per-investment row: icon, name, badges (₹=fee-based), current value, daily change, kebab menu, contextual nudge strip: "Rebalance update available → See Update" / "51 days since last investment → Invest more".
- "You have exited from 1 smallcase → See all" (exited history).
- **What's next for you**: app-only investment score, "You have 2 active subscriptions → Manage subscriptions", Explore more.

### 6.9 Investment detail (`/details/{id}`)
- Header: name, manager, type badge, **Subscribed** badge; chevron → public smallcase page.
- **Performance**: Current Value + day %; Total Returns % and ₹; **Show Details** modal → Current Investment, Money Put In, Current Returns (% and ₹), Realized Returns (₹, can be negative), **XIRR** ("calculated only for investments started more than a year ago"), Dividends total.
- **smallcase Constituents**: "last rebalanced by {mgr} on {date}. You are yet to apply that rebalance." + View rebalance timeline; table of constituents with per-stock **Returns %** (sortable, expandable rows, View all).
- **ETF Dividends** card: "You have received ₹27,468.79 in dividends from {ETF}" + Show Details.
- **Portfolio report** upsell (subscriber perk).
- Sticky rebalance banner: "Weekly Update: Stocks/ETFs Change → See {date} Update".
- Primary CTAs: **Start SIP** | **Invest More**.
- **Manage your smallcase** list:
  - Manage constituents ("Buy/sell individual stocks & ETFs")
  - Set up SIP or reminder ("Set it once, invest every month")
  - View subscription ("Active till {date}", ACTIVE chip)
  - View orders (`/orders/{id}`)
  - Exit smallcase ("Partially or as a whole")
- "Need help with your investment? Chat with us."
- **All order-shaped actions open broker-login modal** if no active broker session; rebalance apply also gated by market hours ("Market is Closed…Notify me when market opens / See update").

### 6.10 Watchlist (`/watchlist`)
Count header; rows: name, "Watchlisted on {date}", Daily Change %, Returns % since watchlisted (footnote confirms), Invest/Subscribe CTA. Next steps: discover, create.

### 6.11 Subscriptions (`/subscriptions`)
Active list: smallcase, manager, Plan (₹9,999 annually), "Auto-renewing on {date}", kebab (cancel/manage). FAQs: auto-renewal explanation; lapse behavior — investments remain, updates stop.

### 6.12 Fees ledger (`/fees`)
Chronological list: {smallcase} — "Bought on {date}" / "Invested more on {date}" — Fees ₹118.00 (= ₹100 + 18% GST flat per buy/invest-more txn). FAQs on what/where charged (fees pulled from broker funds).

### 6.13 Account (`/account`)
Profile (name, +91 phone, Edit). **Indian Investment Accounts**: "Stocks & ETFs • Zerodha — Funds ₹15,207 (refresh)" (broker funds shown in-app); "Mutual Funds — Resume account setup" (separate MF onboarding). **Activity**: Subscriptions, Orders, Fees. **Support**: FAQs, Resources, Fine Print, Chat with support. Logout. Notification bell w/ count.

### 6.14 Credit (`/credit`)
Two products: **Loan against Mutual Funds** and **Loan against Stocks** (NEW), each "low-interest credit line → Check now" with eligibility nudges ("Recalculate credit limit", "Sync your stocks"). Comparison table: credit based on (MFs vs stocks), credit line ✓, digital process ✓, interest from 9.99% vs 10.25%, monthly repayment interest-only, closure anytime, nil early-closure penalty, disbursal 2 hrs vs next-day 12pm. Disclosure: facilitated by Essential Investment Managers Pvt Ltd (CASE Platforms group) with RBI-regulated lenders.

### 6.15 More (`/more`)
Create (own smallcase), Loan against MF, Loan against Stocks (NEW), Watchlist (count), Subscribed smallcases, **Invest in US Equities (NEW, by Tickertape)** — 7000+ US stocks/ETFs, instant account opening, IFSC/GIFT City regulated. App-download promo.

### 6.16 Create (`/create`)
Empty state: "Add at least 2 stocks to create a smallcase — you can then change weighting schemes and check performance." → **Add stocks** full-screen search ("Try RELIANCE or INFY"), similar-stock suggestions panel; then Next → weighting schemes (equal/custom…), backtested performance preview, name/save, invest. Custom smallcases behave like private baskets (appear in investments; buy fee ₹118 charged as usual; user's own baskets seen in fee ledger: "Adani all", "Bank bees without psus", "ArxyAI").

## 7. Key user flows

1. **Onboard**: mobile/OTP signup → browse freely → connect broker at first order (OAuth redirect to broker, e.g. Kite) → KYC/demat assumed at broker; PAN/KYC prerequisites.
2. **Discover → Invest (free basket)**: explore/filter → detail → Invest now → broker login (if stale) → order screen: amount ≥ min amount, lump sum vs SIP, fees disclosed → Confirm → batch market orders during market hours → fills → appears in /investments.
3. **Subscribe (fee-based)**: detail → See all plans → pick plan → pay (₹8,300/6m style) → constituents/report unlock, Subscribe→Invest.
4. **Rebalance**: manager publishes → push/email/in-app "Rebalance update available" → See Update (broker session + market open required) → diff preview (buys/sells) → Apply → batch orders; user may skip (state: "yet to apply").
5. **SIP**: Start SIP → pick amount/date → auto-reminder or auto-order each cycle → ₹10+GST per SIP txn.
6. **Invest More**: additional lump sum in same weights (₹100+GST).
7. **Partial/Full Exit**: Exit smallcase → choose whole or partial → sell orders → "Exited smallcases" history retained.
8. **Repair drift**: user sold stock on broker directly → "Incorrect smallcase holdings … Fix now" → reconcile ledger (archive/adjust) so future rebalances compute correctly.
9. **Manage constituents**: buy/sell individual constituents inside an investment (customize live position).
10. **Create custom smallcase**: add ≥2 stocks → weights → performance preview → save → invest/SIP.
11. **Credit**: check limit against MF/stock holdings → digital loan from partner lender.

## 8. Business rules & logic worth replicating exactly

- **Fees**: Buy/Invest-more = min(₹100, 1.5% of amount) + 18% GST (observed as flat ₹118 per txn in ledger). SIP = min(₹10, 1.5%) + GST. Rebalance & exit: platform-free (broker/statutory charges still apply). Fees are debited from broker funds, not a card.
- **Subscription**: manager-priced tiers (monthly/quarterly/semi/annual), auto-renew, per-smallcase (per-manager option exists — Niveshaay explicitly "subscribe to each separately"). Lapse ≠ forced exit; only content/updates stop.
- **Min amount** recomputes with live prices; catalog chips "Under 5k/25k/50k" filter on it.
- **Performance metrics**: since-inception absolute % on detail; CAGR windows chosen by basket age (5Y CAGR / 4Y / 2Y / NM returns); SIP-mode chart; XIRR only after 1 year of holding; Total vs Current vs Realized returns split; dividend accrual tracked per-ETF/stock.
- **Watchlist returns** measured from watchlisted date.
- **Volatility bucket** from constituent std-dev; displayed everywhere.
- **Gating matrix**: browse/watchlist = smallcase login only; constituents of fee-based = subscription; any order/order-history = broker session; rebalance order = broker session AND market open.
- **Market hours**: NSE/BSE 9:15–15:30 IST weekdays; closed-state modal offers "Notify me".
- **Trust/regulatory furniture** (SEBI-driven, must-show): PaRRVA non-verification disclaimer + CA validation note on every performance surface; manager SEBI reg numbers; disclosures pages; "registration does not guarantee performance"; fee-based data locked pre-subscription.
- **Notification surfaces**: bell (in-app), push (app), email; triggers: rebalance published, market-open reminder, SIP due, subscription renewal, holdings mismatch.

## 9. Monetization summary

1. Transaction fees (₹100/₹10 + GST caps as above) on every buy/invest-more/SIP — including user-created baskets.
2. Subscription revenue share on fee-based smallcases (platform % of manager plans).
3. In-house manager (Windmill Capital) subscription products.
4. Cross-sell: loans against securities (lender commissions), FDs, MFs, US investing (Tickertape), broker account-opening referrals.
5. B2B: smallcase Gateway (embedded transactions for partner apps) & broker-platform distribution deals.

## 10. Suggested data model (for replication)

```
User(id, name, phone, email, kycStatus, createdAt)
BrokerConnection(id, userId, broker, brokerUserId, accessToken, tokenExpiry, fundsCached, lastSyncAt)
Manager(id, slug, name, sebiRegNo, type[RA|RIA], bio, people[], strategies[], address, cin, faqs[])
Smallcase(id, scid, slug, name, managerId, type[STOCK|MF|US], categories[], volatility[LOW|MED|HIGH],
  accessType[FREE|FEE], description, rationale, launchedAt, rebalanceFrequency, nextReviewAt,
  minAmount(computed), metrics{cagr5y, cagr..., sinceInception}, benchmarkId)
SmallcaseVersion(id, smallcaseId, effectiveDate, label[+n/-n/no-change], constituents[{instrumentId, segment, weight}])
Instrument(id, symbol, exchange, name, type[STOCK|ETF|MF], marketCapBucket, price...)
Plan(id, smallcaseId|managerId, duration[1m|3m|6m|1y], price)
Subscription(id, userId, planId, startAt, renewAt, status[ACTIVE|CANCELLED|LAPSED], autoRenew)
Investment(id, userId, smallcaseId, versionApplied, status[ACTIVE|EXITED],
  currentValue, currentInvestment, moneyPutIn, realizedPnl, dividends, xirr, lastInvestedAt)
InvestmentHolding(id, investmentId, instrumentId, qty, avgPrice, returnsPct)
OrderBatch(id, investmentId, kind[BUY|INVEST_MORE|SIP|REBALANCE|EXIT|PARTIAL_EXIT|CUSTOMIZE],
  amount, fee, status[PENDING|PLACED|PARTIAL|FILLED|FAILED], placedAt, childOrders[])
ChildOrder(id, batchId, instrumentId, side, qty, fillPrice, brokerOrderId, status)
SipPlan(id, investmentId, amount, dayOfMonth, mode[AUTO|REMINDER], status)
WatchlistItem(id, userId, smallcaseId, watchedAt, priceAtWatch)
FeeLedgerEntry(id, userId, investmentId, kind, amount=100, gst=18, chargedAt)
RebalanceNotice(id, smallcaseId, versionId, publishedAt) / UserRebalanceState(userId, noticeId, applied|skipped)
Update/Post(id, smallcaseId|managerId, title, body, publishedAt)
Collection(id, title, smallcaseIds[], curatedMeta)
PendingAction(id, userId, type[MISMATCH|REBALANCE|...], payload, dismissedAt)
```

## 11. Replication blueprint (module cut for an existing project)

- **Catalog service**: smallcases, versions, managers, collections, filters/sort (param-driven URLs as §6.3), computed min-amount & metrics jobs (daily EOD + intraday price cache).
- **Market-data service**: instrument master, EOD + live quotes, index benchmarks; powers charts (1M/1Y/3Y/5Y/MAX, SIP simulation, comparison overlay).
- **Broker-integration service**: per-broker OAuth adapters, token vault w/ expiry, funds & holdings sync, batch order placement + fill webhooks, drift detection. (If replicating in India, easiest path is literally smallcase Gateway or broker APIs like Kite Connect.)
- **Portfolio-accounting service**: investments ledger, XIRR/returns math, dividends ingestion, realized PnL, exited history.
- **Rebalance service**: version diffing, notice fan-out, apply-order generation, market-hours guard.
- **Billing service**: flat txn fees (broker-funds debit), subscription plans, auto-renew, revenue share, fee ledger UI.
- **Engagement**: watchlist, pending-actions engine, notifications (push/email/bell), updates feed, trending/ranking jobs (most invested, most inflows, most watched…).
- **Create/customize**: basket builder with search, weighting schemes, backtest preview, private baskets.
- **Web/app shell**: 5-tab responsive PWA-style UI; access-gating middleware implementing the matrix in §8; SEBI-style disclosure components baked into templates.
- Sensible MVP order: catalog + detail + watchlist → broker connect + buy/exit → portfolio accounting → rebalance → subscriptions/fee-gating → SIP → create → credit/extras.

## 12. Sources

Live walkthrough of the logged-in web app (smallcase.com: /home, /search, /explore, smallcase & manager pages, /investments, /details, /watchlist, /subscriptions, /fees, /account, /credit, /more, /create), plus:
- [What is a smallcase (official)](https://www.smallcase.com/learn/what-is-smallcase/)
- [smallcase fees & charges (official)](https://www.smallcase.com/learn/smallcase-fees-and-charges/)
- [How subscriptions work (official)](https://www.smallcase.com/learn/how-does-smallcase-subscription-work/)
- [How to invest in smallcase (official)](https://www.smallcase.com/learn/how-to-invest-in-smallcase/)
- [smallcase Gateway developer docs](https://developers.gateway.smallcase.com/docs/overview) and [gateway.smallcase.com](https://gateway.smallcase.com/)
- [Wright Research: what is smallcase & how it works](https://www.wrightresearch.in/blog/what-is-smallcase-and-how-does-it-work/)
- [CASE Platforms corporate identity announcement](https://bestmediainfo.com/mediainfo/mediainfo-marketing/smallcase-technologies-introduces-case-platforms-as-new-corporate-identity-12398802) · [caseplatforms.com](https://caseplatforms.com/)
- [Buying smallcases with Zerodha Kite](https://www.smallcase.com/blog/buying-smallcases-with-zerodha-kite/)
- [Business Standard: $50M raise](https://www.business-standard.com/companies/news/smallcase-raises-50-million-in-funding-from-elev8-venture-others-125032800383_1.html)
