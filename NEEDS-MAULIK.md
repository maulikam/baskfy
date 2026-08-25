# NEEDS-MAULIK

Things only Maulik's hands can supply, queued by the autonomous run (CLAUDE.md §Autonomy charter,
`MERGE-PROMPTS.md` rule 6). **The run does not wait on these** — it continues with every module
that does not depend on them (rule 11) and returns the moment a dependency clears.

Nothing here is urgent unless marked so. Each entry says what is needed, why, what it blocks, and
what was done meanwhile.

**Four items closed during the run of 22 Aug 2026** — and one of them was the project's biggest
blocker:

| | |
|---|---|
| **3. A Kite login** | ✅ you did it; the bridge was then found broken and fixed (M23) |
| **6. The paid data tier** | ✅ answered by one API call, no browser needed — it is active |
| **8. Price or total return?** | ✅ **price return**, measured against the corpus 42–3 (M27) |
| **4. Corporate-action history** | ✅ **recovered from data already on disk** and applied (M24, M28) |

**Five closed now** — items 3, 4, 6, 8 and 10, including the one the M22 report called the biggest
blocker in the project.

**What is actually left:**

| | |
|---|---|
| **1** | Deploy the risk-ceiling lock — outside market hours. The only item needing your hands. |
| **9** | Review the 46 irregular corporate actions (probable demergers). Nothing blocked. |
| **11** | 231 instruments absent from Kite's master — a decision about whether it is worth filling them from the NSE archive. |
| **12** | Historical breadth is survivorship-biased. The page says so now; the fix needs NSE's index-change announcements. |
| **2, 5, 7** | Housekeeping — strangle collectors, two credential items, and a scratch directory to delete. |
| **14** | **Git remote URL** so local SC/D3 commits can be pushed. |
| **13** | ✅ **D3 written** (posture B in `docs/DECISIONS-MERGE.md` §D3, 23 Aug 2026). OAuth gate flipped. Counsel checklist C1–C3 (algo ID, research vs advice, RA/empanelment) remains open but **non-blocking** — see §13 below. |
| **D7 / D10** | ⚠ UNREVIEWED stubs in `docs/DECISIONS-MERGE.md` (Track B flags stay false; no public market-data API). Amounts + licensing opinion still need Maulik / counsel before any flag flip. |
| **15** | **Fundamentals live fill** — pipeline step exists (T9.1, NSE quote-equity folded into snapshots). Live `fundamental_daily` is still empty until a night (or a one-off fetch) runs against NSE. See §15. |
| **16** | **Broker credentials for nine brokers** — Upstox, Angel One, Fyers, 5paisa, Dhan, ICICI, Kotak, HDFC (Groww has no public API). Only `BASKFY_KITE_*` exists today. Blocks consolidated holdings for every non-Zerodha account. See §16. |

---

## Open

### 3. A Kite login — **CLEARED 22 Aug 2026** ✅
**Status:** done. You logged in; the bridge now works end to end. · **Raised:** M9, 22 Aug 2026

**What happened when it arrived:** the bridge wrote the desk's token store and not the pipeline's,
so `make doctor` still said `[DOWN] kite` after a successful sync. Fixed (M23.1) — one login now
writes both. `make doctor` reports `[OK] kite` for the first time in this project.

**Also learned, and it is operational:** a Kite token dies when a *new* one is minted, not only at
06:00. Two tokens were invalidated mid-session by later logins on the box. **Re-run
`make token-sync` after any login**, not just the first of the day (M23.4).

Item 6 below is answered too: the paid historical tier **is** active, and 2011 genuinely reaches
back. The original text follows.

---
#### (original entry)
**Status:** open, **not blocking the run** · **Raised:** M9, 22 Aug 2026

`data/.kite_token.json` is from 19 Aug and Kite mints tokens daily with no refresh, so it is
expired (`TokenException`). M9 wanted 2011→today in `ohlcv_daily`.

