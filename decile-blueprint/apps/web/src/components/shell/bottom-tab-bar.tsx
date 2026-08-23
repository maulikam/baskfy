"use client";

import {
  LineChart,
  Store,
  User,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { PRIMARY_NAV, primarySection } from "@/lib/nav";
import { cn } from "@/lib/utils";

const ICONS: Record<string, LucideIcon> = {
  Market: LineChart,
  Baskets: Store,
  Build: Wrench,
  Me: User,
};

/**
 * App-style bottom tab bar for &lt;768px (Tree 6). Safe-area padded; active tab tinted.
 */
export function BottomTabBar() {
  const pathname = usePathname();
  const section = primarySection(pathname);

  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border/70 bg-background/95 backdrop-blur-md md:hidden"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <ul className="mx-auto flex h-14 max-w-[104rem] items-stretch justify-around px-2">
        {PRIMARY_NAV.map((item) => {
          const active =
            (section === "market" && item.label === "Market") ||
            (section === "baskets" && item.label === "Baskets") ||
            (section === "build" && item.label === "Build") ||
            (section === "me" && item.label === "Me");
          const Icon = ICONS[item.label] ?? Store;
          return (
            <li key={item.href} className="flex min-w-0 flex-1">
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex w-full flex-col items-center justify-center gap-0.5 rounded-md text-[11px] font-medium",
                  active ? "text-brand" : "text-muted-foreground",
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
