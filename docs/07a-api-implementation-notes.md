# 07a — API implementation notes

Companion to `docs/07-api-spec.md`, written while building Prompt 7. Same role as `docs/06a`:
everywhere the implementation departs from the letter of the spec, resolves an ambiguity, or
decides something the spec leaves open, it is written down here.

Implementation: `services/api/src/decile_api/` — `app.py` (wiring), `routers/` (the endpoints),
`auth.py`, `entitlements.py`, `ratelimit.py`, `problems.py`, `csv_export.py`.

---

## 1. A future `as_of` is refused, not clamped — and Prompt 6's behaviour changed

Prompt 7's acceptance criteria require that "run with a `historical_date` in the future returns
422 `no-trading-day`". `docs/07`'s catalogue describes the same status from the other end:
"`as_of` before the data start date".

Prompt 6 implemented neither. `resolve_as_of` *clamped* an out-of-range date to the latest
published day (recorded at the time in `docs/06a` §11), which answers a question the client did
not ask and hides the bug that asked it. That has been replaced: `resolve_as_of` now raises
`AsOfOutOfRange`, and `app.py` maps it to 422.

The distinction that survives is the one `docs/06` §step 1 actually asks for:

| Requested date | Behaviour |
|---|---|
| A trading day inside the range | used as asked |
| A **non-trading day** inside the range (weekend, holiday) | snapped **backwards**, and the response says which date was used |
| Later than the latest published day | `422 no-trading-day` |
| Earlier than the data start date | `422 no-trading-day` |

**The data start date is a constant, not a query.** `docs/01` §2.13 records that the reference
product "states historical data is available **from 1 Nov 2024**", and
`decile_api.screener.DATA_START_DATE` is that date. It is a published product promise, so reading
it back out of `min(factor_daily.date)` on every request would be both slower and less truthful —
a backfill that has not finished yet would silently move the advertised floor.

## 2. Where the client sends `data_version` — an inference

`docs/07` §"Error catalogue" requires `409 stale-data-version` when a "client sent a
`data_version` that no longer exists", but no request in `docs/07` carries one: the run body is
`{ as_of, override_definition }`.

Implemented as an optional `data_version` on the `POST /screens/{id}/run` and
`POST /screens/preview` bodies. The client sends the version it believes is current — from
`GET /meta/status`, or from a previous analytics response — and gets a 409 if the snapshot has
moved on. Omitting it always succeeds, so nothing is forced to send it.

That reading of "no longer exists" is *"is no longer the current one"*. The alternative reading —
a version that never existed — is a strict subset and is also refused. The 409 body carries both
`sent_data_version` and `current_data_version` so the client can re-fetch without another round
trip to `/meta/status`.

## 3. Validation failures answer `400`, not FastAPI's `422`

`docs/07` assigns `400 invalid-screen-definition` to "schema violation; `errors[]` lists field
paths" and reserves `422` for `no-trading-day`. FastAPI's default for a `RequestValidationError`
is `422`, which would put two different meanings on one status code — a client could not tell "your
JSON is malformed" from "that date is outside the range we hold". The handler in `app.py`
overrides it.

`errors[]` entries are `{field, message, type}`, with FastAPI's `body`/`query`/`path` location
marker stripped: `("body", "definition", "sort_by")` is reported as `definition.sort_by`.

## 4. `api_access` is `false` for everyone

Prompt 7 §3 says "everyone is entitled to everything except export/custom columns/historical
ranks". Read literally that grants `api_access`. `docs/07` §Entitlements shows the opposite in its
own example payload (`"api_access": false`), and there is no public API to be entitled to until
Prompt 20 builds one. `docs/07` is the source of truth and is also the safer reading, so
`API_ACCESS_AVAILABLE = False`.

Related: `X-API-Key` is named in `docs/07`'s header line and is **knowingly ignored** — there is no
key store, so presenting the header authenticates nothing and buys no rate-limit tier. The
600/min tier exists in settings and is unreachable until Prompt 20.