**What is needed:** one Kite login, exactly as you do it now — `./run.sh` on the box (or
https://desk.modelbasket.in/), click Login, complete the 2FA. **Then tell me, or just leave it: I
can now fetch the token myself.**

`make token-sync TARGET=momentum-desk` (M18, point 2) reads the token the box already holds over the
same SSH connection `deploy/sync.sh` uses, writes it into the laptop's encrypted store, and verifies
it with one `profile()` call. **The Kite app's Redirect URL stays `desk.modelbasket.in/callback`** —
no second redirect was registered, nothing about the live app changed. The token is never printed.

It is built and exercised end to end against the box. Run this morning (22 Aug, 07:20 IST) it read
Friday's token, correctly refused to store it, and said why:

> The api_key matches the box's, so the key is not the problem: the token on the box has expired.

**So the only manual step left is the login itself, once a day.** Kite mints tokens per human,
through a browser, and expires them at ~06:00 IST the next morning; nothing on this side removes
that. The bridge removes the *second* manual step — getting that token onto the laptop.

**What it unblocks:** *depth only*. `baskfy_worker.backfill` pulls daily candles from Kite, which
is the only source that reaches before 2024.

**What it does NOT block, because the run already did it without Kite:**

- `ohlcv_daily` now holds **1,145,922 bars, 2024-01-01 → 2026-08-21**, 2,553 instruments, from the
  NSE bhavcopy. The recent gap was filled during M9.
- **The product's own window is fully covered.** `docs/01` §2.13 sets `DATA_START_DATE` to
  2024-11-01, so the screener has ten months of headroom.
- **The parity gates M11 and M12 need about one year of history and have two and a half.**

**What stays wrong until it arrives:** the fifteen-year backtest is unrunnable, and anything
reading `high_all_time` is *wrong rather than absent* — it is computed as a running maximum over
whatever history exists, so on a 2024-start series it is a 2024-onward maximum wearing an all-time
label (`decile-blueprint/docs/DECISIONS.md` §21.2). The parity work treats those columns as
unverifiable rather than comparing them.

**Blocks:** deep history, and the backtests that need it. Not the merge's gates.

---

### 1. Deploy the risk-ceiling lock to the Mumbai box — **non-trading hours**
**Status:** waiting for a deliberate deploy · **Raised:** M4, 22 Aug 2026

M4 moved the four risk ceilings — `RISK_MAX_DAILY_LOSS_PCT` (the kill switch),
`RISK_POSITION_HEADROOM`, `RISK_GROSS_MULTIPLE`, `RISK_MAX_ORDERS_PER_DAY` — out of the desk's
`/settings` page and into `.env`-only configuration (`DECISIONS-MERGE.md` M4.1, your answer).

**What you need to do:** nothing yet. When you next deploy the desk, do it **outside market hours**
and know that afterwards:

- the "Risk limits" group on `/settings` is read-only — it still *shows* the ceilings in rupees,
  it no longer lets you type over them;
- changing one is `nano .env` then `sudo systemctl restart momentum-web`;
- the values in force are logged at every startup, so `journalctl -u momentum-web | grep "risk
  ceilings"` answers "what were the limits that day?" — this replaces the `settings_audit` row that
  locking them removed.

**Nothing in force changes.** No `RISK_*` override had ever been stored (the `settings` table holds
one row, `REGIME_ENABLED=true`), so all four were already running on their `.env` defaults.

**Blocks:** nothing. The repo and the box simply differ until you pull.

---

## Cleared

_(nothing yet)_

---

## Not needed yet, but coming

- **M9 — the daily Kite login.** The backfill needs a valid access token. The run will first try
  the desk's existing token at `data/.kite_token.json`; only if that is expired and no login has
  happened will it queue an ask here. Kite tokens expire nightly with no refresh, so this is the
  one step no automation can remove.
- **M21 — deleting `../_baskfy_subtree_tmp/`.** ~~Coming.~~ **M21 is done (22 Aug 2026), so this
  is now open — see item 7 below.**

### 2. The box's strangle collectors will point at moved paths after the next deploy
**Status:** informational, no action until you deploy · **Raised:** M6, 22 Aug 2026

M6 froze the options lab into `frozen/strangle/`. Per the module's own instruction the live box
was **not** touched: `strangle-collect@{nifty,banknifty,sensex}.timer` still runs at 09:20 and
still writes to `data/outputs/strangle_*`, which was left in place because the observation series
cannot be rebuilt.

**When you next deploy the desk from this tree**, those units will invoke `-m scripts.strangle`,
which has moved. Decide then between three options:

1. **Retire the collectors.** The straddle record stops growing. Everything collected so far is
   kept.
2. **Repoint them** at `frozen/strangle/kite-momentum-rebalancer/scripts/strangle.py` — the
   subsystem still runs, it is just not maintained or gated.
3. **Thaw** (`frozen/strangle/README.md` has the exact `git mv`s) and set `OPTIONS_ENABLED=true`.

Nothing breaks until you deploy, and the equity desk is unaffected by all three.

**Blocks:** nothing.

### 4. Corporate-action history — **SOLVED AND APPLIED, 22 Aug 2026** ✅
**Status:** done. Source found (M24), convention measured (M27), actions written (M28).
· **Raised:** M12 · **Closed:** M28

**285 actions written, 151,922 bars rebuilt, `corporate_action` 4 rows → 289.** The
`/baskets` contamination banner went **41 → 5**; M12's top-25 membership went **23/25 → 25/25**;
`SHILPAMED`, which read #10 → #57, now reads #10 → #12.

You do not need to buy a data vendor for this. The one thing left is item 9 — reviewing the 46
recovered actions that are almost certainly demergers. The original entry follows.

---

#### (original entry)

**You do not need to buy a data vendor.** Kite's historical bars are *adjusted* — measured, and
contrary to what `docs/09` assumed — while `ohlcv_daily.close_raw` holds the raw bhavcopy print.
The ratio between the two series is therefore the missing corporate-action history itself.

**85 actions recovered across 65 of the 271 corpus symbols**, 83 of them confirmed by requiring the
ratio to be flat five trading days either side. Evidence:
`decile-blueprint/reconciliation/RECOVERED-ACTIONS.md`. Reasoning: `DECISIONS-MERGE.md` M24.

**What still needs you — see item 8.** 47 of the 85 are splits and bonuses, which are simply wrong
data and mine to fix. The other 38 are dividends, and adjusting for those changes momentum from a
price return to a total return. That is a strategy decision, not a bug fix.

**Also urgent and already acted on:** `make backfill` as built would have written Kite's *adjusted*
prices into `close_raw`. It was **not run** (M24.1).

The original entry follows.

---

#### (original entry)

`corporate_action` holds **four rows**. NSE's API serves only a recent window
(`decile-blueprint/docs/DECISIONS.md` §21.8), so almost no split, bonus or dividend before the last
few weeks is known to the system.

**Consequence, measured:** **40 of the 271 symbols in the desk's own weekly scan — 14.8% — carry
unadjusted price history.** `NESTLEIND` still shows its 10:1 split as a 90% one-day fall;
`BAJFINANCE`, `ANGELONE`, `SHRIRAMFIN` and 36 others likewise. Every factor whose window crosses
one of those dates is wrong for that name — which is how `SHILPAMED` went from #10 to #57 in the
M12 comparison, and why its one-year return reads −21% where the desk was traded on +55%.

**What is needed:** a corporate-actions source that serves *history*, not a recent window. The
candidates are Kite (same login as item 3), NSE's archive under a different endpoint than the one
the API exposes, or a paid vendor.

**Why it matters more than it sounds:** the adjustment code is correct — it reproduces CUPID's
split and both bonuses exactly — so this is purely missing input, and it is currently the dominant
cause of both parity gates failing. It is more load-bearing than the window question M11 raised.

**Blocks:** M13 (rule 7 opens it only on empty delta tables). Does not block M15–M20.

### 6. The paid historical-data tier — **ANSWERED 22 Aug 2026, no browser needed** ✅
**Status:** confirmed active. · **Raised:** M18, 22 Aug 2026

One `historical_data()` call settled it: bars come back, not a 403, and a 2011 request returns
January 2011. **The add-on is active and fifteen-year history is servable.** The browser check
below is no longer needed; kept for the reasoning.

---

#### (original entry)

**What to do, once:** open https://developers.kite.trade, find the app whose api_key fingerprints to
`50c5bd947587`, and check whether the **historical data** add-on is active. It is ₹500/month, billed
separately from the ₹2,000/month app.

**Why it matters:** `profile()` works on every tier, so a successful `make token-sync` does **not**
prove we can pull bars. If historical data is not subscribed, `kc.historical_data()` returns **403**
with a perfectly valid token — and that failure looks exactly like an auth problem while being
nothing of the kind. Knowing the answer in advance is the difference between a five-minute fix and
an afternoon spent re-checking the token bridge that is working fine.

**What it gates:** item 3's deep-history backfill (M9) and, through it, the fifteen-year backtest.
Not the merge's parity gates.

**Blocks:** nothing today — but check it before the next backfill attempt rather than during one.

### 7. `../_baskfy_subtree_tmp/` is yours to delete
**Status:** open, zero urgency · **Raised:** M21, 22 Aug 2026

The scratch copy the `git subtree add` worked from. **No agent will ever delete it** — that rule is
in `MERGE-PROMPTS.md` §M21.4, and it exists because an agent cannot know what a rollback copy is
worth to the person who might need it.

**Safe to `rm -rf` once you have skimmed `docs/00-merge-status.md` and are content with it.** Both
histories live inside `baskfy/.git` — proved four separate ways at M1, because `git log --follow`
cannot prove it for a subtree — so nothing is lost with it. Until then it costs disk and nothing
else.

### 8. Momentum: price return or total return — **ANSWERED BY MEASUREMENT, 22 Aug 2026** ✅
**Status:** settled. **Price return.** You chose option (2) — let the corpus decide — and it did,
decisively. · **Raised:** M24 · **Closed:** M27

* **The vote: price 42, total 3** over 45 deciding rows.
* **The exact matches are the stronger signal.** On 1M, 3M and 6M the price convention matches
  **25 of 25** dividend-paying symbols *exactly* at stored precision. The total convention matches
  only where no dividend falls inside the window and the two are identical anyway.
* **Over all 271 rows:** applying splits and bonuses moves exact matches up at every window
  (6M 255→261); applying dividends on top pushes them *below* today's unadjusted baseline.

So: apply the 47 splits and bonuses, do not apply the 38 dividends. Evidence and the regenerable
measurement are in `decile-blueprint/reconciliation/RECOVERED-ACTIONS.md`; reasoning in
`DECISIONS-MERGE.md` M27.

**A switch to total return stays a recompute, not a schema change** — every action is recorded with
its ex-date, factor and class, `close_raw` is untouched, and the derivation marks its rows with a
source. If you ever want the other convention, it costs one pipeline re-run.

**Two things it confirmed for free:** M11's return-base fix and M24's recovered splits are both
right, because neither could produce a 25-of-25 exact match if it were wrong.

The original question follows.

---

#### (original entry)
**Status:** open, **blocks half of the corporate-action fix** · **Raised:** M24, 22 Aug 2026

This is the only genuinely new question from 22 Aug, and it is yours because it changes what the
strategy trades rather than whether the code is right.

Recovering the missing corporate actions (item 4) turned up **two kinds**:

* **47 splits and bonuses.** An unapplied split is simply wrong data — `NESTLEIND` did not fall 90%
  in a day. I am treating these as a correctness fix and will apply them.
* **38 dividends.** Kite back-adjusts for these too. Applying them makes every return in the
  screener a **total return** rather than a **price return**.

**Why it matters:** a stock yielding 4% gains 4% of momentum a year it would not otherwise have.
Across a 271-name universe that reorders the ranking, which reorders the basket, which changes what
the desk buys. High-yield names (PSU banks, `NATIONALUM`, `COALINDIA`-shaped stocks) rise; zero-yield
compounders fall.

**What I need from you:** which one the product means. Two ways to answer, and the second is better:

1. Say which you want.
2. **Let the reference corpus answer it.** `data/uploads/*.csv` is the answer key the whole merge is
   graded against, and it was produced by the reference product. Whichever convention it used will
   match on the dividend-paying names and mismatch on the other. That is a measurement, not an
   opinion, and I can run it — it is the same arbitration M12 already performs.

**Unless you say otherwise, I will do (2)**: measure which convention the corpus is consistent
with, adopt that one, and record it. The corpus outranks my preference and yours.

**Blocks:** the dividend half of item 4. The split/bonus half proceeds regardless.

### 9. **Review the 46 irregular corporate actions** — the one thing M28 could not settle
**Status:** open, low urgency, **nothing is blocked by it** · **Raised:** M28, 22 Aug 2026

M28 recovered 285 corporate actions from the gap between Kite's adjusted bars and the raw bhavcopy
print, and wrote them. **239 land on shapes a split or bonus actually produces** — 2:1 (84), 5:1
(47), 10:1 (39), 3:1 (14), 4:1 (12), 3:2 (11). Those are not in question.

**46 do not**, and the recognisable ones are all 2025 demergers:

| symbol | ratio | almost certainly |
|---|---|---|
| ITC | 22:19 | the ITC Hotels demerger |
| SIEMENS | 21:16 | Siemens Energy India |
| VEDL | 21:11 | the Vedanta demerger |
| RAYMOND | 23:14 | Raymond Lifestyle |

Nothing issues a 19:17. **They are applied** — the price step is real and Kite adjusts for it — and
each row is tagged `raw['shape'] = 'irregular_probably_demerger'` rather than being called a split.

**What I would like you to check:** the residual risk is that a **dividend above about 5%** also
resolves to a small fraction (21:20, 20:19) and would have been written as a share-count action —
which would quietly violate the price-return convention M27 measured. Corpus-level parity says it
is not currently hurting: every window M11 fixed improved. But nothing *proves* no dividend slipped
through, and I would rather say so than let it sit unnamed.

**To see them:**
```sql
SELECT i.symbol, ca.ex_date, ca.ratio_from, ca.ratio_to, ca.raw
FROM corporate_action ca JOIN instrument i ON i.id = ca.instrument_id
WHERE ca.raw->>'shape' = 'irregular_probably_demerger'
ORDER BY i.symbol;
```

**To undo any or all of it**, one predicate and a reprocess — see `RUN-AND-TEST.md` §3b. Nothing
here touched `close_raw`, so the reversal is total.

**The real fix, when it is worth paying for:** a corporate-actions vendor feed with real event
types. It would settle split-versus-bonus and demerger-versus-dividend, which price data cannot.

**Blocks:** nothing.

### 10. `/baskets` took 67 seconds — **DONE, 22 Aug 2026** ✅
**Status:** fixed. You chose the precompute; it is built. · **Raised:** M29 · **Closed:** M30

**67 seconds → 0.21.** The basket is computed once a night as pipeline step 11, after `publish`,
into `basket_snapshot`, and the page reads a row. Same content.

It runs *after* publish because the basket is identified by the `data_version` publish bumps, and
*last* because it is a cache — it records its own failure rather than raising, so it can never hold
back a good `data_version`. `/baskets` still falls back to computing live when no snapshot exists,
because a page that 404s on a cold cache is worse than a slow one.

The original entry follows.

---

#### (original entry)
**Status:** open, **decision needed, not hands** · **Raised:** M29, 22 Aug 2026

The deep backfill took `ohlcv_daily` from 1.1M bars to 3.5M. `/baskets` computes its basket live
from those bars, so the page went from about a second to **67**. Nothing about the answer is wrong;
it is doing three times the work.

Three ways out, and I did not want to pick one in the last minutes of a session:

1. **Precompute the scan in the nightly chain** — my recommendation. The pipeline already does this
   work every evening; the page would read a stored result instead of recomputing it. Fastest page,
   and the basket becomes a record rather than a live calculation.
2. **Bound the window the basket engine loads.** It needs about a year of bars to score; it is
   currently handed everything. Smallest change, keeps the page live.
3. **Cache the computed scan** behind the existing Redis screen cache. Cheap, but the first
   request after every publish still pays the 67 seconds.

**Tell me which and I will build it** — (2) is roughly an hour, (1) is half a day.

**Blocks:** nothing else. Every other page is unaffected; the desk console is unaffected.

### 11. **231 instruments Kite cannot serve at all**
**Status:** open, low priority, **needs a decision about whether it is worth it** · **Raised:** M29

231 of the 2,525 tradable EQ/BE instruments have **no Kite instrument token** — and this is not a
merge bug: checked against Kite's live dump of 10,222 symbols, **0 of the 231 appear in it**.
`ABAN`, `ALPHAGEO`, `AKSHARCHEM`, `AHLWEST` and 227 others are in NSE's listings register and not
in Kite's instrument master.

They have bhavcopy bars from 2024 onward and **nothing before**. Kite cannot be asked for them at
any depth, so no re-run helps.

**The one source that could fill them** is the NSE bhavcopy archive, which is keyed by symbol
rather than instrument token — which is exactly why it reaches names Kite cannot. That same module
would also fix the convention seam in the deep segment (`DECISIONS-MERGE.md` M29.3): with real
exchange prints for 2017–2023, the whole history could be price-return adjusted instead of the
first six years carrying Kite's dividend adjustment.

**My read:** worth doing before the fifteen-year backtests are taken seriously, not worth doing
before you have looked at the screener. These 231 are small and mostly illiquid; none is in the
momentum basket.

**Blocks:** nothing today.

### 12. **Historical breadth is survivorship-biased** — the page now says so
**Status:** open, **a decision, not work** · **Raised:** M32, 22 Aug 2026

Breadth asks "what percentage of NIFTY 50 was above its 200-DMA on this date", which needs the
constituents **on that date**. NSE publishes today's. **Kite has no constituents endpoint at all** —
its API surface was enumerated, not assumed.

So the most recent published membership is carried backwards and stored `source = 'derived'`, the
value `docs/09` reserved for it. A company dropped from an index after falling is missing from its
own history, which makes the past look healthier than it was.

**This is now on the page** (M33), in the History section, in a reader's words rather than the
schema's — and it says the live gauges are unaffected, because they are.

**What would fix it properly:** NSE's index-change announcements, reconstructed into
`index_member_daily` with real dates. Nothing in this repo has that source. Until then the marker
is what lets a backtest exclude the uncertain period.

**Blocks:** nothing. The gauges are exact; only the history carries the bias.

### 5. Two small credential items from M16
**Status:** informational · **Raised:** M16, 22 Aug 2026

1. **Set `KITE_TOKEN_ENCRYPTION_KEY` in the desk's `.env`.** The access token is now encrypted at
   rest. Without this variable the desk generates a key *beside* the token — which protects the
   secret from being copied out in a backup and **not** from anything that can read files as your
   user. Setting it puts the key somewhere the ciphertext is not:
   ```
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. **`~/baskfy-safety/2026-08-22/data/.kite_token.json` contains a plaintext token.** It is the M0
   safety copy, taken before the encryption change, and the token in it is expired. Worth deleting
   that one file when you next look at the safety copy. The live one at
   `kite-momentum-rebalancer/data/.kite_token.json` is also still plaintext and expired; your next
   Kite login overwrites it with ciphertext automatically.
3. **Observed on 22 Aug during the M21 drill:** the desk logs *"the token blob could not be
   decrypted; the encryption key has probably been rotated"*. That is the bootstrap key beside the
   token no longer matching the blob — harmless, because the token in it expired at 06:00 anyway,
   and your next login writes a fresh pair. Mentioned only so the warning is not a surprise. Setting
   `KITE_TOKEN_ENCRYPTION_KEY` per (1) stops it recurring, because the key then lives somewhere
   that is not regenerated.

**Blocks:** nothing.

### 13. D3 — regulatory posture (SEBI / RA) — **CLEARED 23 Aug 2026** ✅
**Status:** done (engineering unlock) · **Raised:** M41, 23 Aug 2026 · **Cleared:** Tree 3

**Written answer:** posture **B** in `docs/DECISIONS-MERGE.md` §D3 ⚠ UNREVIEWED.
`BROKER_OAUTH_REVIEW.signed_off=True` with `decision_reference="DECISIONS-MERGE.md §D3"`.

**Still for Maulik / counsel (non-blocking) — counsel checklist (D3 follow-ups):**

| # | Ask counsel | Why it matters | Blocks engineering? |
|---|---|---|---|
| C1 | **Algo ID / exchange registration** when Baskfy supplies order *plans* to a third-party Kite app (vs personal algo in own account at <10 orders/sec) | Per-order tagging / broker empanelment if B lands on the “algo supplied to others” side | No — OAuth + encrypted token store already unlocked |
| C2 | **Research vs advice** for ranked baskets; what changes when personalised to existing holdings | Marketing claims, disclaimers, RA paperwork | No — does not gate connect redirects |
| C3 | Whether posture B needs **RA registration** and/or **Kite-Publisher / empanelment** beyond the engineering unlock already taken | Product claims and SEBI filing path | No for sole-tenant operator desk; **yes before multi-tenant paid**. P4.1/P4.3 engineering started 24 Aug 2026 without waiting on this. |

Those filings do not gate OAuth redirects. See also `docs/DECISIONS-MERGE.md` §D3 (posture B) and
§D7 / §D10 (Track B flags and display licensing remain UNREVIEWED continuity stubs).

#### (original entry — superseded)
**Status:** open · **Raised:** M41, 23 Aug 2026

**What is needed:** a written answer to `docs/06-decisions-required.md` D3 in
`docs/DECISIONS-MERGE.md` (posture A / B / C). Intended is B: publish baskets, users execute in
their own broker accounts via OAuth.

**What it blocks:** flipping `baskfy_core.broker_connections.BROKER_OAUTH_REVIEW.signed_off`,
live redirects from `/brokers`, per-user token storage, holdings sync from HDFC / Kotak / ICICI /
peers, and the rest of Phase 4.

**What was done meanwhile (M41):** the ten-broker connect catalog, `/brokers` UI, gated
`POST /brokers/{id}/connect` (always `oauth_available: false` until the gate flips), and the
normalized holdings row shape. CSV import on `/portfolios` remains the way to load a book.


### 15. Fundamentals source — **code landed T9.1; live fill still needs a pipeline night**
**Status:** source decided · **Raised:** UI route audit, 23 Aug 2026 · **Updated:** Tree 3 Numbers, 24 Aug 2026

**What is needed:** a night (or a one-off `equity_fundamentals` fetch) against NSE, with a live
cookie-primed session, so `fundamental_daily` on the latest published date is no longer empty.
The source is no longer an open product decision: NSE `quote-equity`, issued size × `close_raw`.

**Why:** the join in `compute_factors` is ready. The backfilled DB still has **zero** non-null
`marketcap_cr` on the latest date until that fetch runs (~2,500 symbols at the NSE throttle).

**What it blocks:** accurate decile bucketing, M-cap column on `/`, P/E on instrument factsheets
on live (non-seed) data, until the first successful fetch.

**What was done:** T9.1 — parser, archive-then-parse, worker upsert, folded into
`refresh_index_snapshots`. Errors are recorded; they do not fail the night.

---

### 14. Git remote / push — **SSH key for agent push**
**Status:** partial · **Raised:** Tree 4, 23 Aug 2026 · **Updated:** Tree 7, 24 Aug 2026

**What is needed:** Agent (or Maulik) able to `git push` to `origin`. Remote **exists**:
`origin git@github.com:maulikam/baskfy.git`, branch `developer` tracks `origin/developer`.
Agent `git fetch` failed with `Permission denied (publickey)` — the laptop user may push;
this agent session cannot.

**What it blocks:** Agent-driven push; confirming GitHub Actions has run on a real push.

**What was done meanwhile:** Tree 5 local CI baseline; Tree 7 populated `cb_metrics` (1 row for
`momentum-scan`); manager route `/manager/[slug]`; T7.2 investment contract = mark-as-invested.

---

### 15. Mark-as-invested UI/API — **done in Tree 8**
**Status:** closed · **Raised:** Tree 7, 24 Aug 2026 · **Closed:** Tree 8, 24 Aug 2026

**What was needed:** Product implementation of T7.2 (b): create `cb_investment` when the user
confirms they invested at the broker (no web execute).

**What landed:** `POST /api/v1/cb/investments/mark` + `mark-invested-form` (T8.1). Desk fills
promote `PLANNED` → `EXECUTED` via Beat `baskfy.cb.sync_batches` (T8.2). Costs, SIP reminder,
rebalance email-once, and drift-repair shipped in the same tree (T8.3–T8.6).

**What it no longer blocks:** End-to-end investor journeys on web (still no web order path).


---

### 17. **The NSE fetch stalls about every 600 requests — and this now affects a nightly run**
**Status:** open, **not blocking** (a supervisor works around it) · **Raised:** Tree 3
Fundamentals, 25 Aug 2026

**What is needed:** a decision on the fix, and whoever owns docs/09's fetch discipline to make it.
Nothing of yours by hand — this is engineering, filed here because it is the one thing this tree
found that it deliberately did **not** fix.

**What happens.** Twice during the first real fundamentals fill (at ~600 and ~1,180 symbols) the
fetch hung: `provider retry` repeating, archived-file count frozen, process at 0% CPU, and one
ESTABLISHED idle socket to Akamai — while `curl` against the same URL answered 200 in 0.3s from
the same machine. Seventy-five seconds of observation, zero progress, against a 30-second timeout.

**Why the guards miss it.** `call_with_retry` is bounded correctly, so the hang is inside a single
`client.get`. `nse_request_timeout_seconds` is 30, but httpx applies `read` **per socket read**,
not per request — a peer that goes silent mid-response is never timed out, and the retry budget is
never reached.

**Why it matters beyond the backfill.** Step 6 of the nightly pipeline now genuinely fetches
~2,540 quotes (before this tree it fetched 10,481 Akamai deny pages in seconds and stored
nothing). The same stall would hang a night rather than a backfill.

**The fix worth making:** a total-request deadline around `NSEProvider._fetch` — a wall-clock
bound no single request can outlive, whatever the socket does — with a test that a trickling
server is abandoned. Not done here on purpose: `keepalive_expiry` and per-phase `httpx.Timeout`
are both plausible and neither is *known* to address a trickle, and a speculative fix that looks
like a fix is worse than a named open item.

**What was done meanwhile:** `tools/tree3/drive-fill.sh` supervises the fill — bounds each
invocation, restarts with `--resume`, stops rather than spins if a round makes no progress. Sound
rather than a hack because the fill commits every 25 symbols and never refetches an archived key.
See `docs/DECISIONS-MERGE.md` §T3F.6.

---

### 16. **Broker credentials — nine brokers, nine sets of hands-only steps** (tree 5, 25 Aug 2026)

**Status:** open, **not blocking the run** · **Raised:** tree 5 leaf C2, confirmed by C1
· **Urgency:** none of it is urgent; all of it is a prerequisite for the next step

**What is needed.** A developer/API registration at each broker below, and the keys it issues.
**The only broker credentials that exist anywhere in this project today are `BASKFY_KITE_*`**
(`.env.example:56-63`) — Zerodha's, and nothing else. Every other broker in the catalog is a name
with no key behind it.

**What it blocks, precisely.** Consolidated holdings for **any non-Zerodha account**.
`_HOLDINGS_WIRED == {"zerodha"}` and only Zerodha has a holdings adapter, so nine of the ten
brokers a user can see cannot return a position. It also blocks a consolidated live net worth
figure, which is why tree 5 deliberately did not build one: it would have been one live number
plus nine absent ones, presented as if equivalent.

**What was done meanwhile.** The product stopped claiming otherwise. The catalog's
`holdings_sync="ready"` rows went from **8 to 1** — the seven over-claimants (kotak, icici,
upstox, angelone, fyers, fivepaisa, dhan) now read `"planned"` — and a live credential leak was
closed on the way: `POST /brokers/upstox/connect` had been redirecting to Upstox's authorize
dialog carrying **Baskfy's Zerodha app key**, on a signed-off path. See `DECISIONS-MERGE.md`
§PM7 and §PM8.

| Broker | What only your hands can obtain |
|---|---|
| **Upstox** | A developer app: **API key + API secret + a registered redirect URI** |
| **Angel One** | SmartAPI registration: **API key + secret + TOTP/2FA enrolment** on the account |
| **Fyers** | An API v3 app: **app id + secret + redirect URI** |
| **5paisa** | An OpenAPI app: **user key, encryption key, client code** |
| **Dhan** | A **DhanHQ access token** generated from the web console — **per account** |
| **ICICI Direct** | Breeze app registration: **API key + secret**, plus its session flow |
| **Kotak** | **Neo API enablement on the account** + a partner app key |
| **HDFC** | A **partner agreement / empanelment**. There is no self-serve developer programme |
| **Groww** | **Nothing to ask for** — no generally available public trading API. Listed so it is not repeatedly re-investigated |

**How to hand them over safely.** Into `.env` only, never into a file that is tracked, never
pasted into a chat or an issue. Follow the existing `BASKFY_KITE_*` naming for whatever prefix
each broker ends up with. `.env` and `data/` stay untracked (CLAUDE.md §Safety rails); no agent
should ever echo them.

**Order of value, if you only do some.** Upstox, Angel One, Fyers and Dhan are self-serve and
already have known authorize URLs in the code (`_WIRED_AUTHORIZE`), so they are the cheapest to
finish. ICICI and Kotak need an account-level enablement step. HDFC needs a commercial
conversation. Groww needs nothing because there is nothing to get.

**One label problem is still open and does not need you.** `adapter_wired` reports true for five
brokers of which one works, and `capabilities.oauth == "ready"` for eight of which kotak and icici
have no authorize URL at all. That is an engineering fix, recorded in `docs/00-merge-status.md`
under Tree 5 §NOT done item 3.

## §17 — Third-party managers: what only Maulik (or counsel) can supply

Tree-3 built the identity, the lifecycle, the registration capture and the publishing verb. Four
things are blocked on a human and were deliberately **not** guessed.

### 1. The revenue-share rate — D7, blocking

`cb_manager_revenue_share.rate_bps` has **no default**, on purpose. Nothing in the codebase knows
what share a third-party manager keeps, and inventing one would bury a pricing decision in a
migration where it never surfaces for review again.

**Needed:** the number (in basis points), whether it varies by manager or basket, and whether it
is charged on subscription revenue, on AUM, or per investment.
**Blocks:** any actual payout. The schema and the dark endpoint are in; nothing computes money.
**Meanwhile:** the surface 404s while `BASKFY_FEE_COLLECTION_ENABLED` is false, which it is.

### 2. Which registration a manager must hold — D3 / counsel C1–C3, blocking

The product now *captures* SEBI registration (type, number, validity window, verification date)
and checks the number's **format**. It does not and must not decide who is permitted to publish
baskets for other people's money — D3 posture B is ⚠ UNREVIEWED and RA/empanelment filings sit
with counsel.

**Needed:** counsel's answer on which registration (if any) a third-party manager must hold before
their baskets may be listed to other users, and whether Baskfy carries any obligation to verify it
rather than merely record it.
**Blocks:** turning `state = APPROVED` into a defensible decision, and any onboarding that is open
rather than operator-reviewed.
**Meanwhile:** every response carries "Registration details are captured as supplied and are not
verified by Baskfy unless a verification date is shown. A registration is not a statement by
Baskfy that this manager may manage your money." Approval is staff-only and manual.

### 3. Who does the verifying, and how it is evidenced

`sebi_reg_verified_at` is written by a human action, never by the format checker. There is no
process behind it yet.

**Needed:** who checks a number against the SEBI register, how often re-checking happens (a
registration can lapse after approval), and what evidence is retained.
**Blocks:** the verification stamp meaning anything.
**Meanwhile:** NULL, and the disclaimer says unverified.

### 4. The manager agreement itself

**Needed:** the contract a third-party manager signs — liability, termination, what happens to
investors already holding a basket when a manager is suspended or leaves.

**Blocks:** onboarding anyone who is not Maulik. This is the one that makes the other three real:
suspension currently takes a basket out of the listing, and nothing says what an investor already
invested in it is owed or told.
