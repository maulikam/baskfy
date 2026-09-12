# Gates — the PKTEA phantom holding (Leaf 1, 12 Sep 2026)

**The report.** Maulik does not hold PKTEA. The box holds
`PKTEA qty=147.0000 kind=CAPITAL added=2026-09-10 history_source=NONE`.

**The finding, in one paragraph.** PKTEA is not seed data, not a fixture and not a bad write. It
is a row that was *correct when it was written* — 147 shares filed into a group created on
10 Sep — and that nothing has looked at since. The only broker-facing write path in production,
`POST /brokers/{id}/sync-holdings` → `baskfy_api.broker_holdings_sync.sync_holdings_into_portfolio`,
rewrites the broker's own pile and **never questions a row in any other portfolio**. The
reconciliation machinery that exists to ask "we record this and the broker does not" —
`baskfy_worker.tasks.holdings_sync.run_holdings_sync`, built on
`baskfy_core.allocation_ledger.attribute_sell` — has **no caller anywhere outside its own test
file**. So a filed holding is write-once: once a share leaves the demat, its slice stays on the
Portfolio page forever, and `_minus_what_is_filed_elsewhere` subtracts that phantom from the pile,
so the total still *balances* while being wrong. The class of bug is "allocated rows are never
re-checked against the broker"; PKTEA is one instance.

**The fix is a reconciliation, not a delete.** §4.3 forbids silently altering a return series, so
the sweep added here takes no decision of its own: it hands the difference to `attribute_sell`. A
position with one capital owner is an attributed sell and the slice moves (criterion 4's
whole-holding case — PKTEA is this); a position split across capital portfolios raises **one OPEN
`reconciliation_item`** and not one share moves. `GET /portfolio/reconciliation`, the attention
ribbon and the command centre's `open_reconciliation_count` already render that item, which is why
this leaf built no new screen — the surface existed and had nothing to show it.

**A second, latent defect fell out of it (gate 14).** `SPLIT_HOLDING` joined
`ReconciliationReason` on 10 Sep 2026 and 0022's CHECK constraint was never widened, so the answer
`attribute_sell` gives for *every* split sell could not be stored at all. Nobody saw it because
nothing writes reconciliation items in production. Migration `0043_split_holding_reason` fixes it.

Run from `decile-blueprint/` unless the CHECK says otherwise;
`$DB = BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test`.

---

**⚠ This file was rewritten by the parent on 12 Sep 2026, and the reason is the point of it.**
It was delivered as prose — `## G1 — … ✅` headings, fenced CHECK blocks, and expectations written
as sentences like *"a portfolio with is_broker_pile=false"*. It claimed **14/14**. The tooling's
answer was `no runnable CHECK lines`: zero checkboxes, unindented attributes, not one `EVIDENCE:`
line. So the count was the author's own bookkeeping and nothing in it could be re-run.

The findings underneath were correct — every one re-measured below. But a ledger that only a human
can read is the thing this discipline exists to replace, and "14/14" on a file the checker cannot
parse is exactly the confident-wrong-number failure. Rewritten with runnable checks, by the parent,
before any of it was believed.

- [x] P1: **PKTEA is a row Maulik filed himself, not a broker artefact and not seed data.** It sits
      in his own group, whose `is_broker_pile` is false.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select p.id||'|'||p.name||'|pile='||p.is_broker_pile||'|qty='||ph.quantity::text from portfolio_holding ph join portfolio p on p.id=ph.portfolio_id join instrument i on i.id=ph.instrument_id where i.symbol='PKTEA'" 2>&1 | tail -1
  EXPECT: /^6\|Swing Manual\|pile=false\|qty=147\.0000$/m
  EVIDENCE: 6|Swing Manual|pile=false|qty=147.0000 — filed on 2026-09-10 into portfolio 6. It was true the day it was written; nothing has looked at it since.

- [x] P2: **The seeder cannot have written it**, because the seeder never touches the table at all.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "portfolio_holding\|PortfolioHolding" decile-blueprint/services/api/src/baskfy_api/seed.py
  EXPECT: /^0$/m
  EVIDENCE: 0 — `seed.py` makes no reference to `portfolio_holding` in any form, so no seeded row of any symbol is possible.

