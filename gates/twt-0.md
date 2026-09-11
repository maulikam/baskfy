# TW0 — the docs pack, before any code

**Goal (from `docs/twt/06`):** a fresh session can build this sleeve without reading the research
code, and cannot accidentally build a different one. The pack is written in the shape of
`docs/swing/` and `docs/vbt/`, from `research/tight-close/STRATEGY.md`.

---

- [x] G0.1: `docs/twt/` holds the ten files the pack promises: README, 01-method,
      02-scope-and-gating, 03-data-model, 04-business-rules, 05-ui-spec, 06-module-plan,
      STATUS.md, DECISIONS-TW.md, QUESTIONS.md.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && ls docs/twt/ | sort | tr '\n' ' '
  EXPECT: /01-method.md 02-scope-and-gating.md 03-data-model.md 04-business-rules.md 05-ui-spec.md 06-module-plan.md DECISIONS-TW.md QUESTIONS.md README.md STATUS.md/
  EVIDENCE: 01-method.md 02-scope-and-gating.md 03-data-model.md 04-business-rules.md 05-ui-spec.md 06-module-plan.md DECISIONS-TW.md QUESTIONS.md README.md STATUS.md

- [x] G0.2: **Every threshold of `04` carries its value**, and the eleven the kickoff names are
      all present with the numbers it gives: ₹30 close floor, 10,000 volume SMA, 3.01 % tight
      band, 1.3 × the month-3 low, 5 sessions out, ₹5 crore turnover, 40 % breadth, 20 % stop,
      20 % trail, 10 slots, 3 new a session, 1 % of turnover, 25 bps a side.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node -e "const s=require('fs').readFileSync('docs/twt/04-business-rules.md','utf8');const need=['min_close_raw','min_vol_sma','tight_band_pct','month_low_multiple','entry_min_sessions_out','min_turnover_inr','min_pct_above_dma','stop_pct','trail_pct','max_slots','max_new_entries_per_session','max_position_vs_turnover','cost_bps_per_side'];const miss=need.filter(n=>!s.includes(n));console.log(miss.length?'MISSING '+miss.join(','):'all 13 named')"
  EXPECT: /all 13 named/
  EVIDENCE: all 13 named

- [x] G0.3: The real-money gate in `02` §3 is **Maulik's 11 Sep decision**, not a DRY_RUN session
      count: a green tree, a written runbook, his own flag flip, and half size for the first ten
      live entries. No session-count condition survives as a gate.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node -e "const s=require('fs').readFileSync('docs/twt/02-scope-and-gating.md','utf8');const g=s.slice(s.indexOf('## §3'));console.log('no-paper='+/no paper phase|withdrawn|struck|DRY_RUN_SESSIONS_REQUIRED\D{0,20}0/i.test(g),'halfsize='+/first ten live entries/i.test(g),'flagflip='+/his own hand|by his hand|Maulik's hand|flips? (it|the flag) himself/i.test(g),'runbook='+/FIRST-LIVE-MORNING/.test(g))"
  EXPECT: /no-paper=true halfsize=true flagflip=true runbook=true/
  EVIDENCE: no-paper=true halfsize=true flagflip=true runbook=true

- [x] G0.4: `03` names the next free migration number, and it really is free
      (`alembic heads` is single and is the one `03` chains onto).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && ls decile-blueprint/services/api/alembic/versions/ | grep -c '^0041' ; grep -o '0041_twt' docs/twt/03-data-model.md | head -1
  EXPECT: /^0\n0041_twt$/
  EVIDENCE: `1` and `0041_twt`. **The first half no longer reads 0, and that is correct rather than a
      failure:** TW3 shipped `0041_twt.py` (`a2bf343`) and consumed the number `03` reserved. The
      gate asked whether the number was free *at the time the pack was written*, and it was. Left
      as a record rather than rewritten to pass — a gate that is re-aimed after the fact proves
      nothing.

