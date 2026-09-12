/**
 * Where the API lives, and the contract the browser and the server share.
 *
 * docs/07: "Base: `/api/v1`". `NEXT_PUBLIC_API_URL` in `.env.example` already carries the prefix,
 * so it is stripped here: `createBaskfyClient` takes an origin and the generated paths supply the
 * rest. Doing it in one place stops `/api/v1/api/v1/...` from ever being a bug anyone has to find.
 */
import { API_PREFIX } from "@baskfy/api-client";

/** Local-only fallback. Production must set `NEXT_PUBLIC_API_URL` (AUDIT 4.6). */
const DEV_FALLBACK_ORIGIN = "http://localhost:8000";

/**
 * The one public API origin resolver. Middleware, the browser client and server fetchers
 * all call this — the retired dual env name is not read anywhere.
 */
export function apiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (!configured) {
    if (process.env.NODE_ENV === "production") {
      throw new Error("NEXT_PUBLIC_API_URL must be set in production");
    }
    return DEV_FALLBACK_ORIGIN;
  }
  const trimmed = configured.replace(/\/+$/, "");
  return trimmed.endsWith(API_PREFIX) ? trimmed.slice(0, -API_PREFIX.length) : trimmed;
}

/**
 * Where the API lives **as seen from the server process**, which is not always where it lives as
 * seen from a browser.
 *
 * ## Why this exists
 *
 * `docs/08` §3 puts `web` and `api` in one compose network behind Caddy, and the staging host is
 * gated with basic auth. A server component that fetched `https://staging.baskfy.com/api/v1/...`
 * would leave the box, resolve the public name, come back in through Caddy — and be challenged
 * for the password it does not have. Every page would render its error state behind a gate that
 * was only ever meant for humans.
 *
 * `BASKFY_INTERNAL_API_ORIGIN` (e.g. `http://api:8000`) is the same API reached directly across
 * the container network: no hairpin, no TLS handshake with itself, no gate.
 *
 * ## The rule, and why it is enforced by a test rather than by care
 *
 * **Server modules that `fetch` use this. Anything that builds a URL a browser will follow uses
 * {@link apiOrigin}.** A download link, a CSV href, the CSP's `connect-src` — those are consumed
 * by the browser, and `http://api:8000` is not a thing a browser can reach.
 *
 * The trap is a client component: it renders once on the server and again in the browser, so a
 * call to this from one would emit the internal origin into the HTML and the public one after
 * hydration — a mismatch that shows up as a broken link, not as an error.
 *
 * Two things stop that, and neither is a runtime `typeof window` check (which was tried: it is
 * false during SSR of a client component, exactly when it would be needed, and true under jsdom,
 * where it silently disabled the tests below):
 *
 * 1. **The bundler.** `BASKFY_INTERNAL_API_ORIGIN` has no `NEXT_PUBLIC_` prefix, so Next replaces
 *    it with `undefined` in every client bundle. A client module that called this would get the
 *    public origin in the browser no matter what the server is configured with.
 * 2. **The scan.** `__tests__/api-origin-split.test.ts` fails the suite if a `"use client"` file
 *    so much as mentions this function, which catches the SSR half the bundler cannot.
 *
 * Unset — every environment except the container — this is exactly {@link apiOrigin}, so local
 * development and the test suite behave as they always did.
 */
export function serverApiOrigin(): string {
  const internal = process.env.BASKFY_INTERNAL_API_ORIGIN?.trim();
  if (!internal) return apiOrigin();
  const trimmed = internal.replace(/\/+$/, "");
  return trimmed.endsWith(API_PREFIX) ? trimmed.slice(0, -API_PREFIX.length) : trimmed;
}

/** docs/07 §Conventions — echoed back on every response, and useful in a bug report. */
export const REQUEST_ID_HEADER = "X-Request-Id";
