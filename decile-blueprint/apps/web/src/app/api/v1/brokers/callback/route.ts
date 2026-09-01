import { NextResponse } from "next/server";

import { auth } from "@/lib/auth";
import { serverApiOrigin } from "@/lib/api/config";

/**
 * Where Kite lands the browser after a broker login.
 *
 * **Why a route handler and not the API's own callback.** Zerodha's registered redirect points at
 * `/api/v1/brokers/callback`, and until the desk hop was removed (M70) no browser ever arrived
 * there — bare returns were forwarded to the desk and only API clients called the route. Now Kite
 * redirects a *browser*, and the API's callback takes `AuthenticatedDep`, which reads
 * `Authorization: Bearer` and nothing else. A browser carries a session cookie, never that
 * header, so a real Kite return could only ever answer 401 — which is exactly what it did.
 *
 * Two ways to fix that, and this is the second: teach the API to accept a cookie, or let the web
 * app — which already owns the browser session — receive the redirect and call the API properly.
 * The endpoint that redeems a live broker credential keeps its bearer-only auth model untouched,
 * which is the reason to prefer this. Caddy routes this one path here instead of to the API.
 *
 * `state` is required and deliberately not defaulted. Kite echoes back whatever the authorize URL
 * sent, so a login started from Baskfy's Connect button carries one and a login started anywhere
 * else does not. Passing it through unchecked would remove the CSRF protection the API's callback
 * exists to enforce; a login that never came from here is refused with a message saying so.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function outcome(params: Record<string, string>): string {
  return `/brokers?${new URLSearchParams(params).toString()}`;
}

export async function GET(request: Request): Promise<Response> {
  const query = new URL(request.url).searchParams;
  const requestToken = query.get("request_token");
  const state = query.get("state");

  // Kite reports its own failures here too (`status=error`), and they arrive with no token.
  if (!requestToken) {
    return redirectTo(
      request,
      outcome({
        connected: "0",
        reason: query.get("status") === "error" ? "kite-refused" : "no-request-token",
      }),
    );
  }

  if (!state) {
    // A login Baskfy did not start: signing in at Kite directly, or a stale bookmark. The token
    // is real but unusable, and saying so beats a 422 from a validator the reader cannot see.
    return redirectTo(request, outcome({ connected: "0", reason: "not-started-here" }));
  }

  const session = await auth();
  const token = session?.accessToken;
  if (!token) {
    // The session expired while the reader was at Kite. Their request_token dies unused, which
    // is the safe direction: it is single-use and minutes-lived either way.
    return redirectTo(request, outcome({ connected: "0", reason: "signed-out" }));
  }

  const upstream = new URL("/api/v1/brokers/callback", serverApiOrigin());
  upstream.searchParams.set("request_token", requestToken);
  upstream.searchParams.set("state", state);

  let response: Response;
  try {
    response = await fetch(upstream, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch {
    // The API is the only thing that can redeem the token, and it is unreachable. Do not retry:
    // a request_token is single-use, and a second attempt on a call that may have succeeded is
    // how one login becomes two sessions.
    return redirectTo(request, outcome({ connected: "0", reason: "api-unreachable" }));
  }

  if (!response.ok) {
    // 503 is the API refusing a login it cannot complete (leaf 1.1.4) — a missing secret or
    // DRY_RUN. Distinguished from a genuine rejection so the reader is told which.
    return redirectTo(
      request,
      outcome({ connected: "0", reason: response.status === 503 ? "not-configured" : "refused" }),
    );
  }

  return redirectTo(request, outcome({ connected: "1" }));
}

/** Absolute, because `NextResponse.redirect` requires one and typed routes reject a bare path.
 *
 *  Built from the FORWARDED headers, not from `request.url`. Behind Caddy this handler is reached
 *  as `web:3000`, so `new URL(request.url).origin` is the container's own address — which is how
 *  a real login came back to `https://0.0.0.0:3000/brokers?...`. Caddy sets `X-Forwarded-Proto`
 *  and `X-Forwarded-Host`; `NEXT_PUBLIC_SITE_URL` is the fallback for a context that has neither,
 *  and the request origin the last resort so local `next dev` still works. */
function redirectTo(request: Request, path: string): Response {
  const headers = request.headers;
  const forwardedHost = headers.get("x-forwarded-host") ?? headers.get("host");
  const forwardedProto = headers.get("x-forwarded-proto");
  const origin =
    forwardedHost && forwardedProto
      ? `${forwardedProto}://${forwardedHost}`
      : (process.env.NEXT_PUBLIC_SITE_URL ?? new URL(request.url).origin);
  return NextResponse.redirect(new URL(path, origin));
}
