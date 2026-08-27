"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { SECTION_TABS, type SectionKey } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * In-page segmented control for the Market / Discover / Build / Portfolio hubs.
 * Lives under the page header, never in the global nav.
 */
export function SectionTabs({ section }: { section: SectionKey }) {
  const pathname = usePathname();
  const tabs = SECTION_TABS[section];

  return (
    <nav aria-label="Section" className="mb-2">
      <ul className="inline-flex max-w-full flex-wrap gap-1 rounded-lg border border-border/70 bg-muted/40 p-1">
        {tabs.map((tab) => {
          const active =
            pathname === tab.href ||
            (tab.href !== "/discover" && pathname.startsWith(`${tab.href}/`)) ||
            (tab.href === "/discover" && pathname === "/discover");
          return (
            <li key={tab.href}>
              <Link
                href={tab.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "block whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "marker-control font-medium"
                    : "text-muted-foreground hover:bg-background hover:text-foreground",
                )}
              >
                {tab.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
