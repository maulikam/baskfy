# Chronicle HQ prompt — Baskfy pitch deck

Paste everything between the lines into Chronicle. Placeholders you must fill before or after generation are marked `[LIKE THIS]`.

---

Create a 13-slide investor pitch deck for **Baskfy** (baskfy.com) — momentum research and rules-based investment baskets for Indian (NSE) equities.

**Tone and style rules (apply to every slide):**
- Precise, unhyped, numerate. Credibility comes from showing the work, never from adjectives. No "revolutionary", no "disrupting".
- Never promise or imply an investment outcome. Never invent a performance number, market statistic, or user count — use only the figures given in this brief. If a slide needs a market-size figure I haven't given, insert a clearly marked placeholder instead of inventing one.
- Dense is good: this is a numbers-first professional tool and the deck should read like the product. Prefer a real table or a labelled diagram over an abstract illustration.
- Currency in ₹ with Indian formatting (lakh/crore). English (en-IN).
- Visual palette: cool near-white background (#f7f8fa), primary accent blue #1a5fc4, brand fill blue #4184f3, green-up/red-down only for market semantics. Clean, terminal-like, no gradients or stock photos of people pointing at charts.

**Slide 1 — Title.**
Baskfy — "One place for every strategy." Subline: Momentum research and rules-based baskets for Indian equities, with the arithmetic published at every step. Founder: Maulik Dave. [DATE / CONFIDENTIAL note].

**Slide 2 — The problem.**
Indian retail investors who want rules-based portfolios face a broken chain: screeners rank stocks and stop; basket platforms sell a portfolio but hide the method; "backtested" claims come with no visible assumptions, no point-in-time data, and no way to verify anything. Investors are asked to trust, not to inspect. The result: people either follow black-box products or run spreadsheets by hand across broker terminals.

**Slide 3 — Why now.**
Three converging forces: (1) the post-2020 boom in Indian demat accounts and DIY investing [INSERT VERIFIED FIGURE]; (2) smallcase proved retail will pay for curated baskets, but its model still hides the engine; (3) SEBI's tightening stance on unregistered advice rewards products built compliance-first, which Baskfy is. Frame this slide as "the market has learned to want baskets; it hasn't yet been offered a basket it can audit."

**Slide 4 — The solution: close the whole loop.**
One engine runs research → prove → decide → own. A four-step horizontal flow diagram: **Screen** (rank every NSE stock by momentum factors) → **Prove** (point-in-time backtest with published assumptions) → **Assemble** (turn a screen into a weighted basket, or split a portfolio into "sleeves" — a manager's basket + a rule you wrote + holdings you run by hand, each with its own capital) → **Own** (a costed order plan handed to execution; shares stay in your own demat, at your own broker). Tagline under the diagram: "The engine is the proof, not the pitch."

**Slide 5 — Product today (shipped and working).**
A dense two-column feature table, all real: 64 ranking factors across 14 universes; ~10,480 NSE instruments; price history to 2017 (3.5M+ daily bars) with 285 corporate actions applied; point-in-time backtest engine with fragility report and a 20-line assumptions panel; market breadth + regime dashboard over 136 NSE indices (146,000+ index rows); per-instrument factsheets; saved screens and 93-column CSV export; curated-basket catalog with versioned constituents and diffs; portfolio import with rebalance tracker, XIRR, drift and a fees ledger; a 10-broker connect catalog; and a read-only order plan handed to a separate execution desk. Label the slide "Built, not a mockup."

**Slide 6 — The differentiator: radical auditability.**
Three pillars, each one sentence: (1) **Published arithmetic** — every formula, membership rule and rounding rule is written down and the product is tested against the document; any number on screen traces to a nightly data version. (2) **No look-ahead, proven** — the backtest reader physically cannot return a row dated after the simulated date; a test asserts it. (3) **The web app never places an order** — execution happens only on a separate operator desk, only on explicit human confirmation, with brokerage and statutory costs shown before the confirm button, and plans that expire in 30 minutes. Close with: "We publish our gaps on the About page. A tool that hides its limits is asking to be trusted further than it has earned."

**Slide 7 — Proof it works: the founder is user #1.**
The founder runs the momentum strategy with his own capital through Baskfy's desk on Zerodha Kite — every feature exists because the operator needed it. The flagship basket, computed 21 Aug 2026: minimum investment ₹2,03,374; since-inception +14.53%; 1-year −3.71%; volatility LOW. Present both numbers plainly with the as-of date — the willingness to show a negative year IS the brand. All returns are price returns (splits/bonuses adjusted, dividends excluded) and the deck must say so in a footnote.

**Slide 8 — Market and competition.**
2×2 positioning map. X-axis: "research tool → investable product". Y-axis: "black box → fully auditable". Place: screener.in and Tickertape (research, semi-transparent, not investable); smallcase (investable, method hidden); advisory/PMS (investable, opaque, high-minimum); **Baskfy alone in the top-right**: investable AND auditable. One line of respect for smallcase as category-creator, then the wedge: "smallcase sells you a basket; Baskfy shows you why the basket is what it is."

**Slide 9 — Business model.**
Phase 1 (live): SaaS subscriptions for the research platform — monthly, yearly, and a one-time "Forever" plan; identical features, they differ only in duration; prices GST-inclusive. Phase 2 (built, behind feature flags): smallcase-style curated-basket subscriptions with fees of min(₹100, 1.5% of investment) + GST — currently computed but not collected, switched on after the pricing decision and regulatory gate. Use placeholder price cells [₹ MONTHLY / ₹ YEARLY / ₹ FOREVER] for slide layout.

**Slide 10 — Regulatory strategy as moat.**
Honest three-stage gate, shown as a timeline: **Today** — publishes rankings and research; explicitly NOT a SEBI-registered investment adviser; no advice, no execution from the web, disclaimers enforced by an automated copy-lint that fails the build on banned phrasing. **Next** — SEBI Research Analyst / adviser registration in progress with counsel, plus Zerodha Kite-Publisher partnership track. **Then** — execution and multi-user basket subscriptions unlock. Message: compliance is engineered into the codebase (risk ceilings locked out of the UI, DRY_RUN default, read-only web asserted by tests), so the regulatory step is a switch-flip, not a rebuild.

**Slide 11 — Roadmap.**
Four columns: **Done (Aug 2026)** — two codebases merged into one engine, 1,200+ passing tests, Postgres/Timescale, Celery pipeline, brand + web app live single-tenant. **Now** — deep history to 2011, AWS production deployment, shadow-mode validation Fridays. **Next 2 quarters** — multi-user beta, subscriptions on, curated basket layer (12-basket catalog target). **Gated** — in-app execution and broker OAuth, unlocked by SEBI registration. No dates on the gated column.

**Slide 12 — Team.**
Maulik Dave — founder-operator. Built the entire platform; runs his own capital through it daily. [ADD BACKGROUND: prior roles, years in markets/engineering]. Note the leverage: an AI-assisted solo development process shipped a two-product merge (45+ modules) in weeks — the cost structure of this company is structurally different.

**Slide 13 — The ask.**
[AMOUNT] to fund: SEBI registration and legal, cloud infrastructure, data licensing, and first hires in [ROLES]. Milestones the money buys: multi-user launch, [N] paying subscribers, curated basket catalog live. Close the deck with the one-liner: "Baskfy — momentum, ranked. Every number traceable, every trade yours."

---

## After Chronicle generates

Fill the placeholders: market-size figure (slide 3), plan prices (slide 9), your background (slide 12), and the ask (slide 13). Double-check it didn't sneak in an invented statistic — the deck's whole argument is that Baskfy never does.
