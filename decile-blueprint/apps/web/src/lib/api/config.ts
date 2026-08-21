/**
 * Where the API lives, and the contract the browser and the server share.
 *
 * docs/07: "Base: `/api/v1`". `NEXT_PUBLIC_API_URL` in `.env.example` already carries the prefix,
 * so it is stripped here: `createDecileClient` takes an origin and the generated paths supply the
 * rest. Doing it in one place stops `/api/v1/api/v1/...` from ever being a bug anyone has to find.
 */
import { API_PREFIX } from "@baskfy/api-client";

const FALLBACK_ORIGIN = "http://localhost:8000";

export function apiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL ?? FALLBACK_ORIGIN;
  const trimmed = configured.replace(/\/+$/, "");
  return trimmed.endsWith(API_PREFIX) ? trimmed.slice(0, -API_PREFIX.length) : trimmed;
}

/** docs/07 §Conventions — echoed back on every response, and useful in a bug report. */
export const REQUEST_ID_HEADER = "X-Request-Id";