- [x] P3: **The disappearance sweep exists and takes no decision of its own** — it hands the
      difference to `baskfy_core.allocation_ledger.attribute_sell`, which is where that judgement
      already lived.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "attribute_sell" decile-blueprint/services/api/src/baskfy_api/broker_holdings_sync.py
  EXPECT: /^[1-9][0-9]*$/m
  EVIDENCE: the sweep calls `attribute_sell` rather than deciding what a disappearance means. Before this, nothing read a filed row when the broker stopped reporting it: a filed holding was effectively write-once.

- [x] P4: **The whole spec is tested**, both branches of the ownership test, the freeze, house
      rule 7's idempotency, and the three refusals.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf1 uv run pytest services/api/tests/test_broker_sync_reconciles_phantoms.py -p no:cacheprovider 2>&1 | tail -1
  EXPECT: /^9 passed/m
  EVIDENCE: 9 passed. Sole capital owner is attributed and the slice reduced; split ownership raises one OPEN `reconciliation_item` and moves NOTHING (§4.3's freeze); the three refusals are non-live reads, empty reads, and any instrument an unresolvable symbol could have meant.

- [x] P5: **A second defect, found on the way and worth more than the first.** `SPLIT_HOLDING`
      joined `ReconciliationReason` on 10 Sep and migration 0022's CHECK constraint was never
      widened — so the answer `attribute_sell` gives for *every* split sell could not be stored.
      It went unnoticed for two days precisely because nothing ever wrote a reconciliation item.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && ls decile-blueprint/services/api/alembic/versions/ | grep -c "0043_split_holding_reason"
  EXPECT: /^1$/m
  EVIDENCE: `0043_split_holding_reason.py` widens the constraint. The test iterates the enum rather than listing it, so the next member added is covered without anyone remembering to.

- [x] P6: **The migration chain has one head**, so leaf 1's and leaf 4's migrations did not fork it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api && BASKFY_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_leaf1 uv run alembic heads 2>&1 | tail -1
  EXPECT: /^0043_split_holding_reason \(head\)$/m
  EVIDENCE: 0043_split_holding_reason (head) — linear: 0041_twt -> 0042_twt_scan_run -> 0043_split_holding_reason.

- [x] P7: **Nothing in the sweep can reach a broker.** A holdings reconciliation is a bookkeeping
      act; if it could place, it would be the most dangerous code in the repo.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -cE "place_order|OrderGateway|place_gtt" decile-blueprint/services/api/src/baskfy_api/broker_holdings_sync.py
  EXPECT: /^0$/m
  EVIDENCE: 0 — no order path of any kind in the file.

- [x] P8: **PKTEA was not alone, and the whole book is audited rather than the one symbol Maulik
      happened to notice.** Every filed row is compared against a live broker read.
      ⚠️ **The first run of this audit reported all 20 rows as phantoms**, including two written by
      a live sync that same morning. The script read `result.holdings`; the field is `result.rows`,
      so it got an empty default and a silent zero. `HoldingsResult.__post_init__` makes that
      reading impossible on the real object — a row-bearing source with no rows raises — so the
      output was self-contradictory and was caught by asking why, rather than by believing it.
      **A wrong attribute name in THIS script is a script that recommends selling everything.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh api ops/holdings-audit/phantom-audit.py 2>&1 | grep -E "^matched="
  EXPECT: /^matched=18 phantom=2$/m
  EVIDENCE: matched=18 phantom=2 against a live broker read of 19 symbols. **PKTEA 147 and SIGMA 1,444**, both filed in `Swing Manual`, are reported by no broker. Ruled out as a naming mismatch by printing the broker's own list: it contains no variant of either. Also surfaced: **`SGBDE31III` is held by the broker and filed in no portfolio** — a Sovereign Gold Bond, which non-negotiable #6 blocks from trading at the lowest layer, so it is correctly untouchable but is real money sitting outside every group.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: P<n> <reason> is the honest exit. -->
