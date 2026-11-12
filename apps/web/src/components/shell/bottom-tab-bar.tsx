"use client";

import {
  Bookmark,
  Briefcase,
  Compass,
  House,
  LineChart,
  Store,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { DeskMenu } from "@/components/shell/desk-menu";
import { PRIMARY_NAV, primaryNavLabel } from "@/lib/nav";
import { cn } from "@/lib/utils";

/*
  Keyed by the primary label. `Baskets` and `Me` are gone as destinations — the hub was renamed
  Discover, and PORTFOLIO_REDESIGN.md §2 replaced Me with Portfolio — and `Baskets` is kept only
  because it is still the fallback shape for anything unmapped.
*/
const ICONS: Record<string, LucideIcon> = {
  Home: House,
  Market: LineChart,
  Discover: Compass,
  Baskets: Store,
  Build: Wrench,
  Portfolio: Briefcase,
  Watchlist: Bookmark,
};

/**
 * App-style bottom tab bar for &lt;768px (Tree 6; six tabs since Watchlist, 12 Nov 2026).
 * Safe-area padded; active tab tinted. The row
 * is `justify-around` on a flex track, so the tabs narrow rather than overflow.
 */
export function BottomTabBar({ showDesk = true }: { showDesk?: boolean }) {
  const pathname = usePathname();
  const label = primaryNavLabel(pathname, showDesk);

  return (
    <nav
      aria-label="Primary"
      data-testid="bottom-tab-bar"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border/70 bg-background/95 backdrop-blur-md md:hidden"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <ul className="mx-auto flex h-14 max-w-[104rem] items-stretch justify-around px-2">
        {PRIMARY_NAV.map((item) => {
          const active = label !== null && item.label === label;
          const Icon = ICONS[item.label] ?? Store;
          return (
            <li key={item.href} className="flex min-w-0 flex-1">
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex w-full flex-col items-center justify-center gap-0.5 rounded-md text-[11px] font-medium",
                  // The label is type: --brand measures 3.27:1 on the canvas and type needs
                  // 4.5:1, so the accent token carries the same hue at 5.31:1
                  // (src/lib/__tests__/no-brand-as-text.test.ts). The icon's --brand fill below
                  // is a graphical object at 3:1 and stays as it is.
                  active ? "text-accent" : "text-muted-foreground",
                )}
              >
                <span
                  className={cn(
                    "grid size-8 place-items-center rounded-full",
                    active && "bg-brand/15",
                  )}
                >
                  <Icon
                    aria-hidden="true"
                    className={cn("size-5", active && "fill-brand/20")}
                    strokeWidth={active ? 2.25 : 1.75}
                  />
                </span>
                {item.label}
              </Link>
            </li>
          );
        })}
        {showDesk ? (
          <li className="flex min-w-0 flex-1">
            <DeskMenu variant="tab" align="end" />
          </li>
        ) : null}
      </ul>
    </nav>
  );
}
