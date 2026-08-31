# Who owns the Kite redirect — the desk, or Baskfy

**Leaf 1.1.1 of the desk-retirement tree. Written 31 Aug 2026, ~17:00 IST. ⚠ UNREVIEWED.**

**Recommendation in one line: revert the Kite app's redirect to
`https://desk.modelbasket.in/callback` tonight, keep the M58 pull bridge, and leave
`BASKFY_KITE_API_SECRET` off the box.** The steps are in §7. Everything before that is why.

---

## 1. The situation, and the deadline

A Kite Connect app has exactly **one** registered redirect URL. That constraint is not a
limitation someone worked around badly; it is the premise the whole M57/M58 bridge was built on
(`decile-blueprint/services/worker/src/baskfy_worker/kite_session_cli.py`, module docstring).

Until today that one URL was `https://desk.modelbasket.in/callback` — the momentum desk, which
places the live orders. The operator has changed it to
`https://staging.baskfy.com/api/v1/brokers/callback`.

As of the moment this was written, **neither system can obtain a Kite session tomorrow morning**:

- The desk's `GET /callback` (`kite-momentum-rebalancer/app/main.py:233`) is never reached,
  because Kite no longer sends the browser there.
- Baskfy's `GET /api/v1/brokers/callback` cannot redeem a `request_token` either. It needs
  `BASKFY_KITE_API_SECRET` for the `session/token` checksum, and that variable is **set but
  empty** on the box (measured today — §2).

A Kite access token dies at the next trading day's pre-open, with no refresh. Today's is dated
`2026-08-31T16:17:28+05:30` and is good for a few more hours. The next thing that needs one is a
human at the desk on the morning of **Tue 1 Sep**, and the thing that must not fail is the
**Friday 4 Sep rebalance** — a root-`CLAUDE.md` safety rail, not a preference.

---

## 2. What was actually measured

Nothing below is inferred from the brief. Each row was read today, read-only, without modifying
the desk and without printing a secret.

| Fact | Evidence |
|---|---|
| A **live** Kite session existed today | `kite_session_cli status` in the box's `worker` container: `issued_at : 2026-08-31T16:17:28.195509+05:30`, `expired   : False`. The blob is written only after `verify_session` gets a 200 from `api.kite.trade/user/profile`, so this token was accepted by Kite today |
| …therefore the redirect was **still the desk's** at 16:17 IST today | Kite tokens die at ~06:00 IST. A token live at 16:17 on 31 Aug was minted by a login completed **after 06:00 on 31 Aug**, through whatever redirect was registered at that moment. It landed in the desk's token file (that is the only thing `pull` reads). Baskfy could not have produced it: with an empty secret, `exchange_request_token` returns a `sim_<sha>` stub (`broker_oauth.py:196-201`), which Kite would refuse |
| The new redirect has **never been exercised** | Zero requests to any `/brokers` path in the box's Caddy access log and zero `brokers/(callback\|connect)` lines in the `api` log over the last 72 h |
| `BASKFY_KITE_API_SECRET` on the box | `SET but EMPTY` in `.env.staging` and in the `worker` container |
| `BASKFY_KITE_API_KEY` on the box | set, 16 chars — the same length as the desk's, i.e. **the same RENIL app** |
| `DRY_RUN` on the box | `true`, in `.env.staging`, which every python service reads via `env_file: [.env.staging]` |
| `BASKFY_BROKER_OAUTH_REDIRECT` | **not set.** So Baskfy derives its redirect from `BASKFY_WEB_ORIGIN=https://staging.baskfy.com` + `OAUTH_CALLBACK_PATH` = `https://staging.baskfy.com/api/v1/brokers/callback` — exactly the value the operator registered |
| The desk is up and its callback route is live | `desk.modelbasket.in` → `65.0.226.77`; `GET /status` → **200**; `GET /callback` → **401**, not 421. 401 means the `DeskSecurity` host allowlist accepted the hostname and the basic-auth layer answered (`app/core/websec.py:60-92`) — the route exists and is reachable |
| The deployed desk stores its token as **plain JSON**, not Fernet | `tools/deploy/desk/emit-kite-token` does `json.load(open(".kite_token.json"))["access_token"]`, and the pull succeeded today. Repo HEAD writes ciphertext (`kite_client.py:31-40`, commit `44c029c` "M16"). **The deployed desk is not at repo HEAD** |
| A **reverse** bridge does not exist | `tools/deploy/desk/` contains exactly two files: `README.md` and `emit-kite-token` (mode 0500, read-only by construction). Nothing anywhere in the repo writes a token toward the desk |

