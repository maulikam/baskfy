# The kickoff prompt

Paste the block below into a fresh CLI session at the repo root. The run is resumable: a fresh
session reads `docs/options/STATUS.md` and continues from the first module not marked ✅.
**Do not start the condor (OC) run** — it is absorbed here as sleeve O1.

---

```
This is the OP run: build three intraday NIFTY index-option sleeves into Baskfy —
O1 hedged premium selling (the condor, monthly and weekly), O2 directional
buying, O3 expiry-day debit-spread setups — as scans on the web first, then
plans, paper execution through the gateway, exits, a journal and backtests, with
every money flag off.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. docs/options/README.md          ← how this pack absorbs docs/condor/
4. docs/options/02-scope-and-gating.md   ← the law of this run
5. docs/options/07-data-reality.md       ← what a backtest may claim
6. docs/options/06-module-plan.md        ← the task list
7. docs/options/04-business-rules.md     ← the numerical contract (tests assert it)
8. docs/condor/01, 04, 07                ← authoritative for sleeve O1
Read docs/options/01, 03, 05 as modules cite them; docs/options/QUESTIONS.md
holds the standing defaults for everything only Maulik can answer.

Then execute OP0 through OP15 in order, under the Autonomy charter in CLAUDE.md:
run long, decide-record-continue, questions to Maulik are the exception.

- One commit per module: "OP<N>: green — <one line>". Never git add -A; commit
  only the module's files. No module ends with either tree's suite broken; the
  desk must rebalance on any Friday and swing, TWT and VBT must run on any morning.
- Never place a live order. DRY_RUN=true in every environment you create.
  OPTIONS_ENABLED, INTRADAY_ENABLED and every BASKFY_OPTIONS_*_EXECUTION_ENABLED
  stay false; you never flip any of them. There is no auto-execute flag for any
  options sleeve and you never add one. The web app gets no order route.
- Track C in docs/options/02 is forbidden: no overnight option, no product but
  MIS, no naked short at any instant, no auto-executed entry, no stock options,
  no underlying but NIFTY, no futures/calendar/ratio legs, no rolling or
  re-entry, no touching frozen/strangle (port by re-implementation only), no
  touching the weekly, swing, TWT or VBT books, no new data provider, no sizing
  from margin.
- Every threshold is a field of baskfy_core.options.config; lot sizes, strikes
  and expiry dates come from the NFO master, never from a constant or a weekday
  rule. Money is Decimal; prices round at write time.
- A backtest number carries its tier and caveat (docs/options/07 §4) on the row
  and on the page. Never pool tiers, sleeves, real and simulated, or
  paper-one-lot and budget-sized rows.
- Before "fixing" any setting that disagrees with a doc, apply CLAUDE.md's
  "the decision wins" rule (git log -S). In particular O2's trend filter reads
  NIFTY 50 on purpose, not the swing gate's MidSmall 400.
- Judgement calls go in docs/options/DECISIONS-OP.md, numbered by module,
  tagged ⚠ UNREVIEWED, with rejected alternatives and the reversal path.
- Anything only Maulik's hands can supply (a Kite login, the F&O segment, the
  margin pool, a data purchase, a flag flip, sleeve capital) goes to
  NEEDS-MAULIK.md under an "Options" heading; keep working on everything else.
- Update docs/options/STATUS.md at the end of every module, loud about what is
  NOT done. If context runs long, write enough there to resume losslessly.
- Do not weaken tests to pass modules. Tests assert docs/options/04, never
  current behaviour.

Stop only for the charter's stop conditions. When OP15 is green — or a hard
blocker ends the run — write OP-FINAL-REPORT.md at the repo root in the style of
SW-FINAL-REPORT.md: what was built, what was decided, what is NOT done, what
needs Maulik, the first paper morning step by step, and the per-sleeve paper
checklist of docs/options/02 §3.2. Then ask Maulik to review.
```

---

## If the run is resumed

Same prompt. The session reads `STATUS.md`, finds the first non-green module, and continues; a
test that already passes is not rewritten.