- [x] G0.5: `06` plans TW1–TW10, each with a Goal and acceptance criteria that are tests.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node -e "const s=require('fs').readFileSync('docs/twt/06-module-plan.md','utf8');const mods=[...s.matchAll(/^### TW(\d+)/gm)].map(m=>+m[1]);const goals=(s.match(/\*\*Goal:\*\*/g)||[]).length;const acs=(s.match(/\*\*AC:\*\*/g)||[]).length;console.log('modules='+mods.join(',')+' goals='+goals+' acs='+acs)"
  EXPECT: /modules=0,1,2,3,4,5,6,7,8,9,10 goals=11 acs=11/
  EVIDENCE: modules=0,1,2,3,4,5,6,7,8,9,10 goals=11 acs=11

- [x] G0.6: `QUESTIONS.md` carries the four defaults only Maulik can confirm — ₹25 lakh, equal
      weight, ten slots, half size for the first ten live entries — each with the default the run
      builds against.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node -e "const s=require('fs').readFileSync('docs/twt/QUESTIONS.md','utf8');console.log(['25,00,000','equal','10 slots','first ten'].map(k=>k+'='+s.toLowerCase().includes(k.toLowerCase())).join(' '))"
  EXPECT: /25,00,000=true equal=true 10 slots=true first ten=true/
  EVIDENCE: 25,00,000=true equal=true 10 slots=true first ten=true

- [x] G0.7: `NEEDS-MAULIK.md` has a **TWT** heading naming the three things known already: the
      daily Kite login, the flag flip and the capital setting, and the plant's missing
      instrument-days.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node -e "const s=require('fs').readFileSync('NEEDS-MAULIK.md','utf8');const i=s.indexOf('TWT —');const t=i<0?'':s.slice(i,i+9000);console.log('heading='+(i>=0),'login='+/Kite login/i.test(t),'flag='+/BASKFY_TWT_EXECUTION_ENABLED/.test(t),'capital='+/sleeve_capital_inr|₹25/.test(t),'bars='+/instrument-day|missing bar/i.test(t))"
  EXPECT: /heading=true login=true flag=true capital=true bars=true/
  EVIDENCE: heading=true login=true flag=true capital=true bars=true

- [x] G0.8: The pack never claims the sleeve has traded, never sets a capital, and names no flag
      as true.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rniE "BASKFY_TWT_[A-Z_]*\s*=\s*true" docs/twt/ | grep -viE "may only be set|only when|never|flip|would" | wc -l | tr -d ' '
  EXPECT: /^0$/
  EVIDENCE: `0`. No document in the pack sets any `BASKFY_TWT_*` flag true. `FIRST-LIVE-MORNING.md`
      tells Maulik to *"change its value to the opposite"* rather than writing the literal, so the
      runbook cannot be mistaken for a script.

- [x] G0.9: The commit exists: `TW0: green — …`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git log --oneline -20 --format=%s | grep -c '^TW0: green'
  EXPECT: /^1$/
  EVIDENCE: `1` — `5056aa1`, committed late and labelled as such. TW0 was marked ✅ on its own status
      page for hours with **no commit behind it**; the pack, both gate files and the research note
      the whole run reads as its spec existed only on one machine's disk.

- [ ] G0.10: Nothing outside `docs/twt/`, `gates/twt-*`, `NEEDS-MAULIK.md` and `docs/README.md`
      changed in the TW0 commit — TW0 writes no code.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git show --stat --format= $(git log --format='%H %s' -30 | grep '^\w* TW0: green' | head -1 | cut -d' ' -f1) | awk '{print $1}' | grep -vE '^(docs/twt/|gates/twt-|NEEDS-MAULIK.md|docs/README.md|$|[0-9]+)' | wc -l | tr -d ' '
  EXPECT: /^0$/
  ABANDON: G0.10 the commit includes `research/tight-close/`, which was untracked and which the
      committed golden fixtures are derived from. Scoped rather than met.
  EVIDENCE: **FAILS as written, deliberately.** The TW0 commit also carries `research/tight-close/` —
      the strategy note, the reference scanner, the verification, and `chartink_backtest.csv`,
      which is the answer key TW2's goldens score against. That directory was **untracked**, so
      committing the derived fixtures while leaving their source out of version control would have
      made those fixtures unreproducible. The gate's intent — *TW0 writes no code* — holds: nothing
      under `packages/`, `services/` or `apps/` is in it.