`max_screens` is `50`, `docs/07`'s example number. It is not on `plan.features` in
`seed_data.PLANS`, so it is a constant here until Prompt 13 puts a per-plan number on the row.

## 5. What the three gated features gate, and where

Prompt 7 §3 names the three; `docs/07` §Entitlements requires enforcement "server-side on every
gated endpoint". Enforcement points:

| Feature | Enforced on | Trigger |
|---|---|---|
| `export_csv` | `GET /screens/{id}/csv` | always — `docs/07` marks the route "(entitlement-gated)" |
| `historical_ranks` | run, preview, csv | the resolved `as_of` is not the latest published day |
| `custom_columns` | run, preview, csv | the column set exceeds `DEFAULT_COLUMNS` |

Two consequences worth being explicit about. Asking *explicitly* for today's date is not a
historical rank, so a date picker set to the default does not trip a 402. And custom columns are
gated at **delivery**, not at save: a lapsed subscriber keeps their saved screens intact and sees
the default columns' worth of data until they renew, rather than having their configuration
rejected.

The stub grants the three to any caller with an `active` subscription row — the shape Prompt 13
implements for real, so the call sites will not move.

## 6. `screen_run.results` stores `factor_value` as a string

`docs/04` defines the column as `jsonb` holding `[{rank, instrument_id, factor_value}]`. asyncpg
serialises JSONB with `json.dumps`, which has no `Decimal` support, and routing through `float`
would put `13.0` in the audit trail where the API returned `13.00` — exactly the disagreement
CLAUDE.md house rule 8 exists to prevent. The value is written as its exact decimal string.

Related: the audit row is written **only when the result was computed**, not on a cache hit. A
cached payload carries no `instrument_id` (the wire format has no use for one), so there is
nothing to store; the row that a cache hit would have written is by definition the row the first
run already wrote for that `(screen_id, as_of, definition_hash)`.

## 7. Example screens are read-only, and refuse with `404`

`docs/01` §1 calls the six seeded screens "read-only templates". `docs/07`'s catalogue has no 403,
and a 403 on a screen the caller can `GET` would be an argument about permissions rather than an
answer. `PATCH`/`DELETE` on an example screen return `404 not-found` with the actionable detail —
"duplicate it to make an editable copy" — and `POST /screens/{id}/duplicate` is that path.

Another user's screen is also `404` rather than `403`, so the API never confirms that an id exists.

## 8. `503 pipeline-degraded` is for "no good version", not "last run failed"

`docs/07` lists `503 pipeline-degraded` for "last run failed its QA gate". `docs/11` §Reliability
says the opposite should happen: "if the pipeline fails, serve the last good `data_version` with a
banner".

Both are implemented, on the reading that they describe different situations:

* **A failed run with a previous good version** — the screen endpoints keep answering from the last
  published version, and `GET /meta/status` returns `degraded: true` for the banner to read. This
  is `docs/11`'s graceful degradation.
* **No published run at all** — there is nothing to fall back to, and `503 pipeline-degraded` is
  the honest answer. That is `NoPublishedData`.

## 9. The rate limiter fails **closed**

`docs/07` §Conventions fixes the numbers; nothing says what to do when Redis is unreachable. The
limiter answers `503` rather than waving traffic through. Failing open would remove the control at
the exact moment the cache is unhealthy and the database can least afford unmetered traffic —
`docs/11`'s "graceful degradation" is about serving stale *data*, not about dropping controls.

Idempotency makes the opposite choice for the opposite reason: an unavailable Redis degrades to
"not deduplicated", because the worst case is a duplicate screen the user can delete.

The algorithm is the token bucket `decile_providers.ratelimit` already runs for Kite — the same
Lua script, executed against an async client. Its rationale carries over verbatim: a fixed window
permits a burst of 2N across a window boundary, and 120 requests in one second is not "60 per
minute".

## 10. Prompt 6's screen cache became async