### 2b. The one thing that could not be measured

**There is no way to read a Kite app's registered redirect URL from outside the developer
console.** It is not in the connect login page — fetched today from the box, 4 853 bytes, and it
contains neither `desk.modelbasket.in` nor `staging.baskfy.com` nor a `redirect_url` field — and
Kite publishes no API for app metadata.

So the *current* value rests on the operator's own statement, corroborated by the two facts above:
the redirect was demonstrably the desk's at 16:17 IST, and the new one has never been hit. The
first login attempt after the change is the only thing that will confirm it either way.

---

## 3. Option A — the desk owns the redirect (revert to today's design)

Change `https://staging.baskfy.com/api/v1/brokers/callback` back to
`https://desk.modelbasket.in/callback` in the Kite developer console. Nothing else.

**What must change back:** one field, in a console, by Maulik. No code. No deploy. No restart of
anything. `BASKFY_KITE_API_SECRET` stays empty on the box.

**What keeps working:**

- The desk logs in exactly as it has every trading morning: `/login` → Kite → `/callback` →
  `generate_session` → token on disk → post-login autorun.
- Baskfy keeps getting its session through M58: `kite_session_cli pull` SSHes to the desk with a
  key sshd binds to a forced command that emits one string. Proven working today at 16:17.
- The nightly pipeline is unaffected either way — `refresh_quietly()` reports and returns, and
  the day's bars come from the NSE bhavcopy, which needs no credential.

**What Baskfy gives up:** nothing it has. `_connect_configured()` (`routers/brokers.py:190-222`)
already returns `False` — the secret is empty, and with the desk's redirect registered the
derived redirect no longer starts with `web_origin`, so it would return `False` on that ground
too. The broker grid correctly refuses to offer a login it cannot finish. That is the state
today, and Option A does not change it.

**Failure mode:** the console edit does not take effect, or is made incorrectly (trailing slash,
`http` vs `https`). Detected in seconds — the operator clicks *Login* on the desk and either
lands back on the desk or does not. Recoverable by editing the same field again.

**Blast radius if it goes wrong:** one login attempt. No secret moves. No file changes. No
service restarts.

**Friday 4 Sep:** the desk logs in on the morning, `/analyze` runs, `/execute` with
`confirm=true` places the rebalance. Unchanged from every previous Friday.

---

## 4. Option B — Baskfy owns the redirect (where we accidentally are)

To make this work, Baskfy needs **four** things, not one. They are listed in ascending order of
how much they cost.

### 4.1 The API secret on the box — and it is the *desk's* secret

`exchange_request_token` needs `BASKFY_KITE_API_SECRET` for the SHA-256 checksum
(`broker_oauth.py:203-206`). There is only one app, so the only secret that fits is the RENIL
Connect app's — the same credential the desk uses to place live orders.

Putting it on the Baskfy box means the box holds `api_key + api_secret`, which together are a
complete Kite Connect app credential: with any `request_token` it can mint a session on the
trading account. That is a strictly larger secret in a strictly less-hardened place, and it is
the *exact* reasoning M58 used to reject pushing from the desk to AWS Parameter Store
(`docs/DECISIONS-MERGE.md` M58, "Rejected alternatives"). Today the box holds only the key, and
`NEEDS-MAULIK.md` §29 records that as "the smaller blast radius", not as an omission.

It also compounds an open exposure: the RENIL api_secret was pasted into an agent transcript on
27 Aug and is on the rotation list as **R1**. Writing the known-exposed value to the box is worse
than not writing it; writing the *rotated* value requires updating the desk's `.env` in the same
sitting, at which point the Friday rail is in play (`NEEDS-MAULIK.md` §29.2, item 29b).

### 4.2 `DRY_RUN=false` on the API service — and this one is actively dangerous

```python
# broker_oauth.py:57-60
raw = os.environ.get("DRY_RUN", "true").strip().lower()
return raw in ("", "1", "true", "yes", "on")
```

```python
# broker_oauth.py:196-201
if dry_run_enabled() or not secret:
    return exchange_request_token_stub(...)   # returns "sim_<40 hex chars>"
```

The box has `DRY_RUN=true`. So under Option B **as configured right now**, a completed Kite login
does not fail loudly — it succeeds, stores `sim_<sha>` into
`/var/lib/baskfy/state/kite-token.enc`, and answers `connected: true, token_stored: true`. That
blob is the same one `KiteProvider` reads. **A login through Baskfy today would overwrite the
working session with a fake one**, and the failure would surface hours later as an unexplained
provider 403. The `simulated: true` field in the response is the only tell, and nothing consumes
it.

