import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { Suspense, type ReactNode } from "react";

import { Providers } from "@/app/providers";
import { cookieName } from "@/components/shell/announcement";
import { AnnouncementBanner } from "@/components/shell/announcement-banner";
import { SessionSentinel } from "@/components/auth/session-sentinel";
import { AppShell } from "@/components/shell/app-shell";
import { RscListFallback } from "@/components/shell/rsc-list-fallback";
import { auth } from "@/lib/auth";
import { isPublicPath } from "@/lib/auth/public-routes";
import { fetchMe } from "@/lib/auth/me";

/** Change this when the message changes; a dismissal is remembered against this id. */
const ANNOUNCEMENT_ID = "dec-2026-update";

/**
 * The authenticated frame. Everything under `(app)` renders inside the shell of docs/08 §"App
 * shell", including the announcement slot it asks for.
 *
 * The session and the banner's dismissal cookie are both read here, once per request, and passed
 * down. Reading either in the client would cost a layout shift on every page load — see
 * `AnnouncementBanner` for the measurement.
 *
 * **It is also where the gate is actually enforced.** `src/middleware.ts` redirects on the
 * *presence* of a session cookie, which is all an edge function can cheaply do — it never opens
 * the cookie, so an expired or forged one walks past it. `auth()` here verifies the token, and
 * this layout wraps every page in the group, so there is no route under `(app)` that can be
 * reached without passing through it. The middleware is the fast path; this is the truth.
 *
 * `/pricing` is the exception, and the reason the check consults the public list rather than
 * simply demanding a session: it renders in the app shell but has to be readable by somebody
 * deciding whether to sign up at all.
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const [session, headerList] = await Promise.all([auth(), headers()]);

  /* Set by the middleware, which is the only thing in a Next request that knows the URL and can
     hand it to a layout. Absent means something reached this render without the middleware, and
     the safe reading of "I do not know which page this is" is "treat it as gated". */
  const pathname = headerList.get("x-pathname") ?? "";
  if (!session?.user && !isPublicPath(pathname)) {
    const next = `${pathname || "/"}${headerList.get("x-search") ?? ""}`;
    redirect(`/login?next=${encodeURIComponent(next)}`);
  }

  const [cookieStore, me] = await Promise.all([cookies(), fetchMe()]);
  /* The CSP nonce. Read here rather than in the root layout, which must stay request-free so
     the `(marketing)` group can be statically generated (`src/app/layout.tsx`). */
  const nonce = headerList.get("x-nonce") ?? undefined;
  const bannerDismissed = cookieStore.get(cookieName(ANNOUNCEMENT_ID)) !== undefined;

  return (
    <Providers nonce={nonce}>
      <AppShell
        user={{
          email: session?.user?.email ?? null,
          name: session?.user?.name ?? null,
          // Server truth, from `GET /me` — the same place the entitlement gates read from. A
          // signed-out visitor gets `null` and no extra request (`fetchMe` short-circuits).
          isStaff: me?.is_staff ?? false,
        }}
        banner={
          bannerDismissed ? null : (
            <AnnouncementBanner id={ANNOUNCEMENT_ID} action={{ label: "Read the December 2026 update", href: "/blog" }}>
              <strong className="font-medium">December 2026 update:</strong> split- and
              bonus-adjusted history, a longer backfill, and backtests are on the way.
            </AnnouncementBanner>
          )
        }
      >
        {/* Tree-5: stream page RSC after shell — page data hops are timeout-capped. */}
        <Suspense fallback={<RscListFallback label="Loading page…" />}>{children}</Suspense>
      </AppShell>
      {/* Only for a session that exists: on a public page like `/pricing` there is nothing to
          notice the loss of, and a sentinel there would bounce an anonymous reader to `/login`. */}
      {session?.user ? <SessionSentinel /> : null}
    </Providers>
  );
}
