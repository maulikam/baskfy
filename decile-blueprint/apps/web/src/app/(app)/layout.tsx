import { cookies, headers } from "next/headers";
import type { ReactNode } from "react";

import { Providers } from "@/app/providers";
import { cookieName } from "@/components/shell/announcement";
import { AnnouncementBanner } from "@/components/shell/announcement-banner";
import { AppShell } from "@/components/shell/app-shell";
import { auth } from "@/lib/auth";
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
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const [session, cookieStore, headerList, me] = await Promise.all([
    auth(),
    cookies(),
    headers(),
    fetchMe(),
  ]);
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
        {children}
      </AppShell>
    </Providers>
  );
}