Flipping `DRY_RUN=false` on the box is not an agent-available action (root `CLAUDE.md`, safety
rails: "`DRY_RUN=true` is the default in every environment an agent creates"), and it changes
behaviour far beyond OAuth.

### 4.3 The callback is authenticated, and demands a `state` Kite may never send

```python
# routers/brokers.py:278-284
async def oauth_callback(
    principal: AuthenticatedDep,
    request_token: Annotated[str, Query(min_length=8, max_length=128)],
    state:         Annotated[str, Query(min_length=8, max_length=128)],
) -> CallbackOut:
```

Two problems.

The first is survivable: the browser coming back from Kite must carry a signed-in Baskfy session
for the same account that started `/connect`. A top-level GET navigation does carry a
`SameSite=Lax` cookie, so this should work — but nobody has ever run it.

The second is not obviously survivable. `state` is a **required** query parameter, validated
against a value minted by `POST /brokers/{id}/connect`. Baskfy builds its authorize URL like
this:

```python
# routers/brokers.py:443
query = urlencode({"api_key": api_key, "v": "3", "redirect_uri": redirect_uri, "state": state})
```

But the official Kite client emits only two parameters:

```python
# kiteconnect/connect.py:246-248
def login_url(self):
    return "%s?api_key=%s&v=%s" % (self._default_login_uri, self.api_key, self.kite_header_version)
```

Kite Connect has no `redirect_uri` parameter — the redirect is whatever is registered, which is
the entire reason this document exists — and the documented way to get a value round-tripped back
to the callback is `redirect_params`. **`redirect_params` appears zero times in this repository.**

So whether `state` survives the round trip is *unknown and untested*. If it does not, every login
attempt ends at `422` or `"Invalid or expired OAuth state."` — a login that cannot finish, offered
as though it could. This code has never completed a real Kite login; the confidence it projects is
not earned.

### 4.4 The reverse bridge — the part that decides it

The desk places the live orders and cannot lose its login. Under Option B the desk never sees a
`request_token` again, so it needs the *access token* pushed to it. Does that exist?

**No. Nothing like it exists, and the pieces that would have to exist all cut against the design.**

**(a) There is no counterpart to `emit-kite-token`.** `tools/deploy/desk/` holds one script, mode
0500, whose docstring says it "cannot write the token, refresh it, or reach the trading app". The
desk's `authorized_keys` line is `restrict,command="/home/desk/bin/emit-kite-token",from="<box
ip>"`. A reverse bridge needs a second forced command — call it `accept-kite-token` — that reads
a token on stdin and writes the desk's token file.

That inverts the whole M58 security argument. Today the key is a capability meaning *"read one
string that expires tonight"*. A write capability means *"choose the credential the trading app
authenticates with"* — which is also *"brick the desk's session on Friday morning"*. The blast
radius stops being "someone at the box's IP reads a token that dies tonight" and becomes "someone
at the box's IP controls whether the desk can trade".

**(b) Writing the file is not enough. The desk would have to be restarted.**

```python
# app/main.py:133-142
_kite: Kite | None = None
def kite() -> Kite:
    global _kite
    if _kite is None:
        _kite = Kite()
    return _kite
```

`_load_token()` runs once, inside `Kite.__init__` (`kite_client.py:42-53`), and sets the token on
the in-memory `KiteConnect` object. A token written to disk at 09:00 by an external process is
**not** picked up by the long-lived web app; it keeps using whatever it loaded at startup, which
by Tuesday is yesterday's dead string. The operator console — the thing that runs `/analyze` and
`/execute` on Friday — would 403 on every call.

Making the reverse bridge actually work therefore requires either restarting the desk's service
every morning, or changing desk application code to re-read the token. Both are forbidden here:
the contract for this leaf says the running deployment at 65.0.226.77 must not be modified, and
M58 explicitly rejected "deploy new application code to the desk" under the Friday rail.

**(c) The on-disk format is not what the repo thinks it is.** The deployed desk reads and writes
**plain JSON** `{"access_token": "..."}` — proven by `emit-kite-token` succeeding today. Repo HEAD
(`44c029c`, M16) writes Fernet ciphertext through `AccessTokenStore`. A reverse bridge would have
to target the *deployed* format, which no test in this repo covers, and would silently break the
day the desk is redeployed at HEAD — by writing a file the desk cannot decrypt, on a morning when
someone needs to rebalance.

