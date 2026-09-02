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
| **15** | ✅ **Fundamentals — cleared.** The diagnosis in this row was wrong: NSE had *retired* `/api/quote-equity` (403 from Akamai), so no night would ever have filled the table. Provider repointed at `GetQuoteApi`; `fundamental_daily` filled to 82.2% of the published date and the factsheet serves real M-cap and P/E. Finish the last 18% with `make fundamentals DATE=2026-08-18`. See §15. |
| **17** | **The NSE fetch stalls every ~600 requests** — and step 6 is now long enough to hit it, so this can hang a night. Worked around for backfills; the real fix is named. See §17. |
| **19** | ⚠ **Four legal pages are live and show `[SUPPLIER LEGAL NAME]` to visitors.** Needs seven facts from you, then counsel. `docs/COUNSEL-BRIEF.md` is ready to forward. See §19. |
| **18** | **The landing page's sample screen is 402-blocked** by the `custom_columns` entitlement, not by data. Unblocking it moves a paywall — your call (D7-adjacent). See §18. |
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


### 15. Fundamentals live fill — **the diagnosis was wrong; fixed and filled (Tree 3, 25 Aug 2026)**
**Status:** ✅ **cleared** — nothing of yours is needed · **Raised:** UI route audit, 23 Aug 2026
· **Closed:** Tree 3 Fundamentals, 25 Aug 2026

*(Note: two sections in this file are numbered 15. This is the fundamentals one; the
mark-as-invested §15 further down is Tree 8's and was already closed.)*

**What this entry used to say:** the parser and the join were written, the table was empty, and
all it needed was "a night (or a one-off `equity_fundamentals` fetch) against NSE".

**That was wrong, and a night would not have fixed it.** NSE **retired** `/api/quote-equity` in
its Next.js migration. The route now answers **403 from AkamaiGHost**, which reads exactly like a
bot block and is really a removed endpoint — so a night against NSE would have archived ~2,500
copies of an Akamai deny page and reported success. The live quote page calls
`/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData` instead, found by reading the
page's own JavaScript chunks, and that answers 200 with no cookies and no priming. The payload
shape changed too (`equityResponse[0].tradeInfo.issuedSize`, `secInfo.pdSymbolPe`), so the parser
was reading fields that no longer exist.

**A second trap, worth knowing about for any future backfill:** the date the API serves is
`max(pipeline_run.trade_date)` where `data_version IS NOT NULL`, **not** `max(ohlcv_daily.date)`.
Those were 2026-08-18 and 2026-08-21. Filling only the newer one leaves every surface on an em
dash while `fundamental_daily` looks full. Both dates are filled, and the CLI now defaults to the
published date and says which it picked.

**Where it stands.** Measured, not recalled (`bash tools/tree3/report-numbers.sh`):

| | 2026-08-18 (**published — what the API serves**) | 2026-08-21 (newest bars) |
|---|---|---|
| `fundamental_daily` rows | **2,089 of 2,540 — 82.2%** | 605 of 2,545 — 23.8% |
| with `marketcap_cr` | 2,089 | 605 |
| with `pe` | 1,570 | 451 |

**It is not 100%, and the reason is not NSE.** The fill was stopped part-way; the remaining names
were never fetched. Re-running `make fundamentals DATE=2026-08-18` finishes them — `--resume`
skips the 2,089 already stored and the archive never refetches a key it holds, so it costs only
what is genuinely missing. Twenty-four names are genuine misses: NSE answers with an entry whose
sections are all `null`, and no row is invented for them.

**Proof the chain works end to end**, not just the table:

* `factors_cli recompute --date 2026-08-18` took `factor_daily` from 271 market caps / 0 P/E to
  846 / 637, every non-null matching its `fundamental_daily` source exactly. (It has not been
  re-run since the fill passed 2,089, so `make refactors DATE=2026-08-18` is worth one more pass.)
* `GET /api/v1/instruments/BHARTIARTL` at `as_of=2026-08-18` now returns
  `marketcap_cr = 1207038` and `pe = 32.942`. It returned `null`/`null` before.
* Decile bucketing is no longer one giant tie: D1's smallest cap (₹39,471 cr) exceeds the largest
  cap outside D1 (₹39,366 cr), across 760 distinct values.

**What is still true and is not going to change:** `pb` and `div_yield` are not in NSE's current
payload at all, so they stay NULL — permanently, not pending a fetch. An em dash on a single
name's P/E now means NSE publishes no ratio for it (a company without earnings, most BZ-series
names), not that nothing was ever fetched.

**One thing this did *not* fix, filed as §17:** the NSE fetch stalls roughly every 600 requests
and the nightly path can now hit it. See §17 for the symptom, why the timeouts miss it, and the
fix worth making.

**Commands:** `make fundamentals DATE=2026-08-18` then `make refactors DATE=2026-08-18`, or
`bash tools/tree3/drive-fill.sh 2026-08-18` to supervise it. `RUN-AND-TEST.md` §2 has the detail;
`docs/DECISIONS-MERGE.md` §T3F.1–T3F.6 has the reasoning.

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

### 19. **Four legal pages are LIVE and showing `[SUPPLIER LEGAL NAME]` to visitors**
**Status:** open, **the most urgent thing in this file** · **Raised:** UI prune, 25 Aug 2026

**What is needed:** seven facts, then a lawyer. The facts are yours; the review is counsel's.
`docs/COUNSEL-BRIEF.md` is a single document you can forward to a lawyer as-is — it consolidates
the eight questions on the drafts, the C1–C3 regulatory questions from §13, and D7/D10, so nobody
has to assemble them from three files.

**The finding.** `/terms-conditions`, `/privacy-policy`, `/disclaimer` and `/refund-policy` all
return **HTTP 200 to anyone** and render literal bracketed placeholders. Measured 25 Aug 2026:

| Page | What a visitor sees today |
|---|---|
| `/terms-conditions` | `[SUPPLIER LEGAL NAME]` `[SUPPLIER ENTITY TYPE]` `[SUPPLIER ADDRESS]` `[SUPPLIER GSTIN]` `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` `[CITY]` |
| `/privacy-policy` | `[SUPPLIER LEGAL NAME]` `[SUPPLIER ADDRESS]` `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` `[HOSTING PROVIDER]` `[EMAIL PROVIDER]` |
| `/disclaimer` | `[SUPPLIER LEGAL NAME]` |
| `/refund-policy` | `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` |

This is not a rendering bug — the values are genuinely unknown to the repo. The in-repo DRAFT
marker is deliberately invisible on the rendered page (Prompt 18's instruction, on the reasoning
that a "DRAFT" watermark invites a customer to argue nothing was agreed), and a guard test
enforces that. The consequence nobody weighed is what you see above.

**The seven facts (nothing can be filled in without them):**

