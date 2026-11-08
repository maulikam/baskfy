# 08a — Web app implementation notes

Companion to `docs/08-ui-spec.md`, written while building Prompt 8. Same role as `docs/06a` and
`docs/07a`: every departure from the letter of the specification, every ambiguity resolved, and
every decision a later prompt will have to live with.

Implementation: `apps/web/` — `src/app/` (routes), `src/components/{ui,shell,data}`, `src/lib/`.

---

## 1. The palette, and how it is verified

Prompt 8 deliverable 1 asks for "a neutral greyscale scale, ONE accent, semantic positive/negative
tokens that pass 4.5:1 in both light and dark". `docs/11` §Accessibility widens that to every
colour that carries text: "Contrast ≥ 4.5:1 in both themes, including the positive/negative number
colours."

The tokens live in `src/app/globals.css` — Tailwind v4 has no `tailwind.config.js`, so the theme
*is* the stylesheet. `src/lib/__tests__/contrast.test.ts` parses that file, computes the real WCAG
2.x ratios, and asserts each of them. 43 assertions, covering every text token against every
surface it is allowed to appear on.

Measured ratios (light / dark):

| Token | on `--background` | on `--card` |
|---|---|---|
| `--foreground` | 18.1 | 18.3 / 16.3 |
| `--muted-foreground` | 5.8 / 7.4 | 6.1 / 6.6 |
| `--accent` | 5.7 / 7.8 | 6.0 / 7.1 |
| `--positive` | 5.2 / 10.0 | 5.4 / 8.9 |
| `--negative` | 5.6 / 7.8 | 6.1 / 6.9 |

The test also pins two things the ratio alone does not catch: that a primary button's label clears
4.5:1 *against the accent*, and that positive and negative differ in luminance by enough to survive
a monochrome display.

**Tabular numerals are set on `html`, not per component.** `docs/08` asks for "one tabular-figure
setting for every number"; a per-component setting is a per-component thing to forget, and columns
that do not align is the defect users notice first in a screener.

## 2. Auth.js cannot have both credentials and database sessions

`docs/02` locks "Auth.js v5 (credentials + email OTP), **sessions in Postgres**". Those two clauses
cannot both hold: **Auth.js v5 does not support database sessions with the Credentials provider**
and requires `strategy: "jwt"`. This is a constraint of the library, not a preference.

What is implemented:

* `session: { strategy: "jwt" }` — forced, as above. The session cookie is Auth.js's own encrypted
  JWT.
* A **Postgres adapter** (`src/lib/auth/adapter.ts`), written against Decile's own schema rather
  than `@auth/pg-adapter`. That package owns four tables of its own including `users`, and
  `docs/04` already defines the account of record as `app_user` — which is also what
  `services/api` looks a bearer token's `sub` up in. Two user tables would mean two answers to
  "who is this".
* The adapter carries the user half (`app_user`) and the OTP verification tokens. Session rows are
  never written, because with a JWT strategy nothing reads them.

**One table is required and does not exist yet:** `auth_verification_token`, for the OTP codes.
The adapter is written against it, and the migration is Prompt 12's (`/auth/*` is Prompt 12's
whole subject). Until then the OTP path runs through the stub described in §3, which does not
touch that table.

## 3. What "works end-to-end against stubbed endpoints" means here

Prompt 8: "Login/register/forgot-password pages can be placeholders that work end-to-end against
stubbed endpoints for now."

`src/lib/auth/stub-endpoints.ts` is the seam. Each function calls the real `docs/07` endpoint
(`POST /auth/login`, `/auth/request-otp`, `/auth/verify-otp`) and falls back to a stub **only when
the API answers 404** — which is exactly what it does today, because `docs/07a` §14 lists
`/auth/*` among the routes Prompt 7 deliberately did not build.

So: the forms genuinely submit, Auth.js genuinely runs, the session cookie is genuinely set, and
the HS256 token `services/api` verifies is genuinely minted. The only stubbed part is the
credential check. When Prompt 12 ships those endpoints, the fallback stops being reached and
nothing has to be deleted.

Two properties of the stub are deliberate:

