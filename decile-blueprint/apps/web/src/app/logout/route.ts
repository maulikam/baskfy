import { NextResponse, type NextRequest } from "next/server";

import { signOut } from "@/lib/auth";

/**
 * A GET route rather than a form post, because the user menu is a link. Auth.js clears the session
 * cookie and we redirect; there is no state of our own to tear down, since the access token lives
 * only inside that cookie's JWT.
 *
 * **Why this is not just `signOut({ redirectTo: "/" })` any more.**
 *
 * It used to be, and the reasoning written here was that a document navigation discards Next's
 * client router cache while `no-store` on every gated page (`src/middleware.ts`) keeps the Back
 * button honest. Both halves are still true and both are still load-bearing. What they do not
 * cover is the browser's **back/forward cache**, which is a different store with different rules:
 * Chrome 123 began admitting `Cache-Control: no-store` documents to bfcache, and it evicts such an
 * entry only when cookies change *while the entry is held*. Signing out changes the cookie on the
 * way **out** — before the page is stored — so the entry is admitted clean, and pressing Back
 * re-paints a signed-in page from memory with no request on the wire for the gate to answer.
 *
 * `SessionSentinel` catches that on `pageshow`, but catching it means the signed-in page has
 * already been painted and the person watches it disappear. A control that depends on the app's
 * own JavaScript surviving is also the wrong place to put the last line of a sign-out.
 *
 * `Clear-Site-Data` is the header that does it properly. It is the one lever the browser honours
 * without our code running: it drops the origin's cookies, its storage and its cached documents —
 * the bfcache entries among them — as part of *this* response. So the Back button has nothing
 * left to restore, and the request it is forced to make meets the gate.
 *
 * Ignored on insecure origins, which is why it is safe to send unconditionally: in local HTTP
 * development it is a no-op and the `no-store` path still applies.
 *
 * `no-store` on this response is the second thing that changed. A cached sign-out is a sign-out
 * that happens once, and this response was previously the only one in the gated flow carrying no
 * cache directive at all — `/logout` is on the public list, so the middleware's `denyStorage`
 * never saw it.
 *
 * The cookies are deleted twice on purpose: once by Auth.js through `next/headers`, and once
 * explicitly on the response below. The second is not redundant defence-in-depth theatre — it is
 * what makes the deletion legible in the response headers, so a reviewer reading a captured
 * sign-out can see the cookie die rather than trust that a framework did it.
 */
export const dynamic = "force-dynamic";

/**
 * Both spellings Auth.js uses. The `__Secure-` prefix appears on HTTPS deployments; the bare name
 * on plain HTTP local development. Deleting the one that is not set costs a header and nothing
 * else, and hard-coding both means a deployment that changes scheme cannot leave one behind.
 */
const SESSION_COOKIES = [
  "authjs.session-token",
  "__Secure-authjs.session-token",
  "authjs.callback-url",
  "__Secure-authjs.callback-url",
  "authjs.csrf-token",
  "__Host-authjs.csrf-token",
];

/**
 * `"cache"` is the one that reaches the back/forward cache; `"cookies"` is the credential itself;
 * `"storage"` covers `localStorage`, `sessionStorage`, IndexedDB and the Cache API, none of which
 * this app puts a session in today — but "today" is the word doing the work, and a sign-out that
 * has to be revisited every time a feature adds a client-side store is a sign-out that will be
 * wrong at some point.
 *
 * `"executionContexts"` is deliberately **not** in the list. It asks the browser to reload the
 * browsing context, which on a response that is already a redirect is a second navigation racing
 * the first; Chrome does not implement it, and the browsers that do would gain nothing here.
 */
const CLEAR_SITE_DATA = '"cache", "cookies", "storage"';

export async function GET(_request: NextRequest): Promise<NextResponse> {
  /*
   * `redirect: false` so the redirect is ours to build. `signOut` with a `redirectTo` throws a
   * `NEXT_REDIRECT` that Next turns into a bare 307 — a response we never get to put a header on,
   * which is exactly what this route now needs to do.
   */
  await signOut({ redirect: false });

  /*
   * A **relative** `Location`, and this is the whole of the fix for a real bug: signing out on the
   * deployed box sent the browser to `https://0.0.0.0:3000/`.
   *
   * This used to be `new URL("/", request.nextUrl.origin)`. Inside the container the Next server
   * binds to `0.0.0.0:3000`, and a route handler running in the Node runtime resolves
   * `nextUrl.origin` from that bind address rather than from the host the browser asked for —
   * Caddy terminates TLS and proxies onward, so by the time this code runs the public origin is
   * only in a forwarded header. The middleware never had the bug because Next emits its
   * same-origin redirects as relative paths already (`/login?next=%2Fhome` on the live site).
   *
   * RFC 7231 §7.1.2 permits a relative reference in `Location` and every browser resolves it
   * against the request URL — which is the public one, because that is what the browser asked
   * for. So this is correct on the box, on a laptop, and behind any future proxy, without reading
   * an environment variable or trusting a header that a client can set.
   *
   * `NextResponse.redirect` insists on an absolute URL, so the response is built directly.
   *
   * 303, not 307. The browser must issue a plain GET for the destination; 307 preserves the
   * method, and while this route is a GET today, a sign-out that silently replays its method if
   * the trigger ever becomes a form post is a trap laid for a future change.
   */
  const response = new NextResponse(null, { status: 303, headers: { location: "/" } });

  response.headers.set("clear-site-data", CLEAR_SITE_DATA);
  response.headers.set("cache-control", "no-store, no-cache, must-revalidate, max-age=0");
  response.headers.set("pragma", "no-cache");
  response.headers.set("expires", "0");
  for (const name of SESSION_COOKIES) {
    response.cookies.delete(name);
  }

  return response;
}
