# Baskfy on this Mac — off AWS (29 Oct 2026)

Maulik, in session, 29 Oct 2026: *"I dont want to have aws anymore, and want to run everything
locally, and save money."* His answers the same day:

| Question | Answer |
|---|---|
| Live orders without the AWS static IP | **Read-only locally.** Every execution and auto-execute flag is false; trades are placed by hand in Kite |
| Always on? | **On demand.** Start it when you want it; the catch-up job backfills missed sessions after a Kite login |
| Reach it away from home | **Tailscale** — private, never on the public internet |
| AWS | **Torn down whole** once the local copy is verified |

## Why read-only, and what it costs

Since 1 Apr 2026 Kite accepts API orders only from a static IP registered on the developer account
(`docs/07` §4a). That IP was the box's EIP. A home connection is not it, so an order from this Mac
would be refused at Zerodha anyway — `local.sh` makes it refused here first, and `up` stops the
stack if any container reads a money flag true.

What stops: TWT's 09:15 auto-execute and the LIVE-plan drain, the MA-trail and partial
`SELL_AT_OPEN`s, swing auto-entries, F3 auto-exit, and every clicked execute. **GTT stops already
placed live at Zerodha and keep protecting open positions.** Exits the desk would have sent are now
yours, in Kite.

What keeps working (no registered IP needed): screens, baskets, factors, the nightly data plant,
holdings, live quotes, the desk's plans and pages (as simulations).

To trade from here again: a static IP from the ISP, registered in the Kite developer console
(changeable once a week), then the flags — by Maulik's hand, never an agent's.

## Run it

```bash
bash tools/deploy/local.sh up       # postgres + redis, migrate (a no-op at the same tag), every service, flag check
bash tools/deploy/local.sh status
bash tools/deploy/local.sh down     # data stays in the baskfy-local_* Docker volumes
```

**One address** (Maulik, 29 Oct 2026): the app at `http://localhost:8480`, the desk at
`http://localhost:8480/desk` — the same port, bound to 127.0.0.1 only. Caddy routes `/desk/*` to the
desk, and the desk's `DESK_PATH_PREFIX=/desk` keeps every link it renders under `/desk`. The desk
still asks for its own password (HTTP basic auth, unchanged from AWS).

**No Google on this Mac** (Maulik, 29 Oct 2026: "since it is on localhost, no google login
required"): the login page shows *Continue on this Mac*, which signs in as the account in
`/Volumes/T7-Work/baskfy-local/signin.public_id` (`2fe482ed2015`, mdave.5191@gmail.com). Anyone who
can reach the address can press it — which is why the address is reachable only from 127.0.0.1
and your own Tailscale devices. Runtime files
(sanitised env, the box's original config) live in `/Volumes/T7-Work/baskfy-local/`, mode 600,
never in git. Images are `baskfy-{py,web,desk}:d78907f`, pulled from ECR before teardown and also
saved in `/Volumes/T7-Work/baskfy-aws-exit-20261029/images/`. To run newer code, build the three
images locally and set `BASKFY_LOCAL_TAG`.

Docker Desktop needs ~8 GB of memory for the whole stack (the box had 8 GB). On a 16 GB Mac,
close heavy apps during the nightly (18:40–21:15 IST) if it is running then.

## Tailscale (phone and laptop)

1. `brew install --cask tailscale-app` (asks for the Mac's password), open it, sign in. Install
   the app on your phone, same account.
2. Enable MagicDNS and HTTPS certificates in the Tailscale admin (DNS page).
3. `bash tools/deploy/local.sh tailscale` — serves `https://<mac>.<tailnet>.ts.net` (the desk at
   `/desk` on the same address), writes that URL to `/Volumes/T7-Work/baskfy-local/public.url` so every
   later `up` uses it, and restarts the stack (not inside the 18:40–21:20 IST nightly window).
4. Kite developer console → redirect URL
   `https://mauliks-macbook-pro.tail6c95b5.ts.net/api/v1/brokers/callback` (by hand). No Google
   console change is needed: this Mac does not use Google sign-in.

## How it got here

`tools/deploy/exit-aws.sh export` stopped the box's app services after the last nightly, dumped
Postgres with per-table row counts, packed every volume and the box's env, checksummed and
uploaded it; `fetch` brought it to the T7 and verified sha256; `local.sh restore` restored it and
diffed every table's row count against the box. Only then was AWS destroyed.
