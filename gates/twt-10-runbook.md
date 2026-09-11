# TW10a — `docs/twt/FIRST-LIVE-MORNING.md`, written before the morning it describes

**Plan:** `docs/twt/06-module-plan.md` § TW10, the runbook half. The safety-property half is
`gates/twt-10.md` and is NOT this file — it needs the whole sleeve and comes last.

**Why it is written now.** Maulik's decision of 11 Sep 2026: **no paper phase.** The sleeve goes
live at ₹25 lakh the day he flips the flag. A runbook written the night before a first live
session is written by someone tired; this one is written while the reasoning is fresh, and the
modules that follow are built to match it rather than the other way round.

**The run never flips the flag, never sets the capital, never places an order.** This document
tells a human how to do all three. That distinction is the module.

- [x] G1: The file exists and names **every step with the command that runs it** — a runbook whose
      steps are prose is a runbook somebody improvises at 09:05.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && test -f docs/twt/FIRST-LIVE-MORNING.md && grep -c '^\s*\(\$\|make\|uv run\|curl\|bash\|docker\)' docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*[1-9][0-9]*\s*$/
  EVIDENCE: CHECK run 11 Sep 2026 → `13`. Thirteen command lines begin at column 0 with `make`/`curl`/`bash`/`uv run`; dozens more begin with `cd` or `AWS_PROFILE=` inside fenced blocks. Every step in §2, §3, §4, §6, §7, §8, §9 and §10 is a fenced command with its expected output and its failure branch — no step is prose only.

- [x] G2: The morning sequence is complete and in order: the Kite login before 09:00, the token
      sync, the flag, the capital setting, `/analyze`, **what the plan must show before he
      confirms**, `/execute`, the GTT check at 09:20 and again at 15:15.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -ciE "kite login|token sync|the flag|capital|/analyze|/execute|09:20|15:15" docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*[8-9]|^\s*[1-9][0-9]/
  EVIDENCE: CHECK run 11 Sep 2026 → `51`. The sequence is §3's table in order: Kite login by 08:45 (§3.1), token sync + desk restart (§3.2), `/status` (§3.3), the flag (§3.4), the capital (§3.5), build the plan (§4), **what the plan must show** (§5, with §5.5's twelve stop-and-do-not-confirm conditions), confirm (§6), the GTT check at 09:20 and the sweep at 15:15 (§8). TWT's `/analyze` is `make twt-plan … SOURCE=MORNING` + `GET $DESK/twt`; its `/execute` is `POST $DESK/twt/execute` with `confirm=true`, an unexpired `plan_id` and a 30-minute expiry — §6 names all three and the 410 that enforces the third.

- [x] G3: **How to stop the sleeve in one command** is in the document, and it is a single command
      a person can run from a phone. This is the most important line in the file.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -A 4 -iE "stop the sleeve" docs/twt/FIRST-LIVE-MORNING.md | head -12
  EXPECT: /stop/i
  EVIDENCE: §7 "How to stop the sleeve — one command". CHECK run 11 Sep 2026 printed the heading and the four lines under it. The command is one line, no setup beyond §1's three exports:
        `curl -fsS -X POST $DESK/twt/halt -u desk:$DESK_PW -H "Origin: $DESK" -d confirm=true`
      Marked `[NOT YET REAL — TW6 builds it, to this spelling]`, with the four behaviours TW6 must implement — zero the capital (audited), expire every live plan, **never remove protection** (resting GTTs stay, `ARM_GTT`/`RAISE_GTT_STOP` keep working), return the previous capital. Three fallbacks follow, two of them real today: `box.sh … stop desk`, and "do nothing" — no job in this sleeve can place an order without a confirm.
      The `Origin` header is in the command because `app/core/websec.py::DeskSecurity` 403s any POST that cannot name its origin; a phone command without it fails in a way that reads like a permissions problem.

