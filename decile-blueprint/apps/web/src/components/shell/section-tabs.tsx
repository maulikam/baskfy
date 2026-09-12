"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { sectionTabIsActive } from "@/components/shell/section-tab-active";
import { useIsStaff } from "@/components/shell/staff-context";
import { buildSectionTabs, type SectionKey } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * In-page segmented control for the Market / Discover / Build / Portfolio hubs.
 * Lives under the page header, never in the global nav.
 *
 * Active tab = longest matching href — see `section-tab-active.ts` (AUDIT 1.7).
 */
export function SectionTabs({
  section,
  isStaff: isStaffProp,
}: {
  section: SectionKey;
  /** When omitted, StaffProvider (AppShell) supplies the bit. Default is civilian. */
  isStaff?: boolean;
}) {
  const pathname = usePathname();
  const isStaffFromContext = useIsStaff();
  const isStaff = isStaffProp ?? isStaffFromContext;
  const tabs = buildSectionTabs(section, { isStaff });
  const hrefs = tabs.map((tab) => tab.href);

  return (
    <nav aria-label="Section" className="mb-2">
      <ul className="inline-flex max-w-full flex-wrap gap-1 rounded-lg border border-border/70 bg-muted/40 p-1">
        {tabs.map((tab) => {
          const active = sectionTabIsActive(pathname, tab.href, hrefs);
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
