"use client";

import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

/**
 * Overview / Constituents / Versions tabs for `/basket/[slug]` (AF I.1 wiring).
 */

const TABS = [
  { suffix: "", label: "Overview", match: "overview" },
  { suffix: "/constituents", label: "Constituents", match: "constituents" },
  { suffix: "/versions", label: "Versions", match: "versions" },
] as const;

export function BasketSectionNav({ slug }: { slug: string }) {
  const pathname = usePathname();
  const base = `/basket/${slug}`;
  const active =
    pathname.endsWith("/versions") || pathname.includes("/versions?")
      ? "versions"
      : pathname.endsWith("/constituents")
        ? "constituents"
        : "overview";

  return (
    <nav aria-label="Basket sections" className="flex flex-wrap gap-2 text-sm">
      {TABS.map((tab) => {
        const href = `${base}${tab.suffix}` as Route;
        const isActive = active === tab.match;
        return (
          <Link
            key={tab.match}
            href={href}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
              isActive
                ? "border-accent bg-accent-muted text-accent"
                : "border-border/70 bg-card text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
