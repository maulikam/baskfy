import { NextResponse, type NextRequest } from "next/server";

import { signOut } from "@/lib/auth";

/**
 * Sign-out is a state-changing mutation, so it is POST (AUDIT 2.10). GET still exists as a
 * tiny auto-submitting form so layout redirects and the user-menu link keep working without a
 * second navigation that would leave Clear-Site-Data unset.
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
 * `Clear-Site-Data` is the header that does it properly. It is the one lever the browser honours
 * without our code running: it drops the origin's cookies, its storage and its cached documents —
 * the bfcache entries among them — as part of *this* response. So the Back button has nothing
 * left to restore, and the request it is forced to make meets the gate.
 *
 * Ignored on insecure origins, which is why it is safe to send unconditionally: in local HTTP
 * development it is a no-op and the `no-store` path still applies.
 */
export const dynamic = "force-dynamic";

const SESSION_COOKIES = [
  "authjs.session-token",
  "__Secure-authjs.session-token",
  "authjs.callback-url",
  "__Secure-authjs.callback-url",
  "authjs.csrf-token",
  "__Host-authjs.csrf-token",
];

const CLEAR_SITE_DATA = '"cache", "cookies", "storage"';

async function clearSession(): Promise<NextResponse> {
  await signOut({ redirect: false });

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

/** Auto-post form so a GET (layout redirect, menu link) still ends in a POST mutation. */
export async function GET(_request: NextRequest): Promise<NextResponse> {
  const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/><title>Signing out</title></head><body><form id="f" method="post" action="/logout"></form><script>document.getElementById("f").submit();</script></body></html>`;
  return new NextResponse(html, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
    },
  });
}

export async function POST(_request: NextRequest): Promise<NextResponse> {
  return clearSession();
}