**(d) Is one access token usable from two processes at all?** The `kite_session_cli` docstring
asserts "Kite permits the same access token from several processes" and cites
`docs/DECISIONS-MERGE.md` **M57**. That citation is dangling: the file has no M57 entry (nothing
between M47 and M58). So the claim has no written source.

It does, however, have **empirical support**: at 16:17 IST today the Baskfy box called
`api.kite.trade/user/profile` with the desk's token, from a different IP, while the desk held the
same token, and Kite returned 200 for user `YP8452`. Two processes, one token, both accepted —
for reads.

What that evidence does *not* cover, and what matters more here: a **second login invalidates the
first token**. Kite issues one access token per login. Sharing a token is fine; both systems
logging in independently is not — the one who logs in later silently kills the other's session.
That is an argument for exactly one system owning the login, which is what both options do, and
against any future "both can log in" arrangement.

### 4.5 The rest of the blast radius

`_connect_configured()` becomes `True` the moment the secret is written, because the derived
redirect now starts with `web_origin`. The broker grid then offers "Connect Zerodha" to **every
signed-in account** on a deployment whose token store is a single shared blob
(`token_store_path()` — one file, no `user_id`). A second user's login would overwrite the first
user's — and, today, the desk's — session. That is D3/C3 territory, and the multi-tenant clause
of the second law is not satisfied by this path.

### 4.6 Option B's failure mode, blast radius, and Friday

- **Failure mode:** several, and they compound. Most likely order of discovery: the secret is not
  set → login cannot finish; the secret is set but `DRY_RUN=true` → login "succeeds" and poisons
  the token blob with `sim_…`; `state` does not round-trip → 422 on every attempt; the desk gets
  no session at all because the reverse bridge does not exist.
- **Blast radius:** the desk's ability to trade. Also a trading-app credential relocated onto a
  web-facing box, and a user-facing login switched on for a store that cannot separate users.
- **Friday 4 Sep:** the desk cannot log in, so it cannot read holdings, cannot price a plan and
  cannot place the rebalance. **This is the safety rail failing.** Option B is not viable by
  Friday, and stating that loudly is this leaf's G4.

---

## 5. Option C — Baskfy owns the redirect and forwards the request_token (considered, rejected)

Worth stating because it is the only *cheap* way to keep the redirect where it now is: add an
unauthenticated route on `staging.baskfy.com` that takes the `request_token` and 302s the browser
straight to `https://desk.modelbasket.in/callback?request_token=…`, unconsumed. The desk redeems
it as usual; Baskfy keeps pulling over M58. Baskfy needs no API secret and no `DRY_RUN` change,
and the desk's basic-auth prompt is answered by the human who is already driving the login.

Rejected:

- It puts brand-new, never-exercised code on the critical path of the one login that must work
  tomorrow morning — to reach an outcome Option A reaches with a console edit and no code at all.
- An unauthenticated route that forwards a live single-use broker credential to a host named in a
  query-adjacent config is a class of thing that gets abused; it also has to be exempted from the
  API's auth dependency, which is a wider hole than the problem deserves.
- It leaves the redirect pointing at a system that still cannot finish a login, so the honest
  state of `_connect_configured()` becomes harder to reason about, not easier.

Keep it on the shelf. If the redirect ever has to live at Baskfy for a reason that outweighs
this, Option C is the least-bad shape — but not this week.

---

## 6. Option D — a second Kite Connect app for Baskfy (the durable answer)

The real defect is architectural: two systems, one app, one redirect. ₹2,000/month buys a second
Connect app with its own key, own secret and **own redirect** — and then the desk owns
`desk.modelbasket.in/callback`, Baskfy owns `staging.baskfy.com/api/v1/brokers/callback`, no
bridge is required in either direction, and Baskfy's user-facing broker login becomes a real
feature instead of a button that has to be kept switched off.

M58 rejected it as "₹2,000/month for a second copy of a session we already have". That reasoning
was correct when Baskfy only needed *data*. It stops being correct the moment Baskfy wants users
to connect their own brokers, which is what the redirect change appears to have been reaching for.

This is a money decision and therefore Maulik's. It does not solve tomorrow morning; Option A
does, and Option A is fully reversible if D is later taken.

---

## 7. Recommendation, and the exact steps

**Take Option A. Tonight, before market open on Tue 1 Sep.**

