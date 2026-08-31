# The desk side of the Kite session bridge

Four things live on or near the momentum desk (`desk@65.0.226.77`) so that one Kite Connect app,
with its one registered redirect, can serve both Baskfy and the desk. **None of them is
application code** — the trading app is untouched, which is the root `CLAUDE.md` safety rail "the
desk must be able to rebalance on any Friday" taken literally.

The bridge has two legs, and they run in opposite directions:

| | direction | carries | added |
|---|---|---|---|
| **forward (M58)** | Baskfy → desk | "give me today's **access** token" | 30 Aug 2026 |
| **reverse (1.1.5)** | Baskfy → desk | "here is a **request** token, go and log in" | 31 Aug 2026 |

The reverse leg exists because Maulik moved the Kite app's redirect to
`https://staging.baskfy.com/api/v1/brokers/callback` on 31 Aug 2026. The `request_token` now
lands at Baskfy, and a Kite request token is single-use — so exactly one of the two systems can
redeem it. It has to be the desk. That is a fact about the deployed desk rather than a
preference; see "Why the desk must be the one that exchanges" below.

---

## 1. `emit-kite-token` → `~desk/bin/emit-kite-token`, mode 0500 — the forward leg

Reads one field out of `data/.kite_token.json` and writes it to stdout. It cannot write the token,
refresh it, or reach the trading app.

## 2. `accept-kite-request-token` → `~desk/bin/accept-kite-request-token`, mode 0500 — the reverse leg

Reads a Kite `request_token` on stdin and drives the desk's **own** `/callback` on loopback with
it. The desk then exchanges it with its own `KITE_API_SECRET`, stores the access token with its
own code, and — this is the point — updates the Kite client the running uvicorn process is
already holding.

What the capability *is*, exactly: **"cause the desk to complete a Kite login with this string."**

* ignores `SSH_ORIGINAL_COMMAND` completely; takes no arguments;
* reads at most 512 bytes of stdin and accepts only `[A-Za-z0-9]{8,64}`;
* refuses a `sim_`-prefixed value outright, so a simulated token from a DRY_RUN Baskfy callback
  can never be relayed to a box that trades for real (the defect class leaf 1.1.4 fixed);
* speaks to exactly one hard-coded loopback URL, `http://127.0.0.1:8420/callback`, using the
  desk's own `DESK_PASSWORD` read from the desk's own `.env` — the password never leaves the box
  and Baskfy never learns it;
* rate-limits itself to 20 handoffs an hour;
* writes no file the desk trades on. `/callback` writes the token, with the desk's code, in the
  desk's format;
* cannot read the access token back out, cannot place an order, cannot reach `portfolio.db`,
  cannot open a shell;
* verifies the resulting session against Kite's `user/profile` and **alarms if the desk's Kite
  account changed**, which is the one thing a stolen request_token could do that matters;
* appends one line per attempt to `~desk/logs/kite-handoff.log` — outcome, user id, a sha256
  prefix, never a secret.

Stdlib only, and `/usr/bin/python3` rather than the app's virtualenv, so it cannot import the
trading application even by accident.

**A request_token is the weakest credential in this system**: single-use, valid for minutes,
worthless once redeemed. The forward leg emits a live access token that can trade for the rest of
the day. The reverse leg is the *narrower* of the two capabilities, not a widening of the box's
attack surface — which was leaf 1.1.1's worry (a) about inverting M58's design.

## 3. Two lines in `~desk/.ssh/authorized_keys`

    restrict,command="/home/desk/bin/emit-kite-token",from="3.108.148.38" ssh-ed25519 AAAA... baskfy-box kite-session pull (forced command only)
    restrict,command="/home/desk/bin/accept-kite-request-token",from="3.108.148.38" ssh-ed25519 AAAA... baskfy-box kite request_token handoff (forced command only)

Two keys, two forced commands, two capabilities, revoked independently by deleting one line each.
Deliberately **not** one key with two verbs: that would put the choice in the client's hands via
`SSH_ORIGINAL_COMMAND`, which is precisely the property M58's design exists to deny.

`restrict` disables pty allocation and agent, port and X11 forwarding. `command=` replaces
whatever the client asks for, so neither key can open a shell — verified adversarially against
both: asking the pull key to run `id; cat .../.env` returns the token, and asking the handoff key
the same question returns `handoff refused: nothing on stdin`.

