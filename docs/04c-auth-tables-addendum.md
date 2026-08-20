# 04c — Authentication, consent and erasure tables

Addendum to `docs/04-data-model.md`, written while building Prompt 12. Same role as `docs/04a` and
`docs/04b`: tables that are not in `docs/04`'s DDL, and the numbered requirement that each one
exists to satisfy.

`docs/04`'s account section defines `app_user` with a `password_hash` column and nothing else about
authentication — no refresh tokens, no one-time codes, no lockout state, no erasure window.
`docs/11-nonfunctional.md` §Security and §"Compliance & legal (India)" require all four.

Migration: `services/api/alembic/versions/0005_auth_tables.py`.
Models: `packages/core/src/decile_core/models/auth.py`.

---

## The tables

| Table | Required by |
|---|---|
| `auth_verification_token` | Auth.js v5's adapter contract. `docs/02` locks Auth.js, and `apps/web/src/lib/auth/adapter.ts` implements `createVerificationToken` / `useVerificationToken` against it. |
| `auth_token` | `docs/11`: "OTP login as the default path". A one-time code needs an expiry and an attempt count, or it is a password that never changes. |
| `refresh_token` | `docs/11`: "rotating refresh in an httpOnly … cookie". Rotation is only meaningful if the predecessor is remembered long enough to notice a replay. |
| `auth_lockout` | `docs/11`: "account lockout after 10 failures with email notification". |
| `account_deletion` | `docs/11` §Compliance: "DPDP Act: … deletion endpoints", with PROMPTS.md Prompt 12 §5's seven-day window. |
| `consent_record` | `docs/11` §Compliance: "DPDP Act: **consent record** …". |

Plus one column: `app_user.deleted_at`, the soft-delete marker the window needs.

## Column notes that are not obvious from the DDL

**`auth_token.user_id` is nullable.** An OTP can be requested for an address with no account —
`docs/11` makes OTP the default path, and requiring registration first would make the default path
the second step. `email` is always set; the user is resolved at verification.

**`auth_token.token_hash` and `refresh_token.token_hash` are SHA-256, not Argon2id.** Argon2id is
slow on purpose because a password is low-entropy, long-lived and reused across sites. A refresh
token is 256 random bits used once; an OTP lives ten minutes with five attempts and two rate
limits. Neither has an offline guessing game to slow down, and paying 19 MiB per token refresh
would be a denial-of-service surface. What SHA-256 buys — a database dump is not a set of
credentials — is preserved either way. `docs/12a` §3.

**`refresh_token.family_id` is the unit of revocation.** Every token descended from one login
shares it. Presenting an already-rotated token revokes the whole family (RFC 9700 §4.14.2), which
is why the retired rows are kept rather than deleted.

**`auth_lockout` is keyed by the identifier presented, not by `user_id`.** Failures against an
address with no account are counted the same way. Otherwise the lockout is itself an enumeration
oracle: an attacker learns which addresses exist by which ones can be locked.

**`auth_lockout` is a table, not a Redis key.** A cache flush must not silently unlock every
account, and `notified_at` has to survive or the lockout email is re-sent on every subsequent
attempt.

**`consent_record` is append-only.** A row updated in place cannot answer "what did they agree to,
and when", which is the only question it exists to answer. Withdrawal is a new row with
`withdrawn_at` set. `document_version` is stored so a revised Privacy Policy needs fresh consent
rather than silently inheriting the old one.

**`account_deletion` cascades away with the user.** It is keyed by `user_id`, so it is itself a
record about a person; the evidence that an erasure happened is the purge step's
`pipeline_run_step` note, which carries public ids and no addresses.

## What the purge does with invoices

`payment.user_id` is `NOT NULL` in `docs/04`, and `docs/11` §Compliance requires GST-compliant
invoices — a statutory retention obligation that DPDP's erasure right does not override. So an
account that has ever paid is **anonymised in place** rather than deleted: the address becomes an
opaque tombstone at `@deleted.invalid`, the name and password hash go, and every child row that is
not an invoice is removed. An account that never paid is deleted outright and everything cascades.
`services/worker/src/decile_worker/tasks/purge_accounts.py`, `docs/12a` §9.
