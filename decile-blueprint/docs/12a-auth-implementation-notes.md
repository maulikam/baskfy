# 12a — Auth, accounts and profile implementation notes

Companion to `docs/07-api-spec.md` §"Account & billing" and `docs/11-nonfunctional.md` §Security
and §"Compliance & legal (India)", written while building Prompt 12. Same role as `docs/06a`,
`docs/07a`, `docs/08a`, `docs/09a`, `docs/10a` and `docs/11a`.

Implementation: `services/api/src/decile_api/{auth_service,security,csrf,logging}.py`,
`routers/auth.py`, `email/`, `packages/core/src/decile_core/models/auth.py`,
`services/worker/src/decile_worker/tasks/purge_accounts.py`, `apps/web/src/lib/auth/`,
`apps/web/src/app/(auth)/`, `apps/web/src/app/(app)/{profile,change-password}/`,
`apps/web/src/middleware.ts`. New tables are written up in `docs/04c-auth-tables-addendum.md`.

---

## 1. Four endpoints docs/07 does not list

docs/07 §"Account & billing" enumerates nine `/auth/*` and `/me` routes. Four more were needed and
are implemented:

* **`POST /auth/verify-email`** — the counterpart to the confirmation mail `/auth/register` sends.
  docs/07 names `register` but not the confirmation it implies; sending an unverifiable
  verification link would be worse than sending none.
* **`GET /me/export`** and **`DELETE /me`** — docs/11 §Compliance: "DPDP Act: consent record, data
  export and deletion endpoints". docs/07 names none of the three; Prompt 12 §5 asks for all of
  them.
* **`POST /me/restore`** — cancelling a pending deletion without a UI. In practice the web app
  cancels by signing in, which `/auth/verify-otp` and `/auth/login` both do.

## 2. Three dependencies docs/02 does not name

Same pattern as PyJWT in Prompt 7, and each is recorded at its `pyproject.toml` entry:

* **`argon2-cffi`** — docs/11 names the *algorithm* ("Argon2id password hashing") and no library.
  This is the binding to the PHC winner's own C implementation, and the alternative is
  hand-rolling a KDF.
* **`email-validator`** — Pydantic v2's `EmailStr` does not function without it, and docs/11's PII
  inventory starts with "email".
* **`httpx`** — docs/02 locks **Resend**, which is an HTTPS API with no Python SDK worth a
  dependency. `decile-providers` already depends on httpx, so this only makes the use explicit.

**mailpit** is a compose service, not a dependency: Prompt 12 §3 names it by name for local
delivery. `make up` starts it; it accepts SMTP on 1025 and serves what it caught on 8025.

## 3. Codes and refresh tokens are SHA-256; only passwords are Argon2id

Argon2id is deliberately slow because a password is low-entropy, long-lived and reused across
sites — an attacker with the hash has an offline guessing game worth playing. A refresh token is
256 random bits with a single use; a six-digit OTP lives ten minutes, allows five guesses, and is
rate-limited per address *and* per IP. Neither has a guessing game to slow down, and paying 19 MiB
of memory per token refresh would be a denial-of-service surface rather than a defence.

What SHA-256 buys is the property that actually matters for a high-entropy secret: the stored value
is not a credential, so a database dump cannot be replayed. Written up on
`decile_api.security` and in `docs/04c`.

## 4. Refresh reuse revokes the family, and that is deliberate

docs/11 says "rotating refresh". Rotation without reuse detection is bookkeeping. Every use mints a
successor and retires the predecessor; if a retired token arrives again, the entire `family_id` —
every descendant of that login — is revoked and the caller is signed out everywhere. RFC 9700
§4.14.2.

The cost is a real false positive: a client that races itself (two tabs refreshing at once) can
trip it and be signed out. That is the documented trade, and it is the right way round — the
alternative is that a stolen cookie keeps working alongside the legitimate one indefinitely.

## 5. Auth.js carries the API's access token rather than minting its own

Prompt 8 had the web app mint an HS256 token for the API from a shared secret. Now `/auth/login`
mints one and the `jwt` callback carries it, so the token the API verifies is the token the API
created — one issuer, one place where the claims are decided.

The refresh cookie is the API's and is scoped to `/api/v1/auth`. The browser session is Auth.js's
own cookie. Two refresh mechanisms would be two things to expire and two ways to be half-signed-in;
this way the API owns credential lifetime and Auth.js owns the browser session.

`stub-endpoints.ts` and `DECILE_ALLOW_STUB_AUTH` are **deleted**. The browser suite now registers
and signs in for real.

