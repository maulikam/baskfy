"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CommandPalette } from "@/components/shell/command-palette";
import { FreshnessPill } from "@/components/shell/freshness-pill";
import { PRIMARY_NAV_ID } from "@/components/shell/ids";
import { SearchButton } from "@/components/shell/search-button";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { UserMenu, type UserMenuProps } from "@/components/shell/user-menu";
import { Wordmark } from "@/components/shell/wordmark";
import { PRIMARY_NAV, primarySection, type ReadyNavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * Tree 6 consumer chrome: logo · 4 sliding-pill destinations · search · date · theme · avatar.
 * Mobile bottom tabs live in `BottomTabBar`; this header shrinks to logo · search · avatar under md.
 */
export interface TopNavProps {
  user: UserMenuProps;
}

function sectionHref(item: ReadyNavItem, pathname: string): boolean {
  const section = primarySection(pathname);
  if (!section) return pathname === item.href || pathname.startsWith(`${item.href}/`);
  const map = {
    market: "Market",
    baskets: "Baskets",
    build: "Build",
    me: "Me",
  } as const;
  return item.label === map[section];
}

export function TopNav({ user }: TopNavProps) {
  const pathname = usePathname();
  const listRef = useRef<HTMLUListElement>(null);
  const [pill, setPill] = useState<{ left: number; width: number } | null>(null);

  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const active = list.querySelector<HTMLElement>("[data-active-pill='true']");
    if (!active) {
      setPill(null);
      return;
    }
    setPill({ left: active.offsetLeft, width: active.offsetWidth });
  }, [pathname]);

  return (
    <header className="sticky top-0 z-20 border-b border-border/70 bg-background/85 backdrop-blur-md">
      <div className="mx-auto flex h-14 w-full max-w-[104rem] items-center gap-4 px-5 md:px-7">
        <Wordmark />

        <nav
          id={PRIMARY_NAV_ID}
          aria-label="Primary"
          className="relative ml-2 hidden min-w-0 flex-1 md:block"
        >
          <ul ref={listRef} className="relative flex items-center gap-1">
            {pill ? (
              <li
                aria-hidden="true"
                className="pointer-events-none absolute top-0 h-full rounded-md marker-control transition-[left,width] duration-200 ease-out"
                data-active-pill="track"
                style={{ left: pill.left, width: pill.width }}
              />
            ) : null}
            {PRIMARY_NAV.map((item) => {
              const active = sectionHref(item, pathname);
              return (
                <li key={item.href} className="relative z-[1]">
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    data-active-pill={active ? "true" : undefined}
                    className={cn(
                      "relative block whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors duration-150",
                      active
                        ? "font-medium text-brand-foreground"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <SearchButton />
          <FreshnessPill className="hidden sm:inline-flex" />
          <ThemeToggle />
          <UserMenu {...user} />
        </div>
      </div>

      <CommandPalette />
    </header>
  );
}