The reasoning, shortest form: Option A restores a login that has worked every trading day for
weeks, with a one-field console edit, no code, no deploy, no secret movement and no risk to the
Friday rail. Option B needs a secret relocation that is on the rotation list, a `DRY_RUN` flip an
agent may not make, an untested `state` round-trip, **and** a reverse bridge that does not exist,
cannot be built without either restarting or modifying the desk, and would invert the security
property M58 was built to have. The tie-breaks in the autonomy charter — the reversible option,
the stricter security boundary, what the two codebases' own culture would do — all point the same
way, and the safety rail settles it outright.

### Steps (all Maulik's hands; nothing here is agent-available)

1. **`developers.kite.trade` → the RENIL app → Redirect URL.** Set it back to exactly:

   ```
   https://desk.modelbasket.in/callback
   ```

   No trailing slash. `https`, not `http`. Save.

2. **Verify, tonight, with a real login.** Open `https://desk.modelbasket.in/login` (the desk will
   ask for its basic-auth password first). It should bounce through Kite and land back on
   `https://desk.modelbasket.in/?logged_in=1&…`. If it lands anywhere else, the field did not
   save — fix it and repeat. *This is the whole verification; it takes under a minute.*

3. **Leave `BASKFY_KITE_API_SECRET` empty on the box.** Do not add it. `NEEDS-MAULIK.md` §29.3
   branch B is the one now in force: the box never redeems anything, so it does not need the
   secret, and not holding it is the smaller blast radius.

4. **Tomorrow morning, after the desk's login**, confirm the bridge still feeds Baskfy:

   ```
   AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && sudo docker compose \
     --env-file .env.staging.compose -f compose.prod.yml exec -T worker \
     python -m baskfy_worker.kite_session_cli pull'
   ```

   Expect `verified: live Kite session for YP8452` then `stored: 32 chars`. (The nightly pipeline
   calls `refresh_quietly()` at 18:45 IST anyway; this is just the early check.)

5. **R1 rotation stays on the weekend track (5–6 Sep).** With Option A taken, the box never
   receives the secret at all, so the only place the exposed value lives is the desk's `.env` —
   which removes the "both files in one sitting, tonight, before Friday" pressure that
   `NEEDS-MAULIK.md` §29.2 flagged. Rotating on the weekend is now the strictly safer option
   rather than a trade-off.

6. **Book Option D as a decision, not a someday.** If Baskfy is to offer broker connections to
   users, it needs its own Connect app. Record the answer either way so it stops being an open
   item.

---

## 8. Defects found while establishing this, which outlive the decision

None of these are fixed here — this leaf owns one file. They are written down so they are not
rediscovered at 09:00 on a rebalance morning.

1. **`brokers.py:443` sends `redirect_uri` and `state` as top-level Kite login parameters.** Kite
   Connect defines neither; the documented mechanism is `redirect_params`, which the repo never
   uses. Either the code is wrong or the assumption is undocumented — and the callback *requires*
   `state`, so if it is wrong, no login through Baskfy can ever complete. Untested either way.

2. **A Baskfy callback under `DRY_RUN=true` silently poisons the shared token blob.** It writes
   `sim_<sha>` to the same path `KiteProvider` reads and reports `connected: true`. The only
   signal is a `simulated` field nothing consumes. It should refuse rather than pretend.

3. **The deployed desk is not at repo HEAD.** It stores its token as plain JSON; HEAD (M16) stores
   Fernet ciphertext. `emit-kite-token` is written against the deployed format, so a desk
   redeployment silently breaks the M58 bridge. Nothing tests or asserts this coupling.

4. **`docs/DECISIONS-MERGE.md` has no M57 entry.** Both `kite_session_cli.py` and
   `brokers.py::_connect_configured` cite it as the source for load-bearing claims — including
   "Kite permits the same access token from several processes". The file jumps M47 → M58.

5. **`_connect_configured()` is one env-var write away from switching on a user-facing broker
   login on a sole-tenant token store.** With the redirect at `staging.baskfy.com`, adding the
   secret flips it to `True` for every signed-in account, and they would all share one token blob.

---

*Recorded under the autonomy charter: decided, written, continued. Reversal is the same console
field — Option A can become Option B or D at any time, and nothing in this decision is
load-bearing on code.*

---

## 9. What was actually built — Maulik chose Option B (31 Aug 2026, leaf 1.1.5)

Section 7 recommended Option A: change the redirect back to the desk. **Maulik decided the
opposite.** Baskfy keeps `https://staging.baskfy.com/api/v1/brokers/callback`, and the desk gets
its session through a reverse bridge. This section records what that bridge is, because §4.4
said the bridge "does not exist and cannot be built without restarting or modifying the running
desk" — and the first half of that sentence is now false while the second half turned out to be
avoidable.