Public keys come from `tools/deploy/install-kite-session.sh` and
`tools/deploy/install-desk-handoff.sh`, which generate each pair **on the box** and print only the
public half. There is deliberately no step that copies a private key anywhere.

## 4. `prove-live-token-pickup.py` → `~desk/bin/prove-live-token-pickup.py`, mode 0500

Not part of the bridge — the evidence for it. Run on the desk against the deployed source with a
stubbed `kiteconnect`, it demonstrates the cache problem and the fix as facts:

    cd /home/desk/kite-momentum-rebalancer && \
      DRY_RUN=true .venv/bin/python /home/desk/bin/prove-live-token-pickup.py

---

## Why the desk must be the one that exchanges

Two properties of the **deployed** desk, both measured on the box on 31 Aug 2026 rather than read
off repo HEAD, which the box is not at:

* **It caches its Kite client.** `app/main.py:99` holds `_kite` in a module global and
  `app/kite_client.py` reads the token only in `Kite.__init__`. A token *written to its disk* at
  09:00 is therefore never seen by the uvicorn process that places Friday's orders. Worse, the
  order gateway captures `kite().kc` once, so even a new `Kite` would not reach it.
* **It cannot be restarted from here.** `momentum-web.service` is a root-owned system unit with
  `Restart=on-failure`, and the `desk` account has no passwordless sudo. A stop this bridge could
  cause is a stop it could not undo — on the box that places every live order.

`/callback` sidesteps both: it calls `kite().exchange_token(...)`, which calls `set_access_token`
on the cached client, in place, with no new object and no restart. It also means **nothing on the
Baskfy side has to know the desk's on-disk token format** — the deployed desk writes plain JSON
(`{"access_token": "<32 chars>"}`, mode 0600, no sibling `.key`), while repo HEAD writes Fernet
via `app/token_store.py`, which the deployed box does not even have. Leaf 1.1.1's worry (b),
answered by not being in that business at all.

## The two ways a request_token gets here

**In the browser, automatically.** Caddy on the Baskfy box redirects a bare Kite return —
`/api/v1/brokers/callback` carrying `request_token` and *no* `state` — to
`https://desk.modelbasket.in/callback?request_token=…`. A Baskfy-initiated login carries a `state`
and falls through to the API untouched. See `decile-blueprint/infra/docker/Caddyfile`.

**Server to server, with no browser.** `/opt/baskfy/bin/baskfy-desk-handoff`
(`tools/deploy/desk/baskfy-desk-handoff`) pipes the token on stdin over SSH to the forced command:

    printf '%s' "$REQUEST_TOKEN" | sudo /opt/baskfy/bin/baskfy-desk-handoff

The token arrives on stdin and nowhere else. A command line is world-readable in `ps` for as long
as the process lives, and `ssm send-command` parameters are retained in CloudTrail.

## What Baskfy gets

The access token, minutes later, through the forward leg it already has:

    python -m baskfy_worker.kite_session_cli pull

One app, one redirect, one login, two consumers — the sentence M58 was built around, with the
login now landing at Baskfy first and being handed onward.

## When the bridge stops working

It fails *closed*, and the desk says so rather than trading on a dead session. `is_authed()` calls
Kite on every page load; `/status` reports it; `/analyze` refuses with "Kite session expired — click
Login first." before a plan exists, and `/execute` cannot run without a `plan_id` that only
`/analyze` issues. So the worst case is a desk that will not trade, never a desk that trades on a
stale token.

The visible symptoms, in order of likelihood: the Caddy redirect stops matching (Zerodha changed
the query it sends), the `from=` clause stops matching because the box's egress IP moved, or the
desk's `DESK_PASSWORD` changed and the forced command's loopback call starts answering 401. All
three show up as `~desk/logs/kite-handoff.log` lines or as a login that visibly does not complete.

## Rotating

Delete `/opt/baskfy/secrets/ssh/kite-session` or `/opt/baskfy/secrets/ssh/desk-handoff` on the box,
re-run the matching installer, and replace that key's line here. The old line is the whole of the
revocation.

## If the box's IP changes

The `from=` clause stops matching and the leg fails — visibly. Update the clause and the Kite app's
IP whitelist together; they are the same fact written in two places.
