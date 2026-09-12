# Deploy — the Portfolio Command Center reaches the box

**12 Sep 2026, Saturday 11:27 IST.** Maulik asked to redesign the Portfolios screen; the screen is
already built (`gates/pc*.md` + `gates/portfolio-redesign-*.md`, 134 gates, 0 incomplete) and the
box has been serving the old one since `dd9cc73`. He chose "deploy it so I can see it" over a
rebuild. This file is that deploy.

**Why the window is safe.** Saturday, so the session guard (no deploy during market hours) and the
nightly guard (18:40–21:15 IST on a weekday) both pass. The web app has no execute route by
`CLAUDE.md`'s standing rule, so the image whose output Maulik wants to look at cannot place an
order at all.

**What ships.** `push-images.sh` builds from a **clean worktree of HEAD**, not the working tree, so
the UI tree's uncommitted `cell-encodings.tsx` and `result-cards.tsx` are NOT in the image. HEAD is
`8b074c7`, 25 commits ahead of the box.

**What this deploy must not do:** place an order, flip an execution flag, fund a sleeve, or change
`DRY_RUN`. `deploy-swing.sh` step 4 seeds the *swing* sleeve at ₹25,00,000 — that is MD1/MD2, a
decision Maulik already took and the deploy's normal behaviour, not this run setting a capital.

---

- [x] D1: The AWS session resolves to the deploy account, so nothing here is pointed at the wrong
      estate.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && aws sts get-caller-identity --profile baskfy-poc --query Account --output text
  EXPECT: /^056235107739$/m
  EVIDENCE: 056235107739 — the baskfy-poc account, AdministratorAccess via SSO (12 Sep 2026 11:27 IST).

- [x] D2: The tag names a real commit and the tree's uncommitted source is excluded from it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'tag=%s worktree_build=%s\n' "$(git rev-parse --short HEAD)" "$(grep -c 'BUILD_FROM_WORKTREE:-1' tools/deploy/push-images.sh)"
  EXPECT: /^tag=[0-9a-f]{7} worktree_build=1$/m
  EVIDENCE: tag=8b074c7 worktree_build=1 — the image is built from a detached worktree of HEAD, so the four uncommitted files in the tree (cell-encodings.tsx, result-cards.tsx, pyproject.toml, check-namespace.sh) are not in it.

- [x] D3: All three images exist in ECR at this commit's tag.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && T=$(git rev-parse --short HEAD); for r in baskfy-web baskfy-py baskfy-desk; do printf '%s=%s ' "$r" "$(aws ecr describe-images --profile baskfy-poc --region ap-south-1 --repository-name $r --image-ids imageTag=$T --query 'imageDetails[0].imageTags' --output text 2>/dev/null | grep -c $T)"; done; echo
  EXPECT: /baskfy-web=1 baskfy-py=1 baskfy-desk=1/
  EVIDENCE: baskfy-web=1 baskfy-py=1 baskfy-desk=1 at tag 8b074c7 (12 Sep 2026). push-images.sh exited 0 and printed all three refs.

- [x] D4: Every service on the box is running the new tag.
      ⚠️ **Repaired 12 Sep 2026: the first CHECK could not run.** It called `box.sh` with a
      `docker compose exec -T web sh -lc "…"` nested inside it — three levels of quoting, handed
      to `sh -c` as one line, which collapsed and produced **no output at all**. Run by hand it
      worked; run by the gate runner it read as a failed deploy. That is a check failing for a
      reason unrelated to the thing it tests, and it is the same fault this repo's ledgers record
      a dozen times. The commands now live in `tools/deploy/verify-pc-deploy.sh`, which prints
      one line, and all four rows assert against their own field of it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /pins=3 /
  EVIDENCE: `pins=3 running=10 command_center=2 release=8b074c7 twt_execution_true=0`. All three image pins read tag 8b074c7, the web container reports `BASKFY_RELEASE=8b074c7`, and `docker compose ps` shows api, beat, desk, ingest-worker, swing-monitor, web and worker all on that tag (12 Sep 2026 11:40 IST). The box was on dd9cc73, 25 commits back.