- [ ] Supplier legal name, and entity type (proprietorship / LLP / private limited)
- [ ] Registered address
- [ ] GSTIN
- [ ] Grievance officer name + email — **required by the DPDP Act**
- [ ] Governing-law city / venue
- [ ] Hosting provider and email provider, named for the privacy policy
- [ ] Confirm prices are GST-inclusive (they are, in this build), or decide to change it

**What was done meanwhile:** `docs/COUNSEL-BRIEF.md` written; the guard test re-run
(`legal-drafts.test.ts`, 22 passed) confirming the drafts still cannot pass as reviewed; this
entry filed. **An agent cannot engage a lawyer** — that step is yours and nothing here substitutes
for it.

---

### 18. **The landing page's sample screen is 402-blocked — a pricing call, not a bug fix**
**Status:** open, **needs your decision** (D7-adjacent) · **Raised:** Tree 3 Fundamentals,
25 Aug 2026

**What is needed:** a decision on whether the marketing page's teaser table may show gated
columns to a visitor with no account. Two lines of code either way; neither is mine to pick.

**What happens now.** `/` renders *"The sample screen could not be loaded — the data service did
not answer when this page was built."* That is its honest empty state, and the cause is not the
data service. `fetchSampleScreen` posts to `/api/v1/screens/preview` asking for
`close_raw, marketcap_cr, ret_12m, sharpe_12m, vol_12m`. `ANONYMOUS` grants only
`Feature.SCREENER`, and `DEFAULT_RESULT_COLUMNS` is `(ret_12m, vol_12m, close_raw)` — so three of
those five are "custom columns" and the call returns **402 payment-required**.

**Why it lands here rather than being fixed.** The page's own copy says the sample is there to be
read "without an account", so the intent is clear enough — but every way of honouring it changes
product behaviour:

* grant `custom_columns` to `ANONYMOUS`, or exempt the preview route → **moves a paywall**;
* drop the gated columns from the teaser → **removes the M-cap column** that §15 asked for, and
  Sharpe with it;
* give the marketing fetch a service principal → a new trust boundary for a public page.

That is a pricing and positioning question (D7), and CLAUDE.md says never to build against a guess
on those.

**What it does *not* block.** Fundamentals themselves are filled and rendering: the instrument
factsheet serves real `marketcap_cr` and `pe`, and the screener's own columns carry them for the
served date. This is the last surface §15 named that still shows nothing, and it would show
nothing today even with a perfect fundamentals table.

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

**Update — 26 Aug 2026, portfolio-redesign leaf C2 (broker holdings sync).** The worker now has a
holdings sync (`baskfy_worker.tasks.holdings_sync`) behind a provider port, with a Kite adapter
(`KiteProvider.broker_holdings` / `.broker_cash`) and a fixture adapter. **No live Kite fetch has
ever been executed** — the whole chain is proven against the fixture provider, with the test
suite's network block armed. Two hands-only things stand between that and a real sync, both
Zerodha-side and neither blocking any further engineering:

1. **A valid daily Kite access token.** Kite invalidates it at the start of each trading day, and
   regenerating it is a login + 2FA that only Maulik can perform. Without one the holdings
   capability is simply unavailable, and — deliberately — the stack raises rather than falling
   back to invented positions (`CapabilityNotAvailable`, never a fixture).
2. **Which `broker_account.id` that token belongs to.** P4.2 (per-user encrypted OAuth tokens) is
   not built, so the adapter holds exactly one token. `KiteRuntime.holdings_account_id` exists to
   record the answer and refuses a read for any other account; left unset on the single-tenant
   founder box, it serves whichever account is asked for. It must be set before a second tenant
   exists.

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

---

## 20. ✅ RESOLVED 2 Sep 2026 — the Kite token encryption keys

**Resolved in two parts, because there were two stores and only one was in this heading.**

**Baskfy's store, re-keyed.** Not what §20 originally described, and the more urgent of the two: it
held a *live* token. Re-encrypted under a freshly generated Fernet key that has never left the box,
verified before and after — `api` and `worker` both decrypt it and Kite answers 200 for YP8452, and
the previous key can no longer decrypt the store. The old key and ciphertext are backed up on the
box as `.env.staging.pre-fernet-*` and `kite-token.enc.pre-fernet-*`.

**The desk's store, deleted.** This is the key the heading below was about. Maulik confirmed the
desk has been dead since M70; its token was issued 22 Aug and Kite ends a session overnight, so it
was ten days stale. Key and ciphertext were copied to `~/baskfy-retired-credentials/` outside the
repo and removed from the working tree, and the `.key` is untracked. `portfolio.db` — the
unrebuildable evidence — was checked and is intact at 6,029,312 bytes.

**Also rotated:** the Kite API secret, by Maulik, in the Kite console. Worth recording that this
did **not** invalidate the live access token, contrary to what was predicted here: Kite validates an
issued token independently of the secret, which only signs the checksum that mints one. The new
secret takes effect at the next Connect.

**What remains, and it is a decision rather than a task.** The dead key is still in commit
`44c029c` on `origin/developer`. Removing it needs `git filter-repo` and a force-push, which
rewrites every SHA on the branch and obliges anyone with a clone to re-clone. It is now worth
little: the key decrypts a file that no longer exists.

`tools/deploy/verify-safety.sh` no longer carries a by-name exemption for the path — it passes on
its own terms.

<details><summary>The original entry, for the record</summary>

### ⚠️ URGENT — the Kite token's encryption key is committed and pushed

**This one is not about the deployment; the deployment's safety check found it.**

