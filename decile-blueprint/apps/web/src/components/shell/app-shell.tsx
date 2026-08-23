"use client";

import type { ReactNode } from "react";

import { BottomTabBar } from "@/components/shell/bottom-tab-bar";
import { MAIN_CONTENT_ID } from "@/components/shell/ids";
import { SkipLink } from "@/components/shell/skip-link";
import { TopNav } from "@/components/shell/top-nav";
import type { UserMenuProps } from "@/components/shell/user-menu";
import { Disclaimer } from "@/components/data/disclaimer";

/**
 * The application frame — docs/08 §"App shell", departing from it in one deliberate way.
 *
 * docs/08 asks for a collapsible left sidebar. M36 removed it; `top-nav.tsx` carries the argument
 * and `docs/DECISIONS-MERGE.md` §M36.7 records the departure. What is left is about as little
 * frame as an application can have: one bar, one column, and the page.
 *
 * Order matters for keyboard users: the skip link is the first focusable element, then the
 * navigation, then main. `<main>` carries `tabIndex={0}` for two reasons that point the same way.
 * The skip link needs a focusable target — without one the browser scrolls but leaves focus
 * behind, and the next Tab returns to the navigation the user just skipped. And `<main>` is the
 * page's scroll container, so WCAG 2.1.1 requires it to be *reachable by Tab*, not merely
 * focusable programmatically: someone scrolling with the keyboard has to be able to put focus in
 * the region that scrolls. `tabIndex={-1}` satisfies the first and fails the second (axe
 * `scrollable-region-focusable`), which the long instrument factsheet is what surfaced.
 *
 * **The page scrolls, not an inner pane.** The old frame nested a scrolling `<main>` inside a
 * fixed-height flex column, which meant the browser's own scrollbar never moved and neither did
 * anything anchored to the viewport. With the navigation stuck to the top instead, the document
 * scrolls the way a document does.
 *
 * The disclaimer sits in the frame rather than on each page, which is what docs/11 §"Compliance"
 * and CLAUDE.md house rule 9 require — "Disclaimers are components, not footers" — and means a
 * new analytics page cannot ship without one.
 */
export interface AppShellProps {
  user: UserMenuProps;
  banner?: ReactNode;
  children: ReactNode;
}

export function AppShell({ user, banner, children }: AppShellProps) {
  return (
    <div className="min-h-dvh">
      <SkipLink />
      {banner}
      <TopNav user={user} />
      <main id={MAIN_CONTENT_ID} tabIndex={0} className="-outline-offset-2">
        {/*
          One container, one measure, everywhere. Wider than the old frame because the sidebar's
          224px came back, and capped because a row the full width of a 27-inch display is a row
          the eye loses on the way across.
        */}
        <div className="mx-auto flex w-full max-w-[104rem] flex-col gap-7 px-5 pb-24 pt-8 md:px-7 md:pb-16">
          {children}
          <Disclaimer className="mt-4 border-t border-border pt-5" />
        </div>
      </main>
      <BottomTabBar />
    </div>
  );
}
