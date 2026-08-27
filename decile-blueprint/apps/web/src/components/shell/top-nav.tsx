"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { CommandPalette } from "@/components/shell/command-palette";
import { FreshnessPill } from "@/components/shell/freshness-pill";
import { PRIMARY_NAV_ID } from "@/components/shell/ids";
import { SearchButton } from "@/components/shell/search-button";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { UserMenu, type UserMenuProps } from "@/components/shell/user-menu";
import { Wordmark } from "@/components/shell/wordmark";
import { PRIMARY_NAV, SECTION_LABEL, primarySection, type ReadyNavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * Tree 6 consumer chrome: logo · 5 destinations · search · date · theme · avatar.
 * Mobile bottom tabs live in `BottomTabBar`; this header shrinks to logo · search · avatar under md.
 *
 * **The sliding pill is gone, and it was a bug as well as a mismatch.** A measured indicator read
 * `offsetLeft`/`offsetWidth` from the active link in an effect; when the face and the capsule
 * changed underneath it the measurement went stale, leaving the marker parked under the wrong item
 * and the active label — white, because it expects the marker behind it — rendered white on white
 * and simply disappeared. Painting the active state on the link itself cannot desync from the link
 * it describes, and it is what the header this product now wears actually does.
 */
export interface TopNavProps {
  user: UserMenuProps;
}

/*
  The section → label map used to be inlined here and again in `BottomTabBar`, and the two had
  drifted: the bar still said `discover: "Baskets"` after the hub was renamed, so that tab never
  lit. `SECTION_LABEL` in `lib/nav` is the one record both read now. A section with no primary
  destination (`me`, since PORTFOLIO_REDESIGN.md §2 moved the money out of it) simply matches
  nothing, which is the right answer: it is drawn in the user menu, not the pill row.
*/
function sectionHref(item: ReadyNavItem, pathname: string): boolean {
  const section = primarySection(pathname);
  if (!section) return pathname === item.href || pathname.startsWith(`${item.href}/`);
  return item.label === SECTION_LABEL[section];
}

export function TopNav({ user }: TopNavProps) {
  const pathname = usePathname();

  return (
    /*
      The same capsule the public header wears, so the product does not change costume when a
      visitor signs in: white at 85% behind a 24px blur, one soft shadow, no border, full-round.

      `sticky`, not `fixed` as the marketing header is. A fixed bar over a virtualised table means
      permanently surrendering its height on every dense page and letting rows slide underneath it;
      sticky keeps the capsule with you and still lets the document own its own flow.
    */
    <header className="sticky top-0 z-20 px-3 pb-2 pt-3 md:px-5">
      <div className="vaaya-pill-bar mx-auto flex h-[3.25rem] w-full max-w-[104rem] items-center gap-4 pl-5 pr-4">
        <Wordmark href="/home" />

        <nav
          id={PRIMARY_NAV_ID}
          aria-label="Primary"
          className="relative ml-2 hidden min-w-0 flex-1 md:block"
        >
          <ul className="flex items-center gap-1">
            {PRIMARY_NAV.map((item) => {
              const active = sectionHref(item, pathname);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "block whitespace-nowrap rounded-full px-3 py-1.5 text-[15px] transition-colors duration-150",
                      active
                        ? "bg-accent font-medium text-accent-foreground"
                        : "text-foreground/70 hover:bg-foreground/5 hover:text-foreground",
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