`kite-momentum-rebalancer/data/.kite_token.json.key` is a **44-byte base64 string — a Fernet
key** — and it is tracked by git. It entered in commit `44c029c` ("M16: green — the order path is
its own package, and the token is encrypted", 22 Aug 2026) and `git branch -r --contains 44c029c`
puts it in **`origin/developer`**, i.e. on GitHub at `git@github.com:maulikam/baskfy.git`.

The cause is a one-line gap, now closed: `kite-momentum-rebalancer/.gitignore` ignored
`data/.kite_token.json` — the ciphertext — and said nothing about `data/.kite_token.json.key`, the
key that decrypts it. Ignoring the ciphertext while committing its key protects nothing.

**What is and is not exposed.** The encrypted token itself was never tracked (`git ls-files`
confirms only the `.key` is in the index), so the repository alone does not yield a usable Kite
session. What is exposed is the key, permanently, to anyone who has ever had read access to that
repository or a clone of it. The token that key decrypts grants full account read **and order
placement** until it expires.

**Needed from you, in this order:**

1. **Rotate the Fernet key and re-encrypt the token** — assume the committed key is public. This
   is the fix; everything below is cleanup. Deleting the file from GitHub does not un-disclose it.
2. **Rotate the Kite API secret** at the Kite developer console, and re-authenticate, if you want
   to be certain no derived session survives.
3. Decide about history. `git rm --cached` untracks it going forward; only a history rewrite
   (`git filter-repo`) removes it from the 22 Aug commit, and that rewrites every SHA on
   `developer` and needs a force-push. **Not done autonomously** — it is destructive, it touches a
   branch with a remote, and it is worth nothing without step 1.

**Blocks:** nothing technically — but it is the largest single security item open in the repo, and
it is worth more than any feature currently in flight.

**Done meanwhile:** the gitignore gap is closed (`data/.kite_token.json.*` and `data/*.key`), and
`tools/deploy/verify-safety.sh` now fails the build if a secret-shaped file is ever tracked again.
The file is left in the index untouched, because untracking it without rotating the key would look
like a fix while changing nothing.

</details>

---

## 21. AWS account access for the Phase A box — ✅ RESOLVED 26 Aug 2026, except DNS

**Applied.** Account `056235107739` ("Proof of Concept"), `ap-south-1`. 29 resources. The stack is
running on `i-086986250704e4392` / EIP `3.108.148.38`. See `docs/DECISIONS-MERGE.md` AWS3.

**The one thing still outstanding is yours:** point `baskfy.com`'s nameservers at the Route 53 zone
(below). Until then `staging.baskfy.com` is NXDOMAIN, Caddy cannot pass the ACME challenge, and
there is no certificate — so the container is deliberately stopped rather than burning Let's
Encrypt's five-failures-per-hour limit.

```
ns-1364.awsdns-42.org
ns-1695.awsdns-19.co.uk
ns-485.awsdns-60.com
ns-909.awsdns-49.net
```

The gate password is in `ops/baskfy-staging-gate-password.txt` (gitignored). Put it in a password
manager; nothing else has a copy.

### Original entry, kept for the record

Everything in `docs/08` §3 is now built and verified locally — images, compose, Caddy gate,
Terraform — and none of it can be applied, because there is no AWS on this machine: `which aws`
finds nothing and `~/.aws` does not exist.

**Update, 26 Aug 2026:** Maulik logged into the AWS **console**. That is not CLI access — this
machine still has no credentials. The AWS CLI (2.36.31) is now installed, and
`bash tools/deploy/preflight-aws.sh` reports exactly what is missing; it currently stops at
"no working credentials".

**Needed:**

1. **CLI credentials for the `ap-south-1` account.** `aws configure sso` is the recommendation —
   short-lived, nothing written to disk that is worth stealing. `aws configure` with an access key
   also works and is what most people do; it puts a long-lived secret in `~/.aws/credentials`,
   which in a single-owner account is the only real risk in this whole step. The permission set is
   `decile-blueprint/infra/terraform/deploy-policy.json`, derived from the 29 resources the
   configuration actually declares; `AdministratorAccess` via Identity Center is the simpler and,
   on balance, safer choice for the first apply.
2. **`baskfy.com`'s nameservers pointed at the Route 53 hosted zone** Terraform creates, so
   `staging.baskfy.com` resolves and Caddy can complete an ACME challenge. Registrar-side, so only
   you can do it.
3. **A budget email address** for the billing alarm the Terraform sets at $75/mo — §7's line is
   "nothing above $75/month exists until the SEBI gate is passed", so the alarm is set to notice
   the moment that stops being true.
4. **The gate password** — run `bash tools/deploy/gate-password.sh`, keep the plaintext in a
   password manager, put the hash in `.env.staging.compose` on the box. It is printed once and
   stored nowhere.

**Blocks:** `terraform apply`, and therefore the live URL. Nothing else.

**Done meanwhile:** the whole stack runs and is verified end to end on Docker locally — eight
services, migrations at `0024`, the gate challenging strangers, server rendering reaching the API
across the container network. `bash tools/deploy/verify-stack.sh` reproduces it. When the
credentials exist the remaining work is `terraform apply` and one `docker compose up`, both
scripted in `decile-blueprint/docs/runbooks/07-deploy-phase-a.md`.

**Not blocking this, but blocking going public:** the four legal drafts are still unreviewed
(§19), which is why the first deploy is gated and `noindex` rather than public. Flipping it public
is runbook §7 and deliberately takes more than one edit.

---

## 22. A web session cannot be revoked — ✅ BUILT 27 Aug 2026

**Found 26 Aug 2026, chasing your report that Back still shows the app after signing out.**

That report is fixed (`docs/DECISIONS-MERGE.md` §SEC1): the sign-out response now sends
`Clear-Site-Data`, which drops the origin's cookies, storage and cached documents — the
back/forward cache among them — so Back has nothing left to restore. Shipped, tested, no
migration.

Chasing it surfaced something larger, and it is not fixed because the fix is yours to sequence.

**The gap.** The web session is an Auth.js JWT cookie with `maxAge` of 30 days, and `jwt` strategy
is forced — Auth.js v5 cannot use database sessions with the Credentials provider (`docs/08a` §3).
That cookie is a self-contained 30-day credential. Sign-out deletes the browser's copy; it does
not invalidate the credential. The `jwt` callback re-mints the 15-minute API access token locally
from `BASKFY_JWT_SECRET` without ever calling the API, and `current_principal` checks signature,
claims and lifetime and then only that `sub` names a row in `app_user` — it never reads the
refresh-session table.

**So `revoke_all_for_user` does not do what its docstring says.** `set_password` calls it, and its
docstring reads "A password change that leaves old sessions alive does not evict whoever prompted
it." That is true of the API's own refresh sessions and false of the session real users hold.
**Changing your Baskfy password does not sign an attacker out of the web app** — they keep it for
the remainder of the 30 days. Password reset and account deletion have the same gap. API keys, by
contrast, have `revoked_at` and a test asserting a revoked key dies within a second; user sessions
have no equivalent.

**The fix, and why it is not already done.** A session epoch: an integer column on `app_user`,
bumped by `revoke_all_for_user`, returned on `MeOut`, stamped into the Auth.js JWT at sign-in, and
compared in `(app)/layout.tsx` — which already fetches `/me` on every gated render, so it costs no
new round trip. Sign-out bumps it too, which turns the 30-day cookie into a dead one. That is an
Alembic migration, a regenerated `@baskfy/api-client`, and a change to the auth contract, applied
to a box that is already serving. Landing it beside a header fix without your say-so is the kind
of change that should not arrive as a surprise.

**Built 27 Aug 2026** on your go-ahead, exactly as recommended above:
`app_user.session_epoch` (migration `0025_session_epoch`), bumped by `revoke_all_for_user`, so a
password change, a password reset and an account deletion all now end web sessions as well as API
ones. Returned on `MeOut` and `SessionOut`, frozen into the Auth.js token at sign-in, compared in
`(app)/layout.tsx` where `GET /me` is already awaited — no extra round trip. Full reasoning, the
four edge cases and what was rejected are in `docs/DECISIONS-MERGE.md` §SEC2.

**Two things deliberately NOT changed, which are still yours to call:**

1. **Ordinary sign-out does not bump the epoch** — signing out on a laptop should not end the
   session on a phone. The epoch now makes an explicit **"sign out everywhere"** button possible
   for the first time; say the word and it is a small addition to the security page.
2. **`maxAge` stays at 30 days.** How often people re-authenticate is a product decision, and the
   epoch is what makes 30 days defensible rather than alarming.

**Still needs your hands — one deploy step.** `alembic upgrade head` against the staging database
before the new web image goes up. The migration is additive, `NOT NULL DEFAULT 0`, backfills every
existing account to the generation their current sessions already carry, and is invisible to
anybody signed in when it runs — nobody gets logged out by the deploy. Down-migration round-trips
cleanly if you need to back it out.

**Blocks:** nothing. The sentence "signing out of Baskfy ends the session" is now true of the
browser, and "changing your password evicts whoever prompted it" is now true of the server.

**Done meanwhile:** the browser half is closed and verified — `Clear-Site-Data` plus `no-store`
plus explicit cookie deletion on `/logout`, pinned by
`decile-blueprint/apps/web/src/app/logout/__tests__/route.test.ts`. The server-side gate was
audited over the public internet and is sound: every gated path redirects without a session, a
forged cookie is refused by the layout rather than the middleware, `?next=` cannot be pointed
off-origin, every `/admin/*` route is 401 anonymous and 404 to a non-staff session, and none of
the 142 paths the live API publishes can place an order. The full list of what was checked and
found good is in `docs/DECISIONS-MERGE.md` §SEC1, so nobody re-audits it.

---

## §24 — Google OAuth client credentials (M46)

**What is needed.** `BASKFY_GOOGLE_CLIENT_ID` and `BASKFY_GOOGLE_CLIENT_SECRET` from the Google
Cloud console, for the `baskfy` project's Web application OAuth client.

- **Authorized redirect URIs:** `https://staging.baskfy.com/api/auth/callback/google` and
  `http://localhost:3000/api/auth/callback/google`. Verified against the code: there is no
  `basePath` override in the Auth.js config and the box sets `NEXTAUTH_URL=https://staging.baskfy.com`.
- **Scopes:** `openid`, `email`, `profile` — all non-sensitive, so publishing to Production needs
  no Google verification review.
- **Both containers need it.** The web container runs the OAuth dance and needs both values; the
  API needs the **client id** only, because it checks that `aud` on the incoming ID token is ours.
  That check is what stops a token minted for somebody else's Google app from signing that person
  into Baskfy.

**Do not set the secret through `tools/deploy/box.sh`.** Its own header says why: `ssm
send-command` parameters are retained in command history and visible in CloudTrail. Use
`aws ssm start-session --target i-086986250704e4392 --profile baskfy-poc` and edit
`/opt/baskfy/.env.staging` there, the way `ses-credentials.sh` writes the SMTP password.

**Blocks:** **all sign-in.** M46 removed the password and the OTP, so with no client id there is
no way into the product. `Settings.require_configured` refuses an empty client id in production
outright; in staging the API boots and `POST /auth/google` answers 401.

**Done meanwhile:** everything else. The endpoint, the verifier, `auth_identity` + migration 0026,
the Auth.js Google provider, the rewritten `/login`, and the removal of the whole email/password
surface are built and tested (45 API auth tests, 1928 web tests). The moment the two values are on
the box, sign-in works without another code change.

---

## §25 — Two things the legal pages now promise that nothing enforces (M46)

**Supplied 27 Aug 2026 and filled in:** RENIL, sole proprietorship, 703/2 Sector 4C, Gandhinagar,
Gujarat 382006, GSTIN `24ANFPD9399F1ZS`, grievance officer Maulik / `grievance@baskfy.com`,
jurisdiction Gandhinagar. All ten placeholders across the four documents are gone.

**What is needed, item 1: an inbox for `grievance@baskfy.com`.** It receives nothing today. SES on
this deployment is send-only and the domain has no MX for it. That address is now printed in
`terms-conditions.mdx` §15 and `privacy-policy.mdx` §10 as the statutory grievance contact under
the Consumer Protection (E-Commerce) Rules and the DPDP Act. **A grievance address that silently
bounces is worse than not naming one.** Needs Google Workspace on the domain, or SES inbound
receiving.

**What is needed, item 2: a full legal name for the grievance officer.** "Maulik" alone is
recorded. The E-Commerce Rules contemplate a named individual, which in practice means a full
name. One-line fix in three places once supplied.

**What is needed, item 3: enforce the 90-day log retention we now state.** `privacy-policy.mdx` §5
says server logs are kept 90 days. The box rotates container logs by **size** — Docker `json-file`
at `max-size=50m, max-file=5`, 250 MB per service — not by age, which at current traffic is very
likely *more* than ninety days. The policy currently promises a shorter retention than the
infrastructure delivers. Needs a cron or a log shipper with a time-based lifecycle.

**Blocks:** nothing technical. Items 1 and 2 should be closed before the legal pages are relied on
by a regulator or a payment provider; item 3 before anyone audits the claim.

**Done meanwhile:** all four documents are internally consistent and the privacy policy was
corrected for M46 — §1, §2, §3, §4, §6 and §7 described passwords, sign-in codes and a lockout
that no longer exist, and §4 omitted **Google**, which now receives an authentication request for
every sign-in. `DRAFT-NOTICE.md` items 1 and 8 are closed, item 7 amended. The remaining six
decisions there still need a lawyer.

---

## §26 — The Kite Publisher API key (M47)

**What is needed.** `BASKFY_KITE_PUBLISHER_API_KEY` — the api key of the **Publisher** app
("Baskfy", created 27 Aug 2026), from the Kite developer console.

**It is not a secret.** A Publisher key travels inside the form the user's browser posts to
`kite.zerodha.com/connect/basket`; Zerodha's own embed snippets put it in page source. So it can
go through `box.sh`, unlike the Google client secret. Paste it here and it is a one-line env
change plus a restart.

**Where it goes:** `/opt/baskfy/.env.staging` (the API reads it; the web app never sees it — the
key reaches the browser inside the `GET /baskets/plan/kite` response, not through the bundle).

**Blocks:** the "Review N orders in Kite" button. Empty means the hand-off is **off** and the
button is not rendered — deliberately, because a form posted with no `api_key` lands the user on
an error page inside Kite, which reads as Baskfy being broken rather than as Baskfy being
unconfigured.

**Two things about that app that are worth checking before this goes live:**

1. **The redirect URL currently reads `https://staging.baskfy.com/callback`, which is not a route
   this app serves.** For Publisher it does not matter — Zerodha's console says so, and the basket
   hand-off never uses it. Leave it or blank it; do not point it at
   `/api/v1/brokers/callback`, which belongs to the Kite **Connect** flow and would be misleading.
2. **Publisher is not Kite Connect.** `/brokers` — connect Zerodha, sync holdings — needs a Kite
   Connect app (`BASKFY_KITE_API_KEY` + `BASKFY_KITE_API_SECRET`), which is the paid product. The
   desk already holds Connect credentials in `kite-momentum-rebalancer/.env`, but that app is
   wired to one account for single-user live execution; pointing multi-tenant web OAuth at it is a
   separate decision, and `CLAUDE.md` still lists P4.2 two-token OAuth as not done.

**Done meanwhile:** the whole hand-off is built and tested — `GET /baskets/plan/kite`,
`baskfy_api.kite_basket`, the `KiteBasketForm` component, 21 API tests and 9 web tests. It is not
yet placed on a page; see §27.

---

## §27 — Where the "Review in Kite" button should live (M47)

**A product decision, not an engineering one.** The component is built and tested but not mounted
on any page. `PlanHandoffPanel` (`components/cb/plan-handoff-panel.tsx`) is the obvious home — it
is the existing hand-off surface — but today it points at `https://desk.modelbasket.in`, the
**operator console**, which is Maulik's own and useless to a signed-in user. Replacing that button
with the Kite one changes what that panel is for: from "the desk will execute this" to "you
execute this, in your account".

That is the right change under D3 posture B, and it is not one to make silently while a page still
says "Execution stays in the desk console". Confirm the wording and the placement and it is a
small edit.

**Blocks:** nothing. The API route serves today; only the button is unplaced.

---

## §28 — Zerodha connect needs a Kite **Connect** app, and the Publisher key is still missing (M49)

**The message** "The Zerodha app key is not configured on this deployment" comes from
`POST /brokers/{id}/connect` (`routers/brokers.py`). It is the app refusing to start a login it
cannot finish, which is correct behaviour, not a bug.

**Both Kite variables are empty on the box**, confirmed 27 Aug 2026:

| Variable | State | What it is for |
|---|---|---|
| `BASKFY_KITE_API_KEY` | **empty** | Kite **Connect** — `/brokers` connect + holdings sync |
| `BASKFY_KITE_API_SECRET` | **empty** | Kite Connect — redeems the `request_token` |
| `BASKFY_KITE_PUBLISHER_API_KEY` | **unset** | Kite **Publisher** — the basket hand-off (§26) |

**These are two different Zerodha products and one does not substitute for the other.** The
Publisher app created on 27 Aug 2026 is the basket widget; it issues no api_secret and cannot
redeem a `request_token`, so it can never satisfy `/brokers`. Kite Connect is the paid product
(₹2000/month per app).

**Three options, and they are genuinely different decisions:**

1. **Create a Kite Connect app for Baskfy** and set both variables. The proper answer for a
   multi-tenant product.
2. **Reuse the desk's Connect credentials** — they exist (`kite-momentum-rebalancer/.env`,
   `KITE_API_KEY` 16 chars, `KITE_API_SECRET` 32). But that app is wired to one account for
   single-user live execution, and `CLAUDE.md` still lists **P4.2 two-token OAuth as not done**.
   Pointing multi-tenant web OAuth at the desk's app is a decision to take deliberately.
