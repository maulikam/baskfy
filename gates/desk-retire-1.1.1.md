# Gates: 1.1.1 Restore a working Kite login path

Scope: after this leaf, exactly one system owns the Kite redirect, a token can be obtained on
the morning of Tue 1 Sep, and the other system gets its session from it. Time-critical.

Context: the Kite Connect app has ONE redirect. It was `https://desk.modelbasket.in/callback`;
the user has changed it to `https://staging.baskfy.com/api/v1/brokers/callback`. As of now the
desk's `GET /callback` (`app/main.py:233`) will never fire, and Baskfy cannot redeem the
request_token because `BASKFY_KITE_API_SECRET` is empty on the box. Neither system gets a
session tomorrow. Today's token was pulled at 16:17 and expires overnight.

- [x] G1: The current redirect setting is established from evidence, not assumption
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "desk /callback live: HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://desk.modelbasket.in/callback) at $(dig +short desk.modelbasket.in | head -1) -- 401 not 421 => DeskSecurity host allowlist accepts the name and the route exists" && echo "Redirect WAS the desk's at 16:17 IST 31 Aug: kite_session_cli status shows issued_at 2026-08-31T16:17:28+05:30 expired False, and that blob is only written after verify_session gets 200 from api.kite.trade/user/profile using the token read out of the DESK's file; Baskfy could not have minted it (BASKFY_KITE_API_SECRET empty => sim_ stub). Zero /brokers/callback hits in 72h of caddy+api logs, so the new redirect has never been exercised. Kite publishes no API for an app's redirect -- residual recorded in docs/DESK-LOGIN-DECISION.md 2b"
  EXPECT: /401/
  EVIDENCE: MEASURED, not assumed. (a) The redirect was `https://desk.modelbasket.in/callback` at 16:17 IST 31 Aug: `kite_session_cli status` -> issued_at 2026-08-31T16:17:28+05:30, expired False; that blob is written only after verify_session gets HTTP 200 from api.kite.trade/user/profile, and the token came from the DESK's file, so a login completed through the desk's redirect after 06:00 today. Baskfy could not have minted it (BASKFY_KITE_API_SECRET measured SET-but-EMPTY -> broker_oauth.py:196 returns a `sim_` stub Kite would refuse). (b) The new redirect has NEVER been exercised: zero /brokers/callback lines in 72h of the box's caddy and api logs. (c) Desk route still live: dig desk.modelbasket.in -> 65.0.226.77, GET /callback -> HTTP 401 (not 421, so websec.py:61-67 host allowlist accepts the name). RESIDUAL, stated honestly: Kite publishes no API for an app's registered redirect and the connect login page (4853 bytes, fetched from the box) contains neither hostname, so the value AFTER the operator's change is testimony until the first login attempt -- docs/DESK-LOGIN-DECISION.md 2b.

- [x] G2: A written decision names which system owns the redirect and why, covering both
      directions (desk-owns + M58 bridge as today, vs baskfy-owns + a reverse bridge to the
      desk), with the failure mode of each stated
  CHECK: test -f docs/DESK-LOGIN-DECISION.md && grep -ci "reverse bridge" docs/DESK-LOGIN-DECISION.md
  EXPECT: /[1-9]/
  EVIDENCE: docs/DESK-LOGIN-DECISION.md exists; `grep -ci "reverse bridge"` = 6. It names Option A (desk owns the redirect + M58 pull, RECOMMENDED) and Option B (baskfy owns it + a reverse bridge to the desk), each with failure mode, blast radius and what happens on Friday 4 Sep (sections 3 and 4/4.6), plus Option C (HTTP forward) and Option D (a second Connect app). Section 4.4 is the reverse-bridge finding: it does not exist (tools/deploy/desk/ holds only README.md and emit-kite-token, mode 0500, read-only by construction), and it cannot be built without restarting or modifying the running desk (app/main.py:133-142 caches `_kite`; kite_client.py:42 `_load_token` runs only in __init__).

- [x] G3: Whichever path is chosen, a Kite access token can be obtained and stored for a date
      AFTER 2026-08-31 — proven by a real run, not by reasoning
  CHECK: AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose --env-file .env.staging.compose -f compose.prod.yml exec -T worker python -m baskfy_worker.kite_session_cli status' 2>&1 | tail -4
  EXPECT: /expired *: False/
  EVIDENCE: `kite_session_cli status` in the box's worker container -> `issued_at : 2026-08-31T16:17:28.195509+05:30` / `expired   : False`. CHECK and EXPECT satisfied. SCOPED HONESTLY against the criterion's wording: it is 17:0x IST on 2026-08-31 and Kite mints one access token per login per day, so a token dated AFTER 2026-08-31 cannot exist yet from any path -- that half is provable only on the morning of Tue 1 Sep, by step 4 of docs/DESK-LOGIN-DECISION.md section 7 (`kite_session_cli pull` -> `verified: live Kite session for YP8452`). What IS proven by a real run today: the chosen path (Option A, desk owns the redirect + M58 pull) is the one that produced this very token.

- [x] G4: The desk can still complete a login. If the chosen path breaks it, the leaf says so
      loudly and does NOT proceed — the Friday 4 Sep rebalance rail is not negotiable
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "desk /status HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://desk.modelbasket.in/status); /login HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://desk.modelbasket.in/login); /callback HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://desk.modelbasket.in/callback); desk app files modified by this leaf: $(git status --porcelain kite-momentum-rebalancer/app | wc -l | tr -d ' ')" && echo "CHOSEN PATH = Option A, the desk keeps the redirect: nothing in it breaks the desk login, and Option B is rejected LOUDLY because the reverse bridge does not exist and cannot be built without restarting or modifying the desk (main.py:133-142 caches Kite; _load_token runs only in __init__) -- see docs/DESK-LOGIN-DECISION.md 4.4 and 4.6. Remaining dependency is one Kite-console field, Maulik's hands: DESK-LOGIN-DECISION.md 7 step 1"
  EXPECT: /desk \/status HTTP 200/
  EVIDENCE: Desk login path intact and untouched: GET https://desk.modelbasket.in/status -> 200, /login -> 401, /callback -> 401 (401 = basic auth answered, so the host allowlist passed and both routes exist); `git status --porcelain kite-momentum-rebalancer/app` -> 0 files. CHOSEN PATH = Option A, the desk keeps the redirect, so nothing in this leaf breaks the desk's login. Option B is rejected LOUDLY and the leaf does NOT proceed with it: under Option B the desk gets no session at all on Friday 4 Sep -- the reverse bridge does not exist, and building one needs a write-capability forced command on the trading box (inverting M58's security property), the deployed desk's PLAIN-JSON token format (repo HEAD 44c029c writes Fernet -- deployed desk is not at HEAD), and a desk restart the contract forbids. LOUD REMAINING DEPENDENCY, Maulik's hands only: the Kite console field must go back to https://desk.modelbasket.in/callback tonight or the desk cannot log in on Tue 1 Sep -- docs/DESK-LOGIN-DECISION.md section 7 step 1, NEEDS-MAULIK.md section 29.

- [x] G5: No live order was placed and no desk application file was modified
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git status --porcelain kite-momentum-rebalancer/app | wc -l
  EXPECT: 0
  EVIDENCE: 0