## 6. Every neutral answer is neutral in wording *and* in timing

`/auth/register`, `/auth/request-otp` and `/auth/forgot-password` answer `202` with the same
sentence whether or not the address is known — docs/11's PII inventory makes the customer list PII,
and for a paid product it is the customer list.

Two details make that hold:

* `verify_password` hashes even when there is no stored hash, so "this address has no password" is
  not observable from the response time.
* the lockout counts against the *identifier presented*, so an address with no account can be
  locked exactly like one with. Counting only real accounts would make the lockout the oracle the
  wording avoids being.

Registering an address that already exists mails **the owner** a sign-in code, which is the useful
thing to do with that information and reveals nothing at the form.

## 7. One place deliberately logs a secret, and it cannot run in production

`ConsoleTransport` prints the message body — including the OTP — when `email_transport` is
`console`, which is the no-configuration default. That is the point: a developer with no mail
server still has to be able to read the code. `Settings.require_configured` refuses `console` in
production, alongside refusing an insecure cookie and an empty Resend key.

Everywhere else, `RedactingFilter` (in `decile_api.logging`) strips secrets at the record, before
any formatter sees them: by field name, and by pattern in the rendered message. Prompt 12's second
acceptance criterion drives the real endpoints and asserts that no password, access token, refresh
token or CSRF token appears in the captured output.

## 8. `PATCH /me` changes the display name only

Changing an email address is an identity change: the new address has to be proved before it takes
effect, or the endpoint is an account-takeover primitive for anyone holding a stolen access token.
That is a separate flow (issue a code to the *new* address, swap on verification) and it is not
built. The profile page says so rather than showing a field that does nothing.

## 9. Erasure anonymises when there are invoices, and deletes otherwise

`payment.user_id` is `NOT NULL` in docs/04, and docs/11 §Compliance requires GST-compliant invoices
— a statutory retention obligation that DPDP's erasure right does not override. So an account that
has ever paid is anonymised in place: tombstone address, no name, no password hash, and every child
row that is not an invoice deleted. An account that never paid is deleted outright.

The `account_deletion` row cascades away with the user, so the audit evidence is the purge step's
`pipeline_run_step` note — public ids, never addresses. `decile.accounts.purge` runs daily at 03:00
IST.

## 10. The consent checkbox names documents that are not written yet

docs/11 §Compliance requires Terms, a Privacy Policy and a Refund Policy "before taking a single
payment" — Prompt 13's gate, with the pages themselves in Prompt 18. The registration form
therefore names them as plain text rather than linking to a 404.

`consent_record.document_version` is stored regardless, so consent to the published text will be a
new record rather than a silent re-use of this one. **This is an open item**: the consent captured
today references a version string and not a document.

## 11. The middleware gate is a convenience; the API is the enforcement

`src/middleware.ts` redirects an unauthenticated visit to `/profile` or `/change-password` to
`/login?next=…`. It decides on the presence of a session *cookie*, which is not a claim a server
should trust on its own — every gated read is authorised again by the API against the bearer token.
The middleware exists so the user sees a sign-in form instead of an empty page.

`?next=` is validated as a relative path in both places. An unvalidated `next` is an open redirect,
which is a phishing primitive.

The middleware also sets the CSP docs/11 asks for ("Strict CSP (`default-src 'self'`)"), with a
per-request nonce. `next-themes` writes an inline script before hydration to prevent the theme
flash, so the nonce is threaded from the middleware header through the root layout into
`ThemeProvider`. Reading that header makes every route dynamically rendered — which they already
were, and a nonce baked into a static page would be the same nonce for every visitor and therefore
no protection at all.

## 12. Two test-fixture facts worth knowing

* **The browser suite's password is a published constant** (`decile_api.seed.E2E_PASSWORD`). It
  guards a throwaway database and lives in a config file the suite also reads; a secret in both
  places is not a secret. `seed e2e` is not a command anything but a test database should see.
* **Test addresses moved from `@decile.test` to `@example.com`.** RFC 6761 marks `.test`
  special-use and `email-validator` refuses it outright, so an address there could never be
  registered. RFC 2606's `example.com` is the documentation domain and is accepted.

## 13. What Prompt 12 does *not* deliver

* **`GET /plans`, `POST /checkout/session`, `POST /webhooks/razorpay`, `GET /invoices`** — the
  billing half of docs/07 §"Account & billing". Prompt 13.
* **Changing an email address** (§8).
* **The legal pages the consent checkbox names** (§10).
* **`api_access` remains false for everyone** — Prompt 20 owns the key store (`docs/07a` §4).