3. **Do neither for now.** `/brokers` keeps saying it is not configured — which is honest — and
   the Publisher basket hand-off (§26) covers "let the user trade this", needing only the free
   Publisher key. Given the hand-off is built and the Connect flow is not needed for it, this is
   the cheapest coherent state.

**Fixed while looking at this (M49):** the OAuth `redirect_uri` default was
`https://baskfy.com/brokers/callback`, wrong twice over — the apex has **no DNS record** (only
`staging.baskfy.com` resolves), and `/brokers/callback` is not a route at all; the callback this
service serves is `/api/v1/brokers/callback`. Neither would have surfaced until Kite sent the
browser back, *after* the user had signed in at Zerodha and authorised the app. It is now derived
from `web_origin` plus the path constant the callback route is registered under, so the two cannot
drift, and a test asserts it.

**Whichever option you pick, the redirect URI registered in the Kite console must be exactly:**
`https://staging.baskfy.com/api/v1/brokers/callback`

---

## §29 — Credential inventory and rotation list for the desk→Baskfy cutover (leaf 1.1.3, 31 Aug 2026)

**No value of any credential appears in this section.** Every "status" below was measured with a
presence/length check, never a read. Lengths are given because they are what lets you confirm a
rotation actually landed (a rotated secret has a new length or, at minimum, a changed `docker
compose exec` length read) without anyone ever printing the value.

