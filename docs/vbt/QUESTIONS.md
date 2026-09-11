# QUESTIONS — for Maulik, with the pack's standing default for each

The run never waits on these. Each carries the default it proceeds on; an answer here (or in
`NEEDS-MAULIK.md` § VBT) becomes a `DECISIONS-VB.md` entry and the module that reads it adjusts.

**Answered by Maulik, 11 Sep 2026: Q1, Q3, Q4.** The rows carry the answers; the standing
defaults are kept beside them so a later reader can see what changed and when.

| # | Question | Standing default the run uses |
|---|---|---|
| Q1 | **The money.** What capital does this sleeve get, and is equal weight across ten slots the sizing you want? Written into `DECISIONS-VB.md` as the risk decision `02` §3.4 requires | ✅ **ANSWERED 11 Sep 2026: ₹25 lakh**, equal weight, ten slots — slots of ₹2.5 lakh. `vb_config.sleeve_capital_inr` is still **seeded at 0** and is set by hand (DECISIONS-VB VB11.1); the seed does not change, because a sleeve with no money is a sleeve that cannot trade by accident. *Was: ₹10 lakh, the study's own frame, which every fixture and the backtest still use* |
| Q2 | **The stop.** 12% is the study's; 10% and 15% cost about 1.8 CAGR points and change the drawdown by ±1.5 (STRATEGY §4). Do you want a different one? | **12%**, and it is a bounded `vb_config` setting (ceiling 15%) so you can change it on the page without a code change |
| Q3 | **Does this sleeve share the swing sleeve's delegation?** `docs/swing/02` §3.5 lets an agent flip the *swing* execution flag on your written decision. That delegation names the swing flag | ✅ **ANSWERED 11 Sep 2026: NOT delegated, and it stays that way.** No agent may set `BASKFY_VBT_EXECUTION_ENABLED` under any circumstances. Unchanged from the run's own default, now confirmed rather than assumed |
| Q4 | ✅ **ANSWERED 11 Sep 2026: no paper gate at all** — *"i dont want to have dry run at all go live"* (DECISIONS-VB VB11.4). `DRY_RUN_SESSIONS_REQUIRED` is 0; the counter still counts. *The question was:* **Twenty DRY_RUN sessions, or the swing sleeve's shorter gate?** You withdrew the twenty-session paper gate for the swing book (STANDING-ANSWERS A11) because its code work was complete and one real DRY_RUN morning bought the same rehearsal | **Twenty sessions** (PACK.7). This sleeve has rehearsed nothing and its new mechanism — a limit that expires after three sessions — can only be exercised by sessions passing |
| Q5 | **The 262 missing instrument-days and the pre-2024 corporate actions** (STRATEGY §1, §6). Re-running the study once the plant fills them is the note's own recommendation. Do you want that before the flag, or after? | The run builds VB9's drift flag so the number is re-measured from the plant's bars every time it is asked, and the gap is visible. It does **not** block on the backfill; `NEEDS-MAULIK.md` § VBT carries it |
| Q6 | **Should the sleeve hold cash in a liquid fund when the gate is shut?** The book is idle 37% of the time; STRATEGY §5 notes interest on cash would add ~2 CAGR points | **No.** Long-only equity and cash otherwise, exactly as studied. Anything else is a second instrument, a second product gate and a second tax treatment |
| Q7 | **One sleeve or two books?** The strategy could run a second, larger-cap parameterisation beside this one | **One.** `04`'s numbers are one book's; a second would need its own study, its own tables and its own capital |
| Q8 | **The desk page's refresh.** The swing page polls every five seconds during the session. This one is end-of-day | **No polling.** The plan is built twice a day and the page refreshes on demand (`05` §3) |