* It **never invents a user**. It looks the address up in `app_user` through the adapter, so a
  stub login cannot mint a token for an account the API would not recognise.
* It **refuses to run in production** (`NODE_ENV === "production"` disables it), so a deployment
  that has not yet shipped Prompt 12 fails closed rather than accepting `000000` as an OTP.

## 4. The access token is not the session

Two tokens, easily confused:

| | Session cookie | `session.accessToken` |
|---|---|---|
| Issued by | Auth.js | `src/lib/auth/jwt.ts` |
| Signed with | `AUTH_SECRET` | `DECILE_JWT_SECRET` |
| Read by | the web app | `services/api` |
| Lifetime | 30 days | **15 minutes** (`docs/11` §Security) |

The access token is re-minted inside the `jwt` callback whenever it is within a minute of expiring,
so a long session never carries a stale bearer token. `src/lib/auth/__tests__/jwt.test.ts` pins the
claim shape against what `decile_api.auth.decode_token` requires — one algorithm, `sub`/`iat`/`exp`
all present, and `exp - iat` exactly 900 seconds. That verifier refuses a longer-lived token *even
when correctly signed* (`docs/07a` §11), so the two halves have to agree exactly.

## 5. ⌘K is wired to an endpoint that does not exist yet

`docs/08` §"App shell" requires "global instrument search (⌘K)", and Prompt 8 says to wire it to
`/instruments?search=`. That endpoint is Prompt 10's — `docs/07a` §14 lists it among the routes
Prompt 7 left out.

It is wired to exactly that path (`src/lib/api/instruments.ts`). Because the API answers a
documented RFC 9457 `404 not-found` rather than an unparseable error, the palette can tell "not
built yet" from "search failed" and says so, instead of showing an empty result set that reads as
"no such stock". The palette's other half — navigation — works today, which is what makes it worth
opening at all.

It is a hand-written `fetch` rather than a generated client call for the same reason: the path is
not in `openapi.json` yet, so it is not in `paths`.

## 6. Lighthouse, axe, and what each one proves

Prompt 8's first acceptance criterion is "Lighthouse ≥ 95 accessibility on `/` and `/kitchen-sink`
in both themes". Two suites, because one tool cannot do both halves well:

* `e2e/lighthouse.spec.ts` runs the **real Lighthouse binary** and asserts the category score.
  Getting it to measure the dark theme took care: Lighthouse opens its own tab with its own
  emulation, so launching Chrome with `colorScheme: "dark"` is not enough. The run primes
  `localStorage.theme` in the same browser profile first — that is what `next-themes` actually
  reads — and asserts `html.dark` before Lighthouse starts. Without that the dark run would
  silently score the light page.
* `e2e/accessibility.spec.ts` runs **axe-core** directly, per page and per theme, against the
  WCAG 2.2 AA rule set that `docs/11` §Accessibility names.

Lighthouse's accessibility category is a weighted subset of axe, so these agree by construction;
axe is the stricter of the two and gives a violation list rather than a number.

**Scores: `/` 100 in both themes, `/kitchen-sink` 100 in both.** The landing page scored 98 before
`landmark-one-main` was fixed by giving it a real `<main>`.

## 7. Three real defects the acceptance criteria caught

Worth recording, because each one was invisible until measured.

1. **The announcement banner cost 0.031 CLS on every page load.** Gating it on `localStorage` meant
   it could only appear *after* hydration, pushing the entire shell down by its own height one
   frame late. Dismissal is now a **cookie**, which the server reads, so the first paint is already
   correct. Dismissing still moves the layout, but that shift follows a click and `hadRecentInput`
   excludes it — which is the distinction CLS exists to draw.
2. **The repeated header row was an `aria-hidden` container full of focusable buttons.** axe's
   `aria-hidden-focus`, and a genuine defect: a keyboard user would tab into sort controls the
   screen reader had been told did not exist. The repeat is now a picture of the header — no roles,
   no buttons — while the sticky header at the top keeps them.
3. **The loading skeleton was two pixels shorter than the table.** The `height` was on the outer
   bordered box in one branch and on the inner scroll container in the other. Both now share the
   same structure.

## 8. The DataTable is a `role="grid"` of divs, not a `<table>`