### 1. The inventory

Three hosts hold credentials, and it matters which is which:

- **desk** — the momentum desk that places live orders, reachable as `desk.modelbasket.in`, SSH
  user `desk`. Its config is `kite-momentum-rebalancer/app/config.py` reading
  `kite-momentum-rebalancer/.env` (gitignored at `kite-momentum-rebalancer/.gitignore:4`,
  confirmed untracked).
- **box** — the Baskfy Phase-A EC2 box (`ap-south-1`), reached only over SSM
  (`tools/deploy/box.sh`). Service env comes from `/opt/baskfy/.env.staging` via
  `env_file: [.env.staging]` (`decile-blueprint/infra/docker/compose.prod.yml:30`); compose
  *interpolation* values come from `/opt/baskfy/.env.staging.compose`. Both are 0600.
- **laptop** — this machine, `decile-blueprint/.env` and root `.env.staging`
  (both gitignored at `.gitignore:18` / `decile-blueprint/.gitignore:18`, confirmed untracked
  with `git ls-files --error-unmatch`).

#### Kite Connect — the RENIL app (one app, one redirect URL, ₹2,000/month)

| Variable | Which system needs it | Where it lives | Status (measured 31 Aug 2026) |
|---|---|---|---|
| `KITE_API_KEY` | desk (`app/config.py:9`) | `kite-momentum-rebalancer/.env` on desk **and** laptop | set, 16 chars (laptop read) |
| `KITE_API_SECRET` | desk (`app/config.py:10`) — redeems the `request_token` at `GET /callback` (`app/main.py:233`) | same file | set, 32 chars (laptop read) |
| `TOKEN_FILE` → `data/.kite_token.json` | desk (`app/config.py:40`) | desk `data/`, **plaintext JSON**, 248 bytes | present; a Kite access token dies ~06:00 IST daily |
| `BASKFY_KITE_API_KEY` | box worker + api (`decile-blueprint/packages/providers/src/baskfy_providers/settings.py`, `kite_api_key`) | `/opt/baskfy/.env.staging`; laptop `decile-blueprint/.env` | **SET, 16 chars** in `worker` and `api` containers; `UNSET` in `web`. Same length as the desk's key — it is the same RENIL app |
| `BASKFY_KITE_API_SECRET` | box worker + api (`kite_api_secret`) | as above | **SET BUT EMPTY** in `worker` and `api`. Laptop holds a 32-char value. See §29.3 |
| `BASKFY_KITE_TOKEN_ENCRYPTION_KEY` | box worker + api (`kite_token_encryption_key`) — Fernet key encrypting the daily access token at rest | as above | **SET, 44 chars** in `worker` and `api` (44 = a Fernet key); laptop 44 |
| `BASKFY_KITE_TOKEN_PATH` | box (`compose.prod.yml:61` → `/var/lib/baskfy/state/kite-token.enc`) | named volume `baskfy-state` | blob present, 248 bytes, `-rw-------  baskfy baskfy`, mtime **31 Aug 16:17** |
| `BASKFY_KITE_PUBLISHER_API_KEY` | box api (`services/api/.../settings.py`, `kite_publisher_api_key`) — the basket hand-off | nowhere | `UNSET`. **Not a secret** — it travels in the browser form post; see §28 |