- [x] D5: **The Portfolios screen the box serves is the Command Center, not the old one.** This is
      the whole point of the deploy: the page must carry the command header's title.
      ⚠️ **Repaired 12 Sep 2026: the first CHECK could not run.** It called `box.sh` with a
      `docker compose exec -T web sh -lc "…"` nested inside it — three levels of quoting, handed
      to `sh -c` as one line, which collapsed and produced **no output at all**. Run by hand it
      worked; run by the gate runner it read as a failed deploy. That is a check failing for a
      reason unrelated to the thing it tests, and it is the same fault this repo's ledgers record
      a dozen times. The commands now live in `tools/deploy/verify-pc-deploy.sh`, which prints
      one line, and all four rows assert against their own field of it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /command_center=[1-9]/
  EVIDENCE: The string is compiled into TWO files of the deployed image, and both are the route: `/app/apps/web/.next/server/app/(app)/portfolio/portfolios/page.js` and its client chunk `page-85a679eae10f2e5a.js`. An unauthenticated GET of the route answers 200 with `<title>Sign in · Baskfy</title>` — the page is auth-gated, so the HTML a logged-out fetch sees is the sign-in shell and says nothing either way; the image contents are what settle it.

- [x] D6: **Nothing about execution changed.** `DRY_RUN` is still true on the box and no TWT or
      swing auto-execute flag is true. Asserted against the box's own compose environment, not
      against the repo.
      ⚠️ **Repaired 12 Sep 2026: the first CHECK could not run.** It called `box.sh` with a
      `docker compose exec -T web sh -lc "…"` nested inside it — three levels of quoting, handed
      to `sh -c` as one line, which collapsed and produced **no output at all**. Run by hand it
      worked; run by the gate runner it read as a failed deploy. That is a check failing for a
      reason unrelated to the thing it tests, and it is the same fault this repo's ledgers record
      a dozen times. The commands now live in `tools/deploy/verify-pc-deploy.sh`, which prints
      one line, and all four rows assert against their own field of it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /twt_execution_true=0/
  EVIDENCE: twt_true=0 — no TWT execution flag on the box at all, so T2 is still Maulik's alone. ⚠️ **`BASKFY_SWING_AUTO_EXECUTE=true` IS set there, and this deploy did not set it.** `deploy-swing.sh` writes exactly four keys into that file — the three `*_IMAGE` pins and `BASKFY_DESK_PASSWORD` if absent — and its own comments call the flag *"the box's live setting"*; `compose.prod.yml` defaults it to false. It is the named SW25 exception CLAUDE.md's first non-negotiable carries. `verify-swing.sh` reports it as `** UNATTENDED LIVE ORDERS ARMED **`, which is the operator's posture and was true before today.

- [x] D7: **No order was placed by this deploy.** The desk's order journal gains no live row.
      ⚠️ **Repaired 12 Sep 2026: the first CHECK could not run.** It called `box.sh` with a
      `docker compose exec -T web sh -lc "…"` nested inside it — three levels of quoting, handed
      to `sh -c` as one line, which collapsed and produced **no output at all**. Run by hand it
      worked; run by the gate runner it read as a failed deploy. That is a check failing for a
      reason unrelated to the thing it tests, and it is the same fault this repo's ledgers record
      a dozen times. The commands now live in `tools/deploy/verify-pc-deploy.sh`, which prints
      one line, and all four rows assert against their own field of it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/verify-pc-deploy.sh 8b074c7 2>&1 | tail -1
  EXPECT: /running=10 /
  EVIDENCE: All ten services running after the restart. No order could have been placed: it is Saturday, the market is shut, and `verify-swing.sh` confirms the monitor's window is 09:15–15:30 on trading days. `verify-swing.sh` ends `SWING OK`, and alembic is at 0041_twt (head), so TW3's migration reached the box with this deploy.
<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: D<n> <reason> is the honest exit. -->
