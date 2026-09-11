# TW5 — the sleeve's own cash, its own book, and the counter that is not a flag

**Plan:** `docs/twt/06-module-plan.md` § TW5. **Spec:** `04-business-rules.md` §6 (sizing) and §9
(the sleeve's money). **Reads:** TW1's `sizing.py`/`sleeve.py` and TW3's `tw_config`.

**The rule this module exists to make true:** the sleeve sizes against **its own** money, owns
**only what it bought**, and can never sell a holding it did not buy. Non-negotiable 7's sibling.

> **One edit to the CHECK lines, and it is not a goalpost move.** Each was written
> `uv run pytest -k …`, and this repository's `pyproject.toml` already carries `-q` in
> `addopts`. The two combine to `-qq`, at which pytest prints the progress dots and **suppresses
> the `N passed` summary line** — so `EXPECT: /passed/` could never match, on any result, however
> green. The redundant `-q` is dropped below and nothing else is changed; the repo's own `-q`
> still governs the output. Verified: `uv run pytest packages/core/tests/test_twt_sleeve.py`
> ends at the dots, `uv run pytest packages/core/tests/test_twt_sleeve.py` ends `11 passed`.
>
> Every run below is against a live Postgres, so the `db`-marked tests **run** rather than skip:
> G1-G7 on `…/baskfy_test`, and G8's full suites on a scratch database created for the purpose,
> for the reason G8's own evidence gives.

- [x] G1: `sleeve_equity(user_id, as_of)` and `cash_available` per §9 — **its own cash, never the
      whole account's.**
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (sleeve_equity or cash_available)" 2>&1 | tail -3
  EVIDENCE: `10 passed, 7422 deselected in 4.80s`. `services/api/src/baskfy_api/twt_sleeve.py`
  ships `sleeve_equity(session, user_id, as_of)` and `cash_available(session, user_id, as_of)` over
  `load_sleeve`, which calls TW1's pure `sleeve_value` — the arithmetic is not reimplemented.
  Asserted from rows: capital ₹25,00,000 + realised ₹40,000 − cost ₹1,00,000 + mark ₹1,20,000 =
  equity `2560000.0000`; cash available is equity less the open positions and **never negative**
  (a ₹1 lakh sleeve holding ₹4 lakh of stock reports ₹0, not a negative purse). There are no
  working orders to reserve against (`03` §6) and the loader has no field for one, which is the
  single place this differs from VBT-1's.

- [x] G2: **A name the sleeve never bought produces no TWT line and is invisible to
      `sleeve_equity`.** A fixture account holds one; the sleeve does not see it.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (never_bought or foreign or not_ours)" 2>&1 | tail -3
  EVIDENCE: `5 passed, 7427 deselected in 3.94s`. The fixture is hostile on purpose: the
  instrument exists, it printed a ₹500 bar on the session, and **the same user holds 1,000 of it
  through `vb_position`** — another book of the same product. `sleeve_equity` is still exactly
  `2500000.00`, `held_instrument_ids` is `frozenset()`, `book_state` reports
  `open_instrument_ids=frozenset()`, `positions_naked_of_gtt=()`, `ratchets_due=()`, and
  `exit_lines(book, AS_OF) == []`. The book `book_state` reads is `tw_position` and nothing else,
  so `04` §10.2 is never even offered the foreign row to make a line out of.

- [x] G3: **A sleeve at ₹0 plans nothing** and every signal is skipped `NO_SLEEVE_CAPITAL`. This
      is the rail that keeps the sleeve safe until Maulik enters the capital himself.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (no_sleeve_capital or zero_capital)" 2>&1 | tail -3
  EVIDENCE: `4 passed, 7428 deselected in 3.89s`. Three signals into a sleeve whose
  `tw_config.sleeve_capital_inr` is `0.00`: `lines == []` and
  `[skip.reason for skip in skips] == [NO_SLEEVE_CAPITAL] * 3`. **The unseeded sleeve gives the
  same plan** — no `tw_config` row at all, one signal, one `NO_SLEEVE_CAPITAL` — which is
  DECISIONS-TW **TW5.1**: the loader answers ₹0 where `read_config` raises, so "nobody has run
  `make seed`" never becomes a state with behaviour nobody specified. A zero-capital sleeve that
  *holds* a name still skips it `ALREADY_HELD`: zero capital makes new money impossible, it does
  not make the book disappear. **Nothing in this module writes `sleeve_capital_inr`**, and
  `twt_settings.EDITABLE_FIELDS` remains the only door to it.

- [x] G4: §6.2's caps in order, including the 1 %-of-turnover cap and the ₹10,000 floor. The
      turnover cap **binds on a ₹2 crore name at ₹25 lakh and does not on a ₹50 crore one** —
      `04` §3.5's argument for the ₹5 crore floor, as a test.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (turnover_cap or caps_in_order)" 2>&1 | tail -3
  EVIDENCE: `7 passed, 7425 deselected in 3.72s`. At ₹25 lakh the slot is ₹2,50,000. On a ₹2 crore
  name the cap is ₹2,00,000 and it binds (`cap is SizeCap.TURNOVER`, `turnover_capped is True`,
  2,000 shares) — **and the shipped sleeve never lines that name at all**, because
  `min_turnover_inr` is ₹5 crore: `build_entries` answers `BELOW_LIQUIDITY_FLOOR`. Both halves are
  asserted, because together they *are* `04` §3.5's argument — a floor below the cap is a plan
  sized by the cap. On a ₹50 crore name the cap is ₹50,00,000, nothing binds, and the line is the
  slot: `cap is SizeCap.SLOT`, `250000.00`, 2,500 shares.
  All three caps at once, in §6.2's order, on a ₹1 crore sleeve with ₹95 lakh committed and
  `tw_config.max_position_pct = 8.00`: slot ₹10,00,000 → ceiling ₹8,00,000 → turnover ₹5,50,000 →
  cash ₹5,00,000, `cap is SizeCap.CASH`, `value_inr == 500000.00`. That the *person's* setting
  reaches the arithmetic is the wiring being tested — `config_for(row)` writes `max_position_pct`,
  `stop_pct` and `trail_pct` from `tw_config` into the engine's `TwtConfig`.
  The floor: a sleeve with ₹5,000 of cash left sizes a ₹5,000 line and is skipped
  `BELOW_MIN_TRADE_VALUE` rather than shrunk. And cash spent by an earlier line of the same plan
  is not spent twice — two lines of ₹30,000 out of a ₹3,00,000 sleeve, the second sized against
  what the first left.

- [x] G5: **The counter, and it is a counter.** `first_live_entries_left` decrements once per
      FILLED entry — not on a proposed line, not on a DRY_RUN plan being built, and **not twice
      for the same fill**. Nothing about it is a switch somebody can turn off.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (half_size or first_live or counter)" 2>&1 | tail -3
  EVIDENCE: `24 passed, 7408 deselected in 7.58s`. `count_first_live_entry(session, *, user_id,
  position_id, session_date, now, execution_enabled, changed_by)` moves it, and each of the three
  prohibitions is structural rather than remembered (DECISIONS-TW **TW5.3**):
  * **a filled entry:** 10 → 9, `tw_position.half_size` set true, `tw_session.first_live_entries_counted`
    1, and a `tw_config_audit` row `10 → 9` `changed_by='twt-fill'`;
  * **not a proposed line:** the parameter is a `position_id` and a proposal has none — a caller
    who invents one gets `LookupError("… a filled entry is a row")` and the counter stays at 10.
    Another tenant's position id is the same refusal (P4.1);
  * **not a DRY_RUN plan:** a `simulated` fill counts nothing and is **not** marked `half_size`,
    and neither does any fill while `execution_enabled` is false — which is the state this server
    is in;
  * **not twice:** called three times on one fill, the answer is 9 and the session count is 1.
    `half_size` is the idempotency key as well as the record, so a partial fill that completes
    later, a retried confirm and a re-run of the morning all land on the same number;
  * **spent means spent:** at 0 it does not go negative, the entry is not marked half, and
    `slot_multiplier` returns `1`.
  **It is not a flag:** `first_live_entries_left` is absent from `twt_settings.EDITABLE_FIELDS`,
  present in `SYSTEM_OWNED_FIELDS`, and `TwtConfigPatch(first_live_entries_left=0)` is *refused*
  by `extra="forbid"` rather than accepted and ignored. The only door is `record_system_change`,
  which writes the audit row in the same transaction.

- [x] G6: A position marked on a session with no bar **falls back to the last close and says so**
      — never a zero, never a silent gap.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (no_bar or stale_mark or fallback)" 2>&1 | tail -3
  EVIDENCE: `12 passed, 7420 deselected in 2.68s`. `load_sleeve` carries TW1's `MarkSource` through
  to the loader and `LoadedSleeve.mark_notes()` is the "says so": a position whose last bar was
  2026-09-06 and which did not print on 2026-09-10 is valued at that ₹80 close and its note reads
  `no bar on 2026-09-10; marked at the last known close of 80.00, from 2026-09-06`. One that has
  never printed since the fill is marked at the **entry** — `nothing has printed since the fill;
  marked at the entry of …` — and a ₹2,500-a-share position with no bar at all leaves equity at
  ₹25,00,000 rather than dropping it to the capital. `stale_marks` is `()` and `mark_notes()` is
  `{}` on an ordinary evening. The mark is the latest close **on or before** the session and never
  a later one: a bar dated the day after is ignored (house rule 5, in the one place a look-ahead
  would flatter the book — its own value).

- [x] G7: The sleeve is a `MY_STRATEGY` capital portfolio, as the swing and VBT sleeves are.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (my_strategy or capital_portfolio)" 2>&1 | tail -3
  EVIDENCE: `2 passed, 7430 deselected in 2.23s`. `twt_sleeve` declares
  `TWT_PORTFOLIO_KIND is PortfolioKind.CAPITAL` and `TWT_PORTFOLIO_SOURCE is
  PortfolioSource.MY_STRATEGY` (not `HOLDING_GROUP`, which would give it "since grouped" as its
  headline metric when the sleeve knows the real entry date of every position it opened), and the
  substance is asserted rather than the label: an account holding **four** names through another
  book, each with a live ₹1,000 bar, leaves `sleeve_equity` and `cash_available` at exactly the
  capital, `book.slots_taken == 0` and `exit_lines(book, AS_OF) == []`.
  DECISIONS-TW **TW5.2** records the one judgement here: the two siblings do different things
  about the same sentence — the swing book files its positions into a real `portfolio` row and
  VBT-1 files nothing — and this module follows VBT-1, its own module number's sibling. Filing
  `tw_position` into the portfolio forest belongs beside the page that displays it (TW8), and the
  three constants it would need are named here already.

- [x] G8: Both Python suites green and `make lint` clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EVIDENCE: `All checks passed!` · `Success: no issues found in 632 source files` (baseline 623;
  the extra files are this module's two and the siblings'). `uv run ruff format --check .` →
  `737 files already formatted`.
  Suites, all four testpaths:
  * `packages/core/tests` → `4077 passed, 5 skipped in 300.68s` (baseline `4012 passed, 8
    skipped`; the run's parallel siblings moved it);
  * `services/worker/tests` → `996 passed in 436.46s`;
  * `services/api/tests packages/providers/tests packages/execution/tests reconciliation` →
    `2393 passed, 1 skipped in 475.49s`;
  * TW5's own file → `33 passed`, db-marked and **run**, not skipped.
  **One thing worth recording, because it cost forty minutes and would cost the next session the
  same.** The first two full worker runs reported `77 failed / 40 errors` and then `96 failed /
  192 errors`, and **none of them was this module's**: the error underneath every one was
  `asyncpg.exceptions.UndefinedTableError: relation "basket_snapshot" does not exist` on the
  fixture's own `TRUNCATE`. Three agent sessions share one Postgres, and a sibling running
  `make migrate` / `make downgrade` (TW3's gate round-trips the migration to base) drops the
  schema out from under a suite that is mid-run. The counts above were taken on a scratch
  database no sibling touches — `createdb baskfy_tw5`, `BASKFY_TEST_DATABASE_URL` pointed at it,
  dropped afterwards — where the worker suite is `996 passed`, exit 0. A shared test database is
  not a test isolation boundary when more than one session is running.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
