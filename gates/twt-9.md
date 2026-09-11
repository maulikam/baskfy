# TW9 — the backtest on the page, from the plant's own bars, with its drift named

**Plan:** `docs/twt/06-module-plan.md` § TW9. **Reads:** TW2's engine, TW3's `tw_backtest_run`,
TW8's page (which already has the slots and renders empty until this lands).

**Goal:** a number, from the plant's own bars, beside the study's, with the difference between
them named rather than explained away.

- [ ] G1: `tools/twt/backtest.py` and `make twt-backtest` run the engine over `ohlcv_daily` at the
      **shipped** ₹5 crore floor, sized against `params.sleeve_inr` and **never** against
      `tw_config.sleeve_capital_inr` — asserted by a spy, because reading the live capital into a
      backtest is how a research number quietly becomes a claim about the user's money.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (capital or spy or sleeve_inr)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G2: **Append-only.** Two runs of the same date produce two rows and edit nothing.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (append or two_runs)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G3: A run that raises writes `finished_at` and `error` **and re-raises** — a failed run that
      looks finished is worse than one that is obviously broken.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and (raises or error)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: The page shows the latest **finished** run per source and **not** a later failed one.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "finished" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G5: Drift is computed against `01` §6 and **flagged above 1.0 CAGR point**; a synthetic
      result 1.5 points away renders the warning **naming both numbers**.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "drift" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G6: `tw_backtest_run.stats` carries every key TW8's page reads — including
      **`gate_off_cagr_pct`, the one number that justifies the gate**, whose slot on the card
      nothing else fills.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and backtest and stats" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G7: **TW2.2's warning is honoured.** The research panel's ETFs carry so few bars that
      including them in the breadth denominator measured as zero difference; that is a property of
      the sparse export and **not of the plant**. This run must not inherit the assumption.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (etf or denominator)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G8: Every suite green and both lints clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