`kite_configured()` requires *both* key and secret, so with the secret empty the box currently
reports Kite as unconfigured — it works only through the M58 bridge below.

#### The M58 desk-session bridge (how the box gets a token today)

| Variable | Which system | Where it lives | Status |
|---|---|---|---|
| `BASKFY_KITE_DESK_SSH_TARGET` | box worker + api | `/opt/baskfy/.env.staging` | **SET, 16 chars** in `worker` and `api`; `UNSET` in `web` (correct) |
| `BASKFY_KITE_DESK_SSH_KEY_PATH` | box worker | not set — pydantic default `/var/lib/baskfy/.ssh/kite-session` applies | `UNSET` in every container, **by design**. The key file exists on the box at `/opt/baskfy/secrets/ssh/kite-session`, 452 bytes, `-rw-------` uid 1001, bind-mounted read-only at `/var/lib/baskfy/.ssh` (`compose.prod.yml:48`) |
| `BASKFY_KITE_DESK_KNOWN_HOSTS_PATH` | box worker | default `/var/lib/baskfy/.ssh/known_hosts` | `UNSET`; file present, 232 bytes, same directory. The desk's host key is **pinned**, not TOFU |
| the desk-side `authorized_keys` line | desk | desk `~/.ssh/authorized_keys` (a backup is written beside it) | one line, `restrict,command="/home/desk/bin/emit-kite-token",from="<box ip>"` — a capability, not an account (`tools/deploy/install-kite-session.sh:8`, `docs/DECISIONS-MERGE.md` M58) |

The private half was generated **on the box** and has never moved; `box.sh` refuses secrets as
arguments because `ssm send-command` parameters are retained in CloudTrail.

#### Google sign-in (M46 — the only way in)

| Variable | Which system | Where it lives | Status |
|---|---|---|---|
| `BASKFY_GOOGLE_CLIENT_ID` | box `web` (runs the OAuth exchange) **and** box `api` (uses it as the only `aud` it accepts on a Google ID token) | `/opt/baskfy/.env.staging.compose` **and** `/opt/baskfy/.env.staging` | **SET, 72 chars** in `worker`, `api` and `web`; 72 chars in `.env.staging.compose`. Not a secret |
| `BASKFY_GOOGLE_CLIENT_SECRET` | box `web` **only** — the API verifies ID-token signatures against Google's public JWKS and needs no secret (`services/api/.../settings.py`, `google_client_secret` docstring) | `/opt/baskfy/.env.staging.compose`; laptop `decile-blueprint/.env` | **SET, 35 chars in `web`**; `UNSET` in `worker` and `api` (correct). 35 chars in `.env.staging.compose`; 35 on the laptop |

Google Cloud console → project `baskfy` → Web application OAuth client. Authorised redirect URIs
must include `https://staging.baskfy.com/api/auth/callback/google` and
`http://localhost:3000/api/auth/callback/google` (root `.env.example:378-387`).

#### Session / adjacent secrets the cutover touches

| Variable | Which system | Where | Status |
|---|---|---|---|
| `AUTH_SECRET` | box `web` (Auth.js session cookies) | `/opt/baskfy/.env.staging` | **SET, 64 chars** in `web`; empty in `worker`/`api`. Not on the exposure list |
| `BASKFY_JWT_SECRET` | box api (verifies) + web (mints) | `/opt/baskfy/.env.staging` | **SET, 64 chars** in all three containers. Not on the exposure list |
| `DESK_PASSWORD` | desk — gates the operator console; loopback is not a boundary on a shared box | desk `.env` | not measured from here (the desk is not reachable over SSM). Root `.env.example:290` documents it |
| `BASKFY_METRICS_TOKEN` | box api | — | `UNSET` |

### 2. The rotation list — ✅ CLOSED 31 Aug 2026: reviewed and accepted, not rotated

> **Maulik's decision, 31 Aug 2026.** Asked directly, with the per-secret risk laid out, he
> declined to rotate. The api_key is public by design and unregenerable without a second paid
> Connect app; the api_secret is worthless without a live single-use request_token the desk
> redeems within seconds; the encryption key needs the 0600 blob on the box; and the transcripts
> are his own private sessions. Nothing is blocked — Google sign-in works, and
> `BASKFY_KITE_API_SECRET` is deliberately absent from the Baskfy box (M64 depends on that).
> Reversal is recorded in `docs/00-merge-status.md`. **Do not reopen this as an oversight.**
>
> The original analysis is kept below as the record of what was weighed.

### 2 (original analysis). Four secrets exposed in agent transcripts

Two were recorded on the status page (`docs/00-merge-status.md`, "What is NOT done"):
*"Two secrets pasted into an agent transcript on 27 Aug still need rotating: the Google client
secret and the RENIL Kite api_secret. The box does not hold the Kite secret, which limits the
blast radius but does not remove the need."* Two more were added on **31 Aug 2026**, when an agent
echoed a whole settings object and printed the Kite **api_key** and the **token encryption key**
into its transcript.

A fifth, already closed: the status page also records the desk's Kite **access** token printed
into the 30 Aug transcript. That token expired overnight and the box's current blob is dated
**31 Aug 16:17**, so it has been retired by ordinary use. No action.

| # | Secret | What it is | Why it must rotate | What breaks during rotation |
|---|---|---|---|---|
| R1 | `KITE_API_SECRET` / `BASKFY_KITE_API_SECRET` (RENIL) | The half of the Kite Connect app credential that redeems a `request_token` into an access token | Pasted into an agent transcript 27 Aug. With the api_key (R3, also exposed) the pair is a complete Kite Connect app credential | Any `request_token` in flight fails. **The desk's login breaks the moment the old secret stops working** — the desk's `.env` and the box must be updated in the same sitting |
| R2 | `BASKFY_KITE_TOKEN_ENCRYPTION_KEY` | Fernet key encrypting the daily access token at rest on the box | Printed into a transcript 31 Aug. Anyone holding it plus the blob at `/var/lib/baskfy/state/kite-token.enc` can read that day's access token | The existing blob becomes undecryptable. Cost is one day's Kite reads until the next `kite_session_cli pull` re-encrypts. **Nothing else** — the bhavcopy path needs no credential (M58 property 3) |
| R3 | `KITE_API_KEY` / `BASKFY_KITE_API_KEY` (RENIL) | The Kite Connect app's identity | Printed into a transcript 31 Aug. **Honest assessment: this one is public by design** — it travels in the login URL in every user's browser (`kite.zerodha.com/connect/login?api_key=…`) and Zerodha's own embed snippets put it in page source. Its exposure matters only in combination with R1, which R1's rotation closes | **The api_key cannot be regenerated.** It is the app's identity: changing it means deleting the app and creating a new one — a new ₹2,000/month subscription, a fresh redirect-URL registration, and both `.env` files updated. **Recommendation: do not rotate R3.** Rotate R1 and the pair is dead |
| R4 | `BASKFY_GOOGLE_CLIENT_SECRET` | Google OAuth web-client secret, used by the `web` service for the code exchange | Pasted into an agent transcript 27 Aug | Google sign-in stops for the seconds it takes to restart `web`. Existing sessions survive (they are Auth.js cookies signed with `AUTH_SECRET`, untouched). The **client ID does not change**, so nothing in `api` needs touching |