- [x] G4: **What to do when a GTT is missing** — the failure that leaves a live line with no stop,
      which is the one this sleeve can actually be hurt by.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -ciE "gtt is missing|missing gtt|naked" docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*[1-9]/
  EVIDENCE: CHECK run 11 Sep 2026 → `12`. §9 is the whole section: three named causes (a fill without an arm; a ratchet that cancelled and did not re-arm — the likely one, because the ratchet runs on up to ten lines a session for months; a corporate action), and the distinction that a *failed cancel* leaves the old stop resting and is not naked. Four steps in order: `POST $DESK/twt/rearm`; read the refusal (three named refusals, each with its own answer); arm the GTT by hand in the Kite app with the exact trigger/limit/quantity, then `POST $DESK/twt/reconcile`; and close the line if no stop can be armed at all. §9.3 is the one thing never to do — never cancel a resting stop to tidy a discrepancy.

- [x] G5: The daily routine after the first morning — login, the ratchet plan, confirm, the sweep.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -ciE "ratchet|sweep|daily routine" docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*[3-9]|^\s*[1-9][0-9]/
  EVIDENCE: CHECK run 11 Sep 2026 → `27`. §10 "The daily routine after the first morning": login by 09:00 + `restart desk` + `/status`; the ratchet plan at 09:05 (`make twt-plan … SOURCE=MORNING`) with the three things to read on every `RAISE_GTT_STOP` line; confirm, exits first; the 15:15 sweep expecting `naked: 0`. Plus what the evening does by itself and what to watch over the first year (137 of 164 exits were the trail; `trail_pct` is floored at 18 % because tightening it is the measured cliff).

- [x] G6: The half-size rule for the first ten live **entries** is stated, with why it is entries
      and not sessions — this book enters about eighteen times a year, so ten sessions would be a
      fortnight and ten entries is most of a year's first cohort.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -ciE "half size|half-size" docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*[1-9]/
  EVIDENCE: CHECK run 11 Sep 2026 → `8`. §5.1 carries the arithmetic (slot ₹2,50,000 → half size ₹1,25,000 at ₹25,00,000 over ten slots) and §5.5 condition 2 makes an oversized line a stop-and-do-not-confirm. §11.1 is the *why*: the swing book and VBT-1 count sessions because they enter most days, **this book enters about eighteen times a year**, so a five-session allowance would be spent by one quiet week and would have protected nothing; ten entries is most of a first year's cohort. It also records that the counter is decremented once per **filled** entry by the session that filled it, that it is a counter and not a flag, and that a `DRY_RUN` plan is full size.

- [x] G7: **The document sets nothing.** (Gate TIGHTENED 11 Sep 2026 after it passed: it expected
      `[0-9]+`, which any count satisfies, so it could not fail. It now expects exactly 0. A gate
      that cannot fail is not a gate — the same lesson `command-center.ts` records about a
      fallback no test could reach.) It contains no instruction an agent could mistake for
      one to execute, and it does not itself flip a flag or set a capital.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -cE "BASKFY_TWT_EXECUTION_ENABLED=true" docs/twt/FIRST-LIVE-MORNING.md
  EXPECT: /^\s*0\s*$/
  EVIDENCE: CHECK run 11 Sep 2026 → `0`. The literal assignment appears nowhere in the file. §3.4 names the key, names the two files on the box (`/opt/baskfy/.env.staging.compose` for the desk, `/opt/baskfy/.env.staging` for api/worker/beat), and says "change its value from `false` to the opposite" — a human instruction that is not a copy-pasteable line and not a command. §3.4 also carries the swing book's finding that the flag alone changes nothing while `BASKFY_DESK_DRY_RUN` is true, and that setting that one takes the **weekly book** live on the same desk. §12 restates the six things this document does not authorise; §0 tells any agent reading the file to run nothing in it.

- [x] G8: `NEEDS-MAULIK.md` § TWT names the three things only his hands supply: the daily Kite
      login, the flag flip and the capital setting, and the plant's missing instrument-days.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -A 30 -E "^#+ .*TWT" NEEDS-MAULIK.md | grep -ciE "kite login|flag|capital|instrument-days"
  EXPECT: /^\s*[3-9]|^\s*[1-9][0-9]/
  EVIDENCE: CHECK run 11 Sep 2026 → `5` (was `2` before this module: "capital" and "instrument-days" sat outside the 30-line window). `NEEDS-MAULIK.md` § TWT now opens with a four-row table naming T1 the daily Kite login, T2 the execution flag, T3 the sleeve's capital and T4 the plant's missing instrument-days, followed by a link to `docs/twt/FIRST-LIVE-MORNING.md` saying which step of the runbook each of T1–T3 is. The four full entries below it are unchanged.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
