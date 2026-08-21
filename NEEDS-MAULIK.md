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

**What is needed:** one Kite login, exactly as you do it now — `./run.sh`, click Login, complete
the flow. That writes a fresh token the pipeline can borrow. Nothing else.

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
