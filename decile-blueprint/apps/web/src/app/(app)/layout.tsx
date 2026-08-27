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
import { isSessionRevoked } from "@/lib/auth/session-epoch";
import { fetchMe } from "@/lib/auth/me";
import { visibleAnnouncement } from "@/lib/marketing/announcement";

/*
 * The announcement moved to `lib/marketing/announcement.ts`, where it carries an expiry date.
 * It was JSX here, hard-coded, and it rotted: a header served in August announced a December
 * update and called three shipped features "on the way". Prose cannot be linted; a date can.
 */

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

  /*
   * **The kill switch, and the only one a `jwt`-strategy session has.**
   *
   * `auth()` above proves the cookie is well-formed and unexpired. It cannot prove the session is
   * still *wanted*, because the cookie is self-contained: nothing this server does can reach
   * inside a token already in somebody's browser. Before `session_epoch` existed that was the
   * whole story — signing out deleted the browser's copy of a credential that stayed good for
   * thirty days, and `revoke_all_for_user` revoked refresh-token rows the web app has never read.
   * A password change did not evict whoever prompted it (`NEEDS-MAULIK.md` §22).
   *
   * The epoch closes it here rather than in the middleware because this is where the API answer
   * already is: `fetchMe()` is awaited on every gated render regardless, so the check costs no
   * extra round trip. The middleware sees only a cookie and could not do this without one.
   *
   * Three deliberate choices in the condition:
   *
   * - **`me !== null` guards it.** `fetchMe` returns null for an unreachable API as well as for a
   *   dead session, and throwing somebody out because the network hiccuped is a worse failure than
   *   rendering a page a moment longer than we should — the same trade `SessionSentinel` makes,
   *   for the same reason: every read behind this page is authorised again by the API.
   * - **`??  0`** for an unstamped session. A cookie issued before this shipped carries no epoch,
   *   and migration 0025 backfilled every existing account to 0; reading a missing stamp as
   *   generation zero therefore agrees with the database instead of signing the entire userbase
   *   out on deploy.
   * - **`>`, not `!==`.** A stamp *ahead* of the server can only mean a rolled-back database or a
   *   replica that has not caught up, and evicting a legitimate session over replication lag is
   *   the wrong way to be wrong. Only the server having moved past this session revokes it.
   *
   * The redirect goes to `/logout`, not `/login`: `/logout` is what actually clears the cookie and
   * sends `Clear-Site-Data`. Sending a revoked session to `/login` would leave the dead cookie in
   * the browser, and the next navigation would arrive here and be refused all over again.
   */
  if (
    session?.user &&
    me !== null &&
    isSessionRevoked({ stamped: session.sessionEpoch, current: me.session_epoch })
  ) {
    redirect("/logout");
  }
  /* The CSP nonce. Read here rather than in the root layout, which must stay request-free so
     the `(marketing)` group can be statically generated (`src/app/layout.tsx`). */
  const nonce = headerList.get("x-nonce") ?? undefined;
  const announcement = visibleAnnouncement(new Date());
  const bannerDismissed =
    announcement !== null && cookieStore.get(cookieName(announcement.id)) !== undefined;

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
          announcement === null || bannerDismissed ? null : (
            <AnnouncementBanner
              id={announcement.id}
              action={{ ...announcement.action }}
            >
              <strong className="font-medium">{announcement.lead}</strong> {announcement.body}
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
