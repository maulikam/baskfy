# Gates: E1 — the record catches up with the code

Scope: every judgement call this tree took is written down as UNREVIEWED, the status page is loud about what is still not done, the schema addendum matches the ORM, and the landing-page claim is either true or corrected.

- [x] G1: a DECISIONS-MERGE entry exists for the portfolio graph, tagged UNREVIEWED, with alternatives and a reversal path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -nE "portfolio graph|portfolio_graph|parent_id" docs/DECISIONS-MERGE.md | head -5
  EXPECT: /parent_id|portfolio graph/
  EVIDENCE: 3992:response, the web tree view, the per-broker roll-up — and an uncapped `parent_id` is a | 4007:**Reversal.** `alembic downgrade 0018_trading_path_tenancy` drops `parent_id` and its constraint;

- [x] G2: every decision entry added by this tree carries the UNREVIEWED tag
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -nE "^## PM[0-9]" docs/DECISIONS-MERGE.md | grep -cE "UNREVIEWED" | awk '{print "TAGGED="$1}' && grep -cE "^## PM[0-9]" docs/DECISIONS-MERGE.md | awk '{print "ENTRIES="$1}'
  EXPECT: /TAGGED=(\d+)\nENTRIES=\1/
  EVIDENCE: TAGGED=12 | ENTRIES=12

- [x] G3: the status page names what this tree did NOT do — nine broker adapters remain unwired, no consolidated live net worth
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -nE "unwired|not done|still open" docs/00-merge-status.md | head -5
  EXPECT: /unwired/
  EVIDENCE: 797:**1. Nine of ten brokers remain unwired. Only Zerodha has a holdings adapter.** | 807:pretends unwired brokers returned data."*

- [x] G4: the schema addendum describes the tables as they now are, and a test binds the doc to the ORM
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_schema_matches_docs.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ................                                                         [100%] | 160 passed in 0.33s

- [x] G5: NEEDS-MAULIK.md lists exactly what each unwired broker needs from Maulik's hands, and what it blocks
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && n=0; for b in Upstox "Angel One" Fyers 5paisa Dhan "ICICI Direct" Kotak HDFC Groww; do grep -qF "$b" NEEDS-MAULIK.md && n=$((n+1)); done; if [ "$n" -eq 9 ] && grep -qiE "consolidated holdings for (any|every) non-Zerodha account" NEEDS-MAULIK.md; then echo "GATE_OK BROKERS_NAMED=$n"; else echo "GATE_FAILED BROKERS_NAMED=$n"; fi
  EXPECT: /GATE_OK BROKERS_NAMED=9/
  EVIDENCE: GATE_OK BROKERS_NAMED=9

- [x] G6: the landing-page sentence about a momentum basket beside a long-term core is now true of the product, or the copy was corrected — state which, with the file:line
  EVIDENCE: BOTH SETTLED, recorded in docs/00-merge-status.md "Two landing-page claims, checked against the code (tree 5 G6)". (1) `apps/web/src/lib/marketing/landing-band.ts:120` "A momentum basket can sit beside a long-term core and still be read as one position." is NOW TRUE and was not before: basket sleeves exist at `services/api/src/baskfy_api/routers/sleeves.py:183` (`BASKET: Final = "basket"`) with the DB constraint at `services/api/alembic/versions/0019_portfolio_graph.py:327`, plus `portfolio.parent_id` and `cb_investment.portfolio_id`. No change needed. (2) `apps/web/src/lib/marketing/landing-band.ts:124` "Connect the brokers you already use and your holdings sync in." is FALSE for 9 of 10 brokers (`_HOLDINGS_WIRED == {"zerodha"}`, measured) and NEEDS CORRECTING — not defensible as aspirational, because it is present tense on the acquisition surface and the same product's own API now returns `holdings_sync="planned"` for those nine. Exact replacement handed off: "Connect Zerodha and your holdings sync in; the other nine brokers are listed as planned, not wired. Orders go out as a read-only plan you confirm on the broker's own screen." Both sentences render via `apps/web/src/components/marketing/landing-band.tsx:169`. E1 does not own `apps/web/**` and did not edit it; `lib/marketing/**` is outside leaf D1's ownership too, so this is an unassigned handoff, not a completed edit.

- [x] G7: the brief's "seven fixture paths" count is corrected in the record to the measured six
  CHECK: echo "CORRECTED=$(grep -cE 'paths\. The measured number at the pre-tree commit was \*\*six\*\*' docs/00-merge-status.md docs/DECISIONS-MERGE.md | awk -F: '{s+=$2} END {print s+0}')"
  EXPECT: /CORRECTED=[1-9]/
  EVIDENCE: CORRECTED=2

- [x] G8: no secret, token or connection string was written into any doc
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "SECRET_HITS=$(grep -rhoE 'postgresql\+asyncpg://[^ ]*:[^ ]*@|api_key=|access_token=|Bearer [A-Za-z0-9._-]{16,}' docs/ NEEDS-MAULIK.md decile-blueprint/docs/04b-pipeline-tables-addendum.md 2>/dev/null | wc -l | tr -d " ")"
  EXPECT: SECRET_HITS=0
  EVIDENCE: SECRET_HITS=0