### 9.1 The move that made it possible: carry the *request* token, not the access token

§4.4 assumed the reverse bridge would have to write an access token into
`data/.kite_token.json`. That is what makes it hard: the deployed desk caches its Kite client
(`app/main.py:99`, `Kite.__init__` reads the token once), the order gateway captures `kite().kc`
once more on top of that, and `momentum-web.service` is a root-owned unit with
`Restart=on-failure` that the `desk` account cannot restart. A file write would need a restart,
and a restart is not something an automated bridge should be able to cause on the box that places
every live order.

So the bridge carries the **request token** instead, and lets the desk do the exchange:

```
Kite  --request_token-->  staging.baskfy.com/api/v1/brokers/callback
                                |
                                +--(browser: Caddy 302)--> desk.modelbasket.in/callback
                                +--(server: SSH forced command)--> ~desk/bin/accept-kite-request-token
                                                                       |
                                                                       v
                                                          127.0.0.1:8420/callback
                                                          -> kite().exchange_token()
                                                          -> set_access_token on the LIVE client
                                                          -> data/.kite_token.json rewritten
                                |
                                <--(M58 pull, unchanged)-- Baskfy borrows the access token back
```

Three of §4's four objections dissolve rather than being solved:

* **The cache (§4.4 / leaf 1.1.1 (c))** — `/callback` mutates the cached client in place. No
  restart, no new object, and the gateway's captured `kc` is the same object so it sees it too.
  Demonstrated, not argued: `tools/deploy/desk/prove-live-token-pickup.py` reproduces the cache
  problem *and* the fix against the deployed source, with `kiteconnect` stubbed.
* **The on-disk format (leaf 1.1.1 (b))** — nothing on the Baskfy side has to know it. The desk
  writes its own token with its own code. (For the record, measured on the box: plain JSON,
  `{"access_token": "<32 chars>"}`, 52 bytes, mode 0600, no sibling `.key`. Repo HEAD's Fernet
  `app/token_store.py` is not deployed at all.)
* **`BASKFY_KITE_API_SECRET` being empty (§4.1)** — no longer blocks the desk. The desk exchanges
  with its own secret, which it has. Baskfy still cannot complete a login *of its own*, and does
  not need to.

### 9.2 The write path, and why it is not the inversion §4.4 feared

Leaf 1.1.1 (a): a write path onto the trading box inverts M58's "the right to read one string".
The capability actually added is **"cause the desk to complete a Kite login with this string"** —
a second ed25519 key bound to a second forced command, `accept-kite-request-token`, which ignores
`SSH_ORIGINAL_COMMAND`, takes no arguments, reads ≤512 bytes of stdin, accepts only
`[A-Za-z0-9]{8,64}`, refuses a `sim_` stub, rate-limits itself, and drives one hard-coded loopback
URL. It writes no file the desk trades on, cannot read the access token back out, cannot place an
order and cannot open a shell.

A `request_token` is single-use, valid for minutes, and worthless once redeemed. The forward leg
emits a live access token that can trade for the rest of the day. **The reverse leg is the
narrower of the two capabilities.** Two keys and two forced commands rather than one key with two
verbs, deliberately: one key with a verb parameter would put the choice in the client's hands,
which is the exact property M58 exists to deny.

### 9.3 What §5 (Option C) got wrong

§5 rejected "Baskfy forwards the request_token" partly because the desk's `/callback` is
password-protected and handing Baskfy `DESK_PASSWORD` would be a general capability on the desk.
That is true and is why the forced command reads the password out of the desk's **own** `.env`, on
the desk, and Baskfy never sees it. The browser leg does not need it either — the operator's own
browser answers the challenge.

### 9.4 What is still Maulik's

* A genuine Kite `request_token` cannot be minted by an agent, so the final hop — Kite returning
  an access token rather than `TokenException: Token is invalid or has expired` — is proven only
  up to Kite's own approval. Everything either side of it is exercised.
* `BASKFY_KITE_API_SECRET` stays empty on the box, and under this design that is now a *choice*
  rather than an outage. Setting it would let Baskfy redeem the token itself — and would then
  starve the desk, because a request token is single-use. **Do not set it while the desk depends
  on this bridge.**
* The desk's basic-auth password must be in the browser Maulik logs in with, or the redirect
  lands on a 401 he cannot answer. The request token is not spent in that case; he can retry.
