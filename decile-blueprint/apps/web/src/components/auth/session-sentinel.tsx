"use client";

import { useEffect } from "react";

/**
 * The half of the gate that HTTP headers cannot reach.
 *
 * The server-side gate (`src/middleware.ts` + `src/app/(app)/layout.tsx`) decides every *request*.
 * The problem is that after a sign-out the browser can put a signed-in page back on the screen
 * without making one, and there are two ways it does that:
 *
 * 1. **The back/forward cache.** Press Back and the browser re-paints the previous document from
 *    memory — DOM, scroll position and all. `Cache-Control: no-store` on every gated response
 *    disqualifies a page from bfcache in Chrome and Firefox; Safari has historically stored
 *    `no-store` pages anyway. So `pageshow` with `event.persisted` — which fires *only* on a
 *    bfcache restore — forces a reload, and the reload meets the gate.
 *
 * 2. **Next's client router cache.** A soft navigation renders an RSC payload the router already
 *    holds. `router.back()`, or a `popstate` inside a single document, can therefore re-mount a
 *    page whose payload was fetched while the session was alive. Nothing on the wire can fix
 *    that, because nothing goes on the wire.
 *
 * Both are answered the same way: ask the server whether the session is still there, and if it is
 * not, leave. `location.replace` rather than `assign`, so the dead page does not become another
 * history entry to go back to.
 *
 * Mounted only when the server rendered a session (`(app)/layout.tsx`), so "no session" here
 * always means *lost*, never *never had one*.
 */

/** Auth.js's own probe. Returns `{}` — no `user` key — once the cookie is gone. */
const SESSION_ENDPOINT = "/api/auth/session";

export function SessionSentinel(): null {
  useEffect(() => {
    let stopped = false;

    const leave = (): void => {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.replace(`/login?next=${encodeURIComponent(next)}`);
    };

    const check = async (): Promise<void> => {
      let session: { user?: unknown } | null;
      try {
        const response = await fetch(SESSION_ENDPOINT, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { accept: "application/json" },
        });
        // A 500 or a captive portal is not a sign-out. Throwing somebody out of the app because
        // the network hiccuped is a worse failure than showing a page a moment longer than we
        // should — the API authorises every read against the bearer token regardless.
        if (!response.ok) return;
        session = (await response.json()) as { user?: unknown } | null;
      } catch {
        return;
      }
      if (!stopped && !session?.user) leave();
    };

    const onPageShow = (event: PageTransitionEvent): void => {
      // A bfcache restore hands back a document whose JavaScript state is as stale as its pixels.
      // Reloading is the only honest thing to do with it.
      if (event.persisted) window.location.reload();
    };

    const onPopState = (): void => void check();

    const onVisibilityChange = (): void => {
      // Signing out in another tab should not leave this one showing an account's holdings.
      if (document.visibilityState === "visible") void check();
    };

    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("popstate", onPopState);
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      stopped = true;
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("popstate", onPopState);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  return null;
}
