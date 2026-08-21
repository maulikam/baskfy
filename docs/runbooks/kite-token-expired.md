# Runbook — the Kite access token has expired

**Alert:** `kite_token_expiring` (critical, or warning when it is only *about* to expire)
**Raised by:** `decile.ops.check_kite_token`, hourly at :05 IST, and by the Prometheus rule on
`decile:provider_error_rate:ratio15m{provider="kite"} > 0.5`
**Verified against:** NOT YET — written from `decile_providers.tokens` and the Kite Connect
documentation, not from a real expiry. See `docs/runbooks/README.md`.

docs/09 calls this "the #1 pipeline failure", and it is structural rather than a bug: **Kite
access tokens die at the start of the next trading day, every day.** They are not refreshable. A
human with the Zerodha login completes an OAuth redirect and hands back a `request_token`, which is
exchanged for a fresh `access_token`. Nothing about that can be automated away without storing a
broker password, which `docs/11` §Security forbids outright ("the app never asks for a broker login
from end users" — and it should not store its own in plain form either).

## Do this

### 1. Confirm it is actually the token

```
uv run python -m decile_providers.cli doctor
```

Look for the `kite` line. `[DOWN] kite` with a detail mentioning the token is the case this runbook
covers. `[DOWN] kite` with `CredentialsMissing` means `DECILE_KITE_API_KEY`/`_API_SECRET` are unset,
which is a deployment problem, not a token problem.

The same report is on `/admin/providers` if you would rather not shell in.

### 2. Get a request token

Open, in a browser signed in to the Zerodha account the subscription belongs to:

```
https://kite.zerodha.com/connect/login?v=3&api_key=<DECILE_KITE_API_KEY>
```

Complete the login. Kite redirects to the app's configured redirect URL with `?request_token=...`
in the query string. **Copy that value.** It is single-use and short-lived — minutes, not hours.

### 3. Exchange it and store the result

**There is no `providers login` command.** `decile_providers.cli` has exactly one subcommand,
`doctor` (Prompt 2 delivered the token *store* and the health report, not the OAuth exchange). So
the exchange is done by hand, in two steps:

```bash
# a) exchange the request token. checksum = sha256(api_key + request_token + api_secret).
python - <<'EOF'
import hashlib, os, httpx
key, secret = os.environ["DECILE_KITE_API_KEY"], os.environ["DECILE_KITE_API_SECRET"]
request_token = os.environ["KITE_REQUEST_TOKEN"]
checksum = hashlib.sha256((key + request_token + secret).encode()).hexdigest()
r = httpx.post(
    "https://api.kite.trade/session/token",
    data={"api_key": key, "request_token": request_token, "checksum": checksum},
    headers={"X-Kite-Version": "3"},
    timeout=30,
)
r.raise_for_status()
print(r.json()["data"]["access_token"])
EOF
```

```bash
# b) store it, encrypted, where the adapter looks.
uv run python - <<'EOF'
import os
from decile_providers.settings import get_provider_settings
from decile_providers.tokens import AccessTokenStore
s = get_provider_settings()
store = AccessTokenStore(s.kite_token_path, s.kite_token_encryption_key)
print(store.save(os.environ["KITE_ACCESS_TOKEN"]).issued_at)
EOF
```

`save` writes the blob to `DECILE_KITE_TOKEN_PATH` (default `.secrets/kite-token.enc`),
Fernet-encrypted under `DECILE_KITE_TOKEN_ENCRYPTION_KEY`, `chmod 600`.

> **This is the worst part of this runbook and it should be a command.** Doing an OAuth exchange
> by pasting a heredoc at 03:00, with a single-use token that expires in minutes, is how the token
> gets pasted into a shell history or a Slack message. A `providers login --request-token` that
> does both halves is a small piece of work and belongs in the next prompt that touches
> `packages/providers`.

### 4. Confirm

```
uv run python -m decile_providers.cli doctor          # kite should read [OK]
uv run python -c "
from decile_providers.settings import get_provider_settings
from decile_providers.tokens import AccessTokenStore
s = get_provider_settings()
print(AccessTokenStore(s.kite_token_path, s.kite_token_encryption_key).require_fresh().issued_at)
"
```

`require_fresh()` raises `AccessTokenExpired` if the stored token is already stale, so a clean
print is the confirmation.

### 5. Re-run the night, if the pipeline already failed on it

Follow [pipeline-failed.md](pipeline-failed.md) §"Re-running a night". In short: `/admin/pipeline`,
find the failed date, press **Re-run**. Every step is idempotent (docs/02 rule 3), so re-running a
night that partly succeeded is safe.

## Why the warning fires six hours early

`DECILE_KITE_TOKEN_WARNING_HOURS` (default 6). `decile.ops.check_kite_token` asks whether the token
would be expired six hours from now, which — because expiry is a *calendar-day* boundary in IST,
not an interval — means the warning arrives at 18:00 IST for a token that dies at midnight. That is
before the 18:45 IST nightly run, which is the whole point: a token replaced at 18:00 costs
nothing, and one replaced at 21:00 costs a night's data.

The check makes **no network call**. Expiry is computed from the stored issue time
(`decile_providers.tokens.AccessToken.is_expired`), and it errs towards declaring expiry early — a
false "expired" costs one login, a false "valid" costs a whole night.

## What this is not

* **Not a retry.** `decile_providers.retry` deliberately does not retry `AccessTokenExpired`: it
  would fail identically five times while delaying the alert a human has to act on.
* **Not fixable by restarting the worker.** The token is on disk, not in process memory.
* **Not silent.** If the token expires mid-run, `fetch_daily_bars` fails, the chain stops, and
  `data_version` is not bumped — so the site keeps serving yesterday's consistent snapshot rather
  than a half-updated one. Users see a stale-data banner, not wrong numbers.
