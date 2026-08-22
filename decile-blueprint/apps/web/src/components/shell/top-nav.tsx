"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { CommandPalette } from "@/components/shell/command-palette";
import { FreshnessPill } from "@/components/shell/freshness-pill";
import { PRIMARY_NAV_ID } from "@/components/shell/ids";
import { SearchButton } from "@/components/shell/search-button";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { UserMenu, type UserMenuProps } from "@/components/shell/user-menu";
import { Wordmark } from "@/components/shell/wordmark";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { NAV_GROUPS, type NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * The whole of the application's navigation, along the top (M36).
 *
 * ## Why there is no left sidebar any more
 *
 * docs/08 §"App shell" asks for a "Collapsible left sidebar (**matching the reference IA**)", and
 * the emphasis is the point: the sidebar was there because the product being reproduced had one.
 * A 224px rail of icons down the left, a strip of chrome across the top, and a dense table filling
 * what is left is not this product's design — it is the shape every screener has, which is exactly
 * what Maulik's note said when he looked at it: *"it shows that we have copied somebody else's
 * screen as it is."*
 *
 * Removing it buys three things:
 *
 * 1. **~15% of the window back**, all of it horizontal, which is the axis a table needs.
 * 2. **One row of navigation instead of fifteen.** Seven destinations sit in a row you read in a
 *    second, and the five desk pages — which answer a different kind of question — collapse into
 *    one menu rather than sprawling as a second block of the same weight.
 * 3. **A shape that is ours.** This is the most visible departure from docs/08 in the whole merge,
 *    and it is deliberate; `docs/DECISIONS-MERGE.md` §M36.7 records it and how to reverse it.
 *
 * Account and Help moved into the user menu, where settings belong.
 *
 * ## The active marker
 *
 * The mark's own orange, and it is the only place in the chrome that carries colour. Ink on
 * `--brand` is 6.42:1, so the active tab is among the highest-contrast things on the page — which
 * is what you want the "where am I" signal to be. It carries a hairline and `aria-current` as
 * well, because the fill alone sits just under WCAG's 3:1 for a graphical boundary.
 */
export interface TopNavProps {
  user: UserMenuProps;
}

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function Tab({ item, active }: { item: NavItem; active: boolean }) {
  const body = (
    <span
      className={cn(
        "relative block whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm transition-colors duration-150",
        active
          ? "marker-control font-medium"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      {item.label}
    </span>
  );

  if (item.status === "planned") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span aria-disabled="true" className="cursor-not-allowed opacity-50">
            {body}
          </span>
        </TooltipTrigger>
        <TooltipContent>Arrives in {item.arrivesIn}.</TooltipContent>
      </Tooltip>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link href={item.href} aria-current={active ? "page" : undefined}>
          {body}
        </Link>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-[16rem] space-y-1 text-left">
        <p className="text-xs leading-relaxed">{item.blurb}</p>
        {item.formerly ? (
          <p className="text-xs opacity-70">Elsewhere called “{item.formerly}”.</p>
        ) : null}
      </TooltipContent>
    </Tooltip>
  );
}

export function TopNav({ user }: TopNavProps) {
  const pathname = usePathname();
  const [primary, desk] = NAV_GROUPS;
  const deskActive = desk?.items.some((item) => isActive(pathname, item.href)) ?? false;

  return (
    <header className="sticky top-0 z-20 border-b border-border/70 bg-background/85 backdrop-blur-md">
      <div className="mx-auto flex h-14 w-full max-w-[104rem] items-center gap-4 px-5 md:px-7">
        <Wordmark />
        <div className="ml-auto flex items-center gap-2">
          <SearchButton />
          <FreshnessPill className="hidden sm:inline-flex" />
          <ThemeToggle />
          <UserMenu {...user} />
        </div>
      </div>

      {/*
        A second row rather than one crowded one. Overflow scrolls rather than wrapping, so the
        bar is always exactly one line tall and the page below it never moves.
      */}
      <nav
        id={PRIMARY_NAV_ID}
        aria-label="Main"
        className="mx-auto w-full max-w-[104rem] px-5 md:px-7"
      >
        <ul className="flex items-center gap-1 overflow-x-auto pb-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {primary?.items.map((item) => (
            <li key={item.href}>
              <Tab item={item} active={isActive(pathname, item.href)} />
            </li>
          ))}

          {desk ? (
            <li className="ml-1 border-l border-border/70 pl-2">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className={cn(
                      "h-auto gap-1 rounded-md px-2.5 py-1.5 text-sm font-normal",
                      deskActive ? "marker-control font-medium" : "text-muted-foreground",
                    )}
                  >
                    {desk.label}
                    <ChevronDown aria-hidden="true" className="size-3.5 opacity-60" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-64">
                  {/* `status === "ready"` is what narrows `href` to a checked `Route`: a planned
                      item's href is a plain string precisely so it cannot be linked to. */}
                  {desk.items.map((item) =>
                    item.status === "ready" ? (
                      <DropdownMenuItem key={item.href} asChild>
                        <Link href={item.href} className="flex-col items-start gap-0.5">
                          <span className="font-medium text-foreground">{item.label}</span>
                          <span className="text-xs leading-snug text-muted-foreground">
                            {item.blurb}
                          </span>
                        </Link>
                      </DropdownMenuItem>
                    ) : null,
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </li>
          ) : null}
        </ul>
      </nav>

      <CommandPalette />
    </header>
  );
}