Virtualisation means ~30 of 4,000 rows exist in the DOM, positioned by `transform`. A real
`<table>` cannot be positioned that way without breaking its own layout algorithm, and the usual
workaround — absolutely positioned `<tr>` — discards the row and column semantics anyway.

The ARIA grid pattern keeps them explicitly: `aria-rowcount` states the true total (4,001, header
included), each row carries its true `aria-rowindex`, and a roving tabindex makes the whole grid
one tab stop with arrow-key movement inside it. That is what `docs/08` §"Accessibility & quality
bar" means by "the table supports arrow-key navigation", and it is what lets 4,000 rows scroll at
60fps.

**Measured, from a Chrome DevTools trace over a 40-step wheel scroll: median frame time 5.8 ms,
p95 24.9 ms** against a 16.7 ms budget and a 33.3 ms p95 allowance. Memoising the row component cut
the median from 8.7 ms — without it, every scroll frame re-rendered ~360 cells that had not
changed. The p95 is inflated by the driving loop's own 16 ms pause between wheel events and by
tracing overhead; the median is the number that describes the table.

## 9. Client-side volatility is where the ×100 happens

`docs/13` §2 finding 4: volatility is stored as a decimal fraction and "the UI multiplies by 100".
`docs/06a` §10 and `docs/07a` §13 record that the screener and the API deliberately do *not*, so
that there is exactly one place in the system where it happens. `src/lib/format.ts`'s
`formatFraction` is that place, and `format.test.ts` says so.

## 10. Deliberately not built

`docs/08` describes the whole product; Prompt 8 builds "the shell and design system". The routes in
`src/lib/nav.ts` marked `status: "planned"` render as **disabled items with a tooltip naming the
prompt that delivers them** rather than as links to a 404 — a nav item that lies about where it
goes is worse than one that admits it is not ready.

The screen editor (Prompt 9), the factsheet (10), the dashboard and market health (11), rebalance
(14) and backtests (15) are all absent. `/kitchen-sink` is where the primitives they will use are
demonstrated and measured.

## 11. Dependencies added beyond `docs/02`'s table

`docs/02` locks the stack; these fill gaps it does not name, and are flagged per CLAUDE.md house
rule 1:

| Package | Why |
|---|---|
| `next-themes` | The no-FOUC dark mode of deliverable 6 needs a blocking pre-paint script. Hand-rolling one is a wrong-by-default inline script in `<head>`. |
| `cmdk` | shadcn/ui's own Command primitive. Supplies the roving focus and `aria-activedescendant` wiring a listbox needs. |
| `jose` | HS256 minting. Already an Auth.js dependency; using it directly avoids a second JWT library. |
| `pg` | The Postgres adapter of §2. `asyncpg` is Python's. |
| `lucide-react`, `class-variance-authority`, `clsx`, `tailwind-merge` | shadcn/ui's stated dependencies. |
| `lighthouse`, `@axe-core/playwright` | The measurement tools the acceptance criteria name. |

`eslint-config-next` was **removed**: at 15.5 it still ships only the eslintrc-era entry point,
which loads `@rushstack/eslint-patch` and refuses to run on ESLint 9.39. The rules it would have
contributed (`@next/next`, `react-hooks`) are configured directly, alongside `jsx-a11y`.

---

## Superseded by Prompt 12

§2 and §3 describe the OTP path running through a development stub because `/auth/*` did not
exist, and `auth_verification_token` as a table Prompt 12 owed. Both are done:

* `/auth/*` is built (`docs/12a`), `stub-endpoints.ts` and `DECILE_ALLOW_STUB_AUTH` are deleted,
  and the browser suite registers and signs in for real against the API.
* `auth_verification_token` exists (`docs/04c`). The adapter's two methods are satisfied by a real
  table, though nothing writes it in the normal path any more — the OTP flow is the API's.

What §3 says about the session strategy still holds: Auth.js v5 cannot use database sessions with
the Credentials provider, so the session cookie is a JWT. What changed is that the *access token*
inside it is now the one `POST /auth/login` minted, rather than one this app derived from the
shared secret (`docs/12a` §5).
