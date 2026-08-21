"use client";

import { useState, type ReactNode } from "react";

import { MAIN_CONTENT_ID } from "@/components/shell/ids";
import { Sidebar } from "@/components/shell/sidebar";
import { SkipLink } from "@/components/shell/skip-link";
import { TopBar } from "@/components/shell/top-bar";
import type { UserMenuProps } from "@/components/shell/user-menu";
import { Disclaimer } from "@/components/data/disclaimer";

/**
 * The application frame — docs/08 §"App shell".
 *
 * Order matters for keyboard users: the skip link is the first focusable element, then the
 * sidebar, then the top bar, then main. `<main>` carries `tabIndex={0}` for two reasons that
 * point the same way. The skip link needs a focusable target — without one the browser scrolls
 * but leaves focus behind, and the next Tab returns to the navigation the user just skipped. And
 * `<main>` is the page's scroll container, so WCAG 2.1.1 requires it to be *reachable by Tab*,
 * not merely focusable programmatically: someone scrolling with the keyboard has to be able to
 * put focus in the region that scrolls. `tabIndex={-1}` satisfies the first and fails the second
 * (axe `scrollable-region-focusable`), which the long instrument factsheet is what surfaced.
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
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="flex h-dvh flex-col">
      <SkipLink />
      {banner}
      <div className="flex min-h-0 flex-1">
        <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((value) => !value)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar user={user} />
          <main
            id={MAIN_CONTENT_ID}
            tabIndex={0}
            /* No `outline-hidden`: now that this is a Tab stop it must show the global
               `:focus-visible` ring like every other one (docs/11 §Accessibility). */
            className="min-h-0 flex-1 overflow-y-auto px-6 py-6 -outline-offset-2"
          >
            {children}
            <Disclaimer className="mt-10 border-t border-border pt-4" />
          </main>
        </div>
      </div>
    </div>
  );
}