**The order to do them in, and why.**

The ordering constraint is not blast radius — it is *leaf 1.1.1*. That leaf is about to decide
whether `BASKFY_KITE_API_SECRET` gets written to the box at all (§29.3). If it does, it must not
be the known-exposed value.

1. **R1 first, before or together with 1.1.1's deployment.** Regenerate the secret at
   `developers.kite.trade`, then write the *fresh* value into the desk's
   `kite-momentum-rebalancer/.env` and (if 1.1.1 chooses branch A) into `/opt/baskfy/.env.staging`
   in the same sitting. Doing it in this order means the box never receives a value that has been
   in a transcript. **Timing has a real conflict, and it is yours to resolve:**
   - *Rotate tonight (31 Aug):* the box gets a clean secret for the 1 Sep login, but if the desk's
     `.env` is not updated in the same sitting the desk cannot log in on 1 Sep, and the Friday
     4 Sep rebalance rail is at risk. Both files, one sitting, outside market hours.
   - *Rotate on the weekend (5–6 Sep):* one more session on the exposed secret; the box is
     configured with the exposed value now and re-configured after. Safer for the Friday rail,
     and the honest cost is a known-exposed secret living five more days.
2. **R2 next, any time, no coordination.** It touches nothing outside the box. Generate with
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
   write it to `/opt/baskfy/.env.staging`, restart `worker` and `api`, then run
   `kite_session_cli pull` (or `deposit`) to re-encrypt. Verify by reading the length back as
   44 and confirming the blob's mtime moved.
3. **R4 third, independent of everything Kite.** Google Cloud console → the `baskfy` web client →
   add a new secret, deploy it to `/opt/baskfy/.env.staging.compose`, restart `web`, confirm a
   sign-in, then delete the old secret in the console. Two-step so there is no outage window at
   all if you add before you remove.
4. **R3 last, and probably never.** Only act on it if you are creating a new Kite Connect app for
   another reason (§28 option 1). Record the decision either way so it stops being an open item.

**Verifying a rotation without printing anything:** re-run the per-container presence check used
to build this table —
`AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh` with a loop over `printenv "$v" | wc -c`. It
reports `UNSET` / `SET_EMPTY` / `SET len=N` and never the value.

### 3. `BASKFY_KITE_API_SECRET` — required or not?

**Measured state:** `SET_EMPTY` in both the `worker` and `api` containers on the box, 31 Aug 2026.
The laptop's `decile-blueprint/.env` holds a 32-char value; the box does not.

**`docs/DESK-LOGIN-DECISION.md` did not exist when this section was written** (leaf 1.1.1 was
running concurrently). Both branches are recorded, and this section should be re-read against that
file once it lands.

- **Branch A — Baskfy owns the redirect. `BASKFY_KITE_API_SECRET` is REQUIRED.**
  The box redeems the `request_token` itself at `/api/v1/brokers/callback`, so it needs the
  secret; `kite_configured()` returns False without it and the login cannot complete. Consequence:
  the **desk's** `GET /callback` never fires again and the desk needs a reverse bridge to get its
  session from Baskfy.
  **This is the live state as of now.** `gates/desk-retire-1.1.1.md` records that the Kite app's
  redirect has already been changed from `https://desk.modelbasket.in/callback` to
  `https://staging.baskfy.com/api/v1/brokers/callback`. Until the secret is set on the box,
  *neither* system can obtain a session — which is why 1.1.1 is time-critical.
- **Branch B — the desk owns the redirect, Baskfy uses the M58 token bridge.
  `BASKFY_KITE_API_SECRET` is NOT required and should stay empty.**
  The box never redeems anything; it pulls the desk's already-redeemed access token over SSH
  (§29.1, M58). This is what M58 was built for, and leaving the secret off the box is a smaller
  blast radius, not an omission. Requires the redirect to be changed **back** to
  `https://desk.modelbasket.in/callback`.

Either way, if the secret is written to the box it should be the **rotated** value (R1), not the
one currently on the laptop.

### 4. What only Maulik's hands can supply

| | What is needed | Why | What it blocks | What was done meanwhile |
|---|---|---|---|---|
| **29a** | **Regenerate the RENIL Kite api_secret** at `developers.kite.trade`, and update `kite-momentum-rebalancer/.env` on the desk in the same sitting | R1 — exposed in a 27 Aug transcript; with the exposed api_key it is a complete app credential | Nothing today; the desk still works on the exposed secret. It gates writing a *clean* secret to the box in branch A | The exposure is recorded here and on the status page; the box does not hold the secret at all right now, which is the smaller blast radius |
| **29b** | **Decide the R1 rotation window** — tonight (clean secret for 1 Sep, desk `.env` must be updated in the same sitting) vs the 5–6 Sep weekend (safer for the Friday 4 Sep rail, five more days of exposure) | Only you can weigh the Friday rebalance rail against five days of a known-exposed secret | The R1 rotation, and therefore R2/R4's ordering | Both options are costed above; neither is being taken autonomously, because the desk's login is outside this repo and the Friday rail is a safety rail |
| **29c** | **Add a new Google OAuth client secret** in the `baskfy` project and retire the old one after the new one is live | R4 — exposed in a 27 Aug transcript | Nothing; sign-in works. It is an open exposure, not an outage | The two-step add-then-remove procedure is written above so there is no sign-in gap. `BASKFY_GOOGLE_CLIENT_SECRET` was confirmed to reach **only** the `web` container — `worker` and `api` do not have it |
| **29d** | **Decide whether R3 (the Kite api_key) is rotated at all** — it cannot be regenerated; rotating means a new Connect app at ₹2,000/month, which is also §28 option 1 | The api_key is public by design, so the engineering recommendation is "no action". But it is money and it interacts with §28, so it is not an autonomous call | Nothing. It is an open item, and leaving it open is the cost | The reasoning is written above; R1's rotation kills the exposed *pair* regardless of what is decided here |
| **29e** | `BASKFY_KITE_TOKEN_ENCRYPTION_KEY` (R2) — **no hands needed**, listed only so it is not lost | Printed into a 31 Aug transcript | Nothing | Fully automatable on the box; the procedure is in §29.2 step 2. An agent can do this one without you |


## 30. Friday 4 Sep — how the desk logs in now (leaf 1.1.5)

