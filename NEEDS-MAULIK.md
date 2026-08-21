# NEEDS-MAULIK

Things only Maulik's hands can supply, queued by the autonomous run (CLAUDE.md §Autonomy charter,
`MERGE-PROMPTS.md` rule 6). **The run does not wait on these** — it continues with every module
that does not depend on them (rule 11) and returns the moment a dependency clears.

Nothing here is urgent unless marked so. Each entry says what is needed, why, what it blocks, and
what was done meanwhile.

---

## Open

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
