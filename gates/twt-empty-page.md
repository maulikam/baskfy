# Why the TWT page is empty — the honest half, and the two things that are genuinely wrong

**12 Sep 2026.** Maulik looked at the three-weeks-tight page on the freshly deployed box and asked
whether anything is missing. Most of what he saw is the page telling the truth. Two things are not.

## What is NOT wrong

The detector has never run on that box, so there is nothing for the page to show.

| | Measured on the box, 12 Sep 2026 |
|---|---|
| `tw_breadth_daily` · `tw_signal_daily` · `tw_state_daily` | **0 rows each** |
| `tw_position` · `tw_plan` | **0 rows each** |
| Last `pipeline_run` | **2026-09-11** — before the deploy that carried TWT |

`baskfy.twt.detect` is scheduled `21:00 mon-fri` and the nightly chain `18:15 mon-fri`. Today is
**Saturday**, so neither runs tonight. The earliest this page can say anything is **Monday**.
"Not read yet" is therefore correct, and an empty open-positions list is the ordinary state the
page already describes.

## What IS wrong

**1. One sentence on the page is false.** With nothing read at all, `tight-names.tsx` renders *"No
name held the pattern on this session"* — which asserts a session was read and nothing matched.
No session has been read. `gate-card.tsx` gets the same distinction right two panels above
(`state === null ? "Not read yet"`), and `TightNames` already receives `session: string | null`
and ignores it in the empty branch. So the page contradicts itself about the same absence.

**2. The sleeve has no configuration row on the box.** `sw_config=1`, `vb_config=1`,
**`tw_config=0`**. `deploy-swing.sh` step 4 seeds `reference` and `swing` and nothing else, so
nothing in the deploy path has ever created a `tw_config` row. Creation is `seed_twt_config`'s job
and it writes ₹0, which is the safety rail working — but the row has to exist before the sleeve
can be configured or funded at all.

---

- [x] E1: The empty tight-names panel distinguishes "no session has been read" from "a session was
      read and no name held the pattern". The two are different facts and the page must not state
      the second when the first is true.
      ⚠️ **The first version of this CHECK answered `false` against correct code.** It searched
      only the 600 characters after the `entries.length === 0` branch, and the comment explaining
      the fix pushed `session === null` to 649. A window that a comment can overflow is not a check.
      It now asserts both testids exist and that the branch is on `session`, with no window at all.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && node -e "const s=require('fs').readFileSync('src/components/twt/tight-names.tsx','utf8'); console.log('branch=' + /entries\.length === 0 \?[\s\S]{0,1200}?session === null \?/.test(s), 'unread=' + s.includes('twt-tight-unread'), 'empty=' + s.includes('twt-tight-empty'))"
  EXPECT: /^branch=true unread=true empty=true$/m
  EVIDENCE: branch=true unread=true empty=true — the empty branch now tests `session === null` first and renders `twt-tight-unread` ("No session has been read for this strategy yet") instead of a claim about what the pattern did.

- [x] E2: A test pins both branches, so the distinction cannot regress into one sentence again.
      ⚠️ **`tail -3` was the wrong window**: vitest prints "Tests N passed" and then two timing
      lines, so the last three lines never contain the count. Grepping the line it wants cannot
      drift as the summary grows. `| grep -E` also anchors on "Tests" so a "Test Files" line cannot
      satisfy it by accident.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/twt/__tests__ 2>&1 | grep -E "^ +Tests +" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed/
  EVIDENCE: Tests 54 passed across 5 files (12 Sep 2026), including the three new ones in `tight-names-empty.test.tsx`: the unread copy must not contain "held the pattern on this session", the read-but-empty copy must, and the two testids never render together.

- [x] E3: The deploy creates the sleeve's config row, as it already does for the swing book. ₹0,
      because creation is not funding.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "baskfy_api.seed twt" tools/deploy/deploy-swing.sh
  EXPECT: /^[1-9]$/m
  EVIDENCE: 1 — `deploy-swing.sh` step 4 now runs `seed twt` beside `seed reference` and `seed swing`. **No `--capital`**: the swing line carries MD1/MD2 because those are decided numbers; TWT's capital is T3 and stays Maulik's keystroke. `bash -n` clean.

- [x] E4: **The box has a `tw_config` row and it is ₹0.** Fixing the script proves nothing until
      the row exists where Maulik is looking.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select count(*)||':'||coalesce(max(sleeve_capital_inr)::text,'none') from tw_config" 2>&1 | tail -1
  EXPECT: /^1:0\.00$/m
  EVIDENCE: 1:0.00 — the row exists on the box and is ₹0. Full row: `0.00 | slots=10 | pos%=12.50 | stop=20.00 | trail=20.00 | first_live=10 | by=seed`, which is `docs/twt/04`'s defaults exactly. Ran `seed twt` against the box directly because the script fix cannot retro-create a row on an already-deployed host.

- [x] E5: The web suite and lint are clean after the copy change.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit 2>&1 | tail -2 && pnpm exec eslint . 2>&1 | tail -2
  EXPECT: /0 errors/
  EVIDENCE: tsc clean; eslint 0 errors, 1 warning — the pre-existing react-hooks/incompatible-library note on data-table.tsx, untouched by this change.

- [x] E6: **Nothing was funded and no flag moved.** Seeding creates a row at zero; it must not set
      a capital or enable execution.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /running=10 .*twt_execution_true=0/
  EVIDENCE: pins=3 running=10 command_center=2 release=8b074c7 twt_execution_true=0 — ten services up, no TWT execution flag on the box, and the seeded capital is 0.00. Creating a row is not funding a sleeve.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: E<n> <reason> is the honest exit. -->