The Kite redirect stays at `https://staging.baskfy.com/api/v1/brokers/callback`, as you decided.
The desk gets its session through a reverse bridge built on 31 Aug. **Nothing here needs your
hands on a normal morning** — this section exists so you know what should happen and what to do
when it does not.

### What should happen

1. Open the Kite login as usual.
2. Zerodha returns you to `staging.baskfy.com/api/v1/brokers/callback?request_token=…`.
3. Caddy immediately redirects your browser to `https://desk.modelbasket.in/callback?request_token=…`.
4. **The desk asks for its basic-auth password.** Have it in the browser, or in your password
   manager — this is the one place the flow can stall. If you cancel, the request token is *not*
   spent; log in again.
5. The desk exchanges the token, updates the running process in place, and lands you on its home
   page with `logged_in=1`. `https://desk.modelbasket.in/status` should then say `"authed": true`.
6. Baskfy borrows the access token back on its own schedule. To do it immediately:
   `AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml exec -T worker python -m baskfy_worker.kite_session_cli pull'`

### If step 3 does not happen

Paste the desk URL by hand — take the `request_token=…` value out of your address bar and open
`https://desk.modelbasket.in/callback?request_token=<that value>`. That is the whole fallback and
it needs nothing from this repo.

### If you have no browser to hand

From the Baskfy box, server to server:

    printf '%s' '<request_token>' | sudo /opt/baskfy/bin/baskfy-desk-handoff

It prints a receipt with the Kite user id and a sha256 prefix, never a token.

### Two things to know

* **Do not set `BASKFY_KITE_API_SECRET` on the box while the desk depends on this bridge.** A Kite
  request token is single-use: if Baskfy redeems it, the desk gets nothing that day. This
  supersedes §3 branch A above — under the design actually built, the empty secret is a choice,
  not an outage.
* The last hop — Kite accepting a *real* request token — is the one thing that could not be
  exercised without you. Everything either side of it was: the redirect fires, the browser lands
  on the desk, the forced command drives the desk's own `/callback`, and the desk's
  `generate_session` was reached and answered by Kite (with `Token is invalid or has expired`, for
  a synthetic token). The first real login is the first full run.

## Swing

Only what needs your hands (SW12, 3 Sep 2026). Everything else the run decided and recorded in
`docs/swing/DECISIONS-SW.md`; the report is `SW-FINAL-REPORT.md`.

| # | What | Why | What it blocks |
|---|---|---|---|
| SW-1 | **`aws sso login --profile baskfy-poc`**, then the five deploy commands in `SW-FINAL-REPORT.md` §"Deploy" — **once more**, for the SW12/SW14 commit (`beb5ff5` is already on the box, SW13-run; the desk vhost waits on **S4** below) | The box has no session an agent can open | The hub's forms and the rewritten gate reaching the box; every morning below runs on `beb5ff5` meanwhile |
| SW-2 | **Confirm the sleeve.** The box's `sw_config` is seeded at ₹25,00,000 / 0.5 % (MD1/MD2, SW13-run: `sw_config_sleeve: 1`); change it on `/me/swing` (SW14, once deployed) or `PATCH /swing/config` — both audited and bounded | A sleeve of ₹0 plans nothing; a wrong one sizes every line wrong | The first plan |
| SW-3 | **The desk-history migration — yes or no.** `tools/migrate-desk` (README there; `--fork-policy` required) moves store A (43,411 rows on 65.0.226.77) into the box's `desk` schema; it needs SSH to the old box, which agents do not have. Until it runs the desk service starts on an empty `desk` schema | D8: the file is evidence; the swing book does not need it, the weekly book's pages do | The weekly book's history on the new desk; nothing swing |
| SW-4 | **The S2 probe morning.** One weekday: Kite login before 09:00, `BASKFY_SWING_TIMING_PROBE=true` in `.env.staging` for the worker (one line, revert after), read `docs/swing/status/S2-kite-timing.md` the same evening | Two Kite timing facts (pre-open `volume`, the forming 09:20 candle) were never observed; the 09:16 scan and the tick-built range are correct either way | Nothing — at most one Beat time moves back to 09:09 |
| SW-5 | **The DRY_RUN drill morning** — `SW-FINAL-REPORT.md` §"The DRY_RUN drill morning", `02` §3.2 | The gate you rewrote (A11) asks for one real session under `DRY_RUN=true` before the flag | SW-7 |
| SW-6 | **Telegram** (optional): create the bot, put `BASKFY_SWING_TELEGRAM_BOT_TOKEN` and `BASKFY_SWING_TELEGRAM_CHAT_ID` in the box's `.env.staging` (never as a `box.sh` argument), restart `desk` + `swing-monitor`. Email needs only the desk's `DESK_SMTP_*` / `DESK_NOTIFY_*` lines in `.env.staging` (`.env.example` §desk, system-only) | The 09:31 focus push; one-way, never a confirm path (MD3) | Nothing — email carries the same line |
| SW-7 | **The flag flip**, by your hand only, after `02` §3.1–3.4 hold and your risk decision is written: `BASKFY_SWING_EXECUTION_ENABLED=true` + `BASKFY_SWING_MONITOR_ENABLED=true` + **`BASKFY_DESK_DRY_RUN=false`** in `/opt/baskfy/.env.staging.compose` (desk, swing-monitor — the desk's own variable since SW13-run; the api's `BASKFY_DRY_RUN` is already `false` and does not reach the desk) **and** `BASKFY_SWING_EXECUTION_ENABLED=true` + `BASKFY_SWING_EP_PREMARKET_ENABLED=true` in `.env.staging` (worker, beat) — two files, compose.prod.yml says why — then `up -d worker beat desk swing-monitor`. A swing order is real only with **both** the desk's `DRY_RUN` off and the flag on (`swing_gates()`); `BASKFY_DESK_DRY_RUN=false` also takes the **weekly book** out of dry-run on this box (its `/execute` still needs your confirm) | Non-negotiable 1; Track B | Real orders |
| SW-8 | **S3 — rotation** (2 Sep): a subagent once ran `docker compose config` unfiltered and the laptop's `.env.staging` values were echoed into its transcript; nothing was written anywhere. Your call under §29's rotation list | §29 R1–R4 class if the box shares those values | Nothing |

Resolved: **S1** (notification channel) → MD3 / SW11: email now, Telegram dark behind SW-6.
**S2** (the two Kite timing questions) → superseded by the 09:04 probe (MD5 / SW11), which is SW-4.

### S4 — One DNS record at GoDaddy for the desk vhost (3 Sep 2026, deploy of beb5ff5)

`baskfy.com`'s nameservers are still GoDaddy's, so the Route 53 record Terraform created for
`desk.staging.baskfy.com` is not authoritative and the name is NXDOMAIN publicly. Add at GoDaddy:
`A  desk.staging  →  3.108.148.38` (or delegate the zone to Route 53). Caddy then passes ACME
on its own; nothing on the box needs touching. Until then the desk service is up and verified on
the box but not reachable from the internet.
