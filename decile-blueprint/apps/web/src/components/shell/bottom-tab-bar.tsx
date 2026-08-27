"use client";

import {
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

import { PRIMARY_NAV, SECTION_LABEL, primarySection } from "@/lib/nav";
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
};

/**
 * App-style bottom tab bar for &lt;768px (Tree 6, five tabs since SC9's `/home`).
 * Safe-area padded; active tab tinted. Five is what the observed product carries too — the row
 * is `justify-around` on a flex track, so the tabs narrow rather than overflow.
 */
export function BottomTabBar() {
  const pathname = usePathname();
  const section = primarySection(pathname);

  return (
    <nav
      aria-label="Primary"
      data-testid="bottom-tab-bar"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border/70 bg-background/95 backdrop-blur-md md:hidden"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <ul className="mx-auto flex h-14 max-w-[104rem] items-stretch justify-around px-2">
        {PRIMARY_NAV.map((item) => {
          // One record, shared with `TopNav` — the copy that lived here had gone stale against
          // the Discover rename and silently stopped lighting that tab.
          const active = section !== null && item.label === SECTION_LABEL[section];
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
      </ul>
    </nav>
  );
}
