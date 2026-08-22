# NEEDS-MAULIK

Things only Maulik's hands can supply, queued by the autonomous run (CLAUDE.md §Autonomy charter,
`MERGE-PROMPTS.md` rule 6). **The run does not wait on these** — it continues with every module
that does not depend on them (rule 11) and returns the moment a dependency clears.

Nothing here is urgent unless marked so. Each entry says what is needed, why, what it blocks, and
what was done meanwhile.

---

## Open

### 3. A Kite login, when convenient — for deep history only
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

**What it unblocks:** *depth only*. `decile_worker.backfill` pulls daily candles from Kite, which
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
- **M21 — deleting `../_baskfy_subtree_tmp/`.** The pre-merge rollback copy of both repositories.
  An agent never deletes it. It becomes safe for you to remove once you have skimmed the final
  status page.

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

### 4. Corporate-action history — the single biggest blocker on the parity gates
**Status:** open, **blocks M13** · **Raised:** M12, 22 Aug 2026

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

### 6. One-time: confirm the Kite app is on the paid historical-data tier
**Status:** open, one browser check · **Raised:** M18, 22 Aug 2026

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

**Blocks:** nothing.