`ScreenCache` was a synchronous protocol, because Prompt 6's only caller was the worker's publish
step. Every caller is in fact inside a coroutine, and a blocking Redis round trip inside an event
loop stops every other in-flight request for its duration — on a warm screen that round trip *is*
the request (`docs/11` budgets 150 ms p95 warm). The protocol, `purge_screen_cache`,
`warm_screen_cache` and the worker's `build_cache()` are all async now.

The protocol's methods are declared as returning `Awaitable[object]` rather than as `async def`,
because redis-py types every command with one signature covering both its sync and async clients;
an `async def` would not match `redis.asyncio.Redis` structurally.

## 11. `PyJWT` is a dependency `docs/02` does not name

`docs/02`'s locked stack has no JWT library, and `docs/07` requires HS256 bearer verification.
PyJWT is the reference implementation, ships `py.typed`, and is the alternative to hand-rolling
signature verification — which is the category of code that should not be hand-rolled. Flagged
here per CLAUDE.md house rule 1.

Three refusals it does not give for free, added in `auth.py`:

* **No algorithm negotiation** — `algorithms=["HS256"]` is a fixed list of one, so `alg: none` and
  RS256-verified-as-HS256 both fail.
* **No unbounded lifetime** — `docs/11` says "15-min access"; a correctly signed token whose
  `exp - iat` exceeds the configured ceiling is refused, so a misconfigured web app cannot issue
  year-long credentials against the same secret.
* **No unknown subjects** — `sub` is an `app_user.public_id`, and a token for a user who is not in
  the database is refused rather than treated as a nameless authenticated caller.

`Settings.require_configured()` refuses to start in `production` with an empty secret, or one
shorter than the 32 bytes RFC 7518 §3.2 requires for HS256.

## 12. The run endpoints return raw bytes

`ScreenResult.to_json()` is the response body, verbatim, rather than a value re-serialised through
`ScreenRunResponse`. `docs/06` §"Determinism guarantee" promises byte-identical results for the
same definition, `as_of` and `data_version`, and that promise is only worth something if the bytes
the client sees are the bytes that were hashed and cached. Re-serialising would also route every
`numeric` through `float` and turn `13.00` into `13.0`.

`response_model=ScreenRunResponse` still documents the shape, which is what the generated
TypeScript client reads — FastAPI skips serialisation when a handler returns a `Response` but keeps
the model in the OpenAPI document.

## 13. Volatility is still a fraction on the wire

`docs/07`'s example row shows `"vol_12m": 57.93`; storage is the decimal fraction `0.5793…`
(`docs/13` §2 finding 4, "the UI multiplies by 100"). The API projects the stored value unchanged.
Flagged in `docs/06a` §10 and unresolved here for the same reason: doing the ×100 at this layer
would put two meanings of `vol_12m` into one system. It belongs to the presentation layer —
Prompt 8.

## 14. Endpoints deliberately absent

Prompt 7 covers "metadata, screens, and run endpoints (the rest come in later prompts)". So
`docs/07`'s `/instruments/*`, `/listings`, `/indices/dashboard`, `/market-health*`, `/portfolios*`,
`/backtests*`, `/auth/*`, `/me`, `/plans`, `/checkout/*`, `/webhooks/razorpay` and `/invoices*` are
not served, and `test_api_artifacts.py` asserts that nothing beyond the Prompt 7 surface is
exposed. An unrouted path answers `404 not-found` in problem+json like everything else.

`GET /me` is where `docs/07` returns the entitlements payload; `Entitlements.as_dict()` already
produces it, and Prompt 12 will mount it.

> **Updated through Prompt 12.** `/instruments/*` and `/listings` landed in Prompt 10,
> `/indices/dashboard` and `/market-health*` in Prompt 11, and `/auth/*` plus `/me*` in Prompt 12.
> Still absent: `/portfolios*` (Prompt 14), `/backtests*` (Prompt 15), and the billing half of
> §"Account & billing" — `/plans`, `/checkout/session`, `/webhooks/razorpay`, `/invoices*`
> (Prompt 13). `test_api_artifacts.py` still asserts that nothing beyond the built surface is
> exposed.
