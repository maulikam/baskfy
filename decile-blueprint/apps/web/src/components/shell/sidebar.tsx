"use client";

import {
  Activity,
  CircleHelp,
  History,
  KeyRound,
  LayoutDashboard,
  LifeBuoy,
  List,
  Newspaper,
  PanelLeftClose,
  PanelLeftOpen,
  Receipt,
  Briefcase,
  Scale,
  Table2,
  Tag,
  User,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { SIDEBAR_ID } from "@/components/shell/ids";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { NAV_GROUPS, type NavIconName, type NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"App shell": "Collapsible left sidebar (matching the reference IA)".
 *
 * The groups and their order live in `src/lib/nav.ts`, where a test pins them against the
 * document; this file only renders them. Items whose route arrives in a later prompt render as
 * disabled buttons with a tooltip saying which one — a nav link that goes to a 404 is worse than
 * one that admits it is not ready.
 *
 * Collapsed, the labels are hidden but the icons keep their accessible names, so the rail is still
 * navigable by keyboard and by screen reader. The width is a CSS variable on the wrapper so the
 * transition is a single layout change rather than one per item.
 */
const ICONS: Record<NavIconName, LucideIcon> = {
  "layout-dashboard": LayoutDashboard,
  activity: Activity,
  "table-2": Table2,
  scale: Scale,
  briefcase: Briefcase,
  history: History,
  list: List,
  tag: Tag,
  receipt: Receipt,
  user: User,
  "key-round": KeyRound,
  "circle-help": CircleHelp,
  newspaper: Newspaper,
  "life-buoy": LifeBuoy,
};

export interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
}

function itemClasses(active: boolean, collapsed: boolean): string {
  return cn(
    "flex h-9 items-center gap-3 rounded-md px-2 text-sm transition-colors duration-100",
    collapsed && "justify-center px-0",
    active ? "bg-accent-muted font-medium text-accent" : "text-muted-foreground hover:bg-muted",
  );
}

function NavEntry({
  item,
  collapsed,
  active,
}: {
  item: NavItem;
  collapsed: boolean;
  active: boolean;
}) {
  const Icon = ICONS[item.icon];
  const body = (
    <>
      <Icon aria-hidden="true" className="size-4 shrink-0" />
      <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
    </>
  );

  if (item.status === "planned") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            disabled
            aria-disabled="true"
            className={cn(itemClasses(false, collapsed), "w-full cursor-not-allowed opacity-60")}
          >
            {body}
          </button>
        </TooltipTrigger>
        <TooltipContent side="right">
          {item.label} arrives in {item.arrivesIn}.
        </TooltipContent>
      </Tooltip>
    );
  }

  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={itemClasses(active, collapsed)}
    >
      {body}
    </Link>
  );
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const pathname = usePathname();

  return (
    <nav
      id={SIDEBAR_ID}
      aria-label="Main"
      data-collapsed={collapsed}
      className={cn(
        "flex shrink-0 flex-col gap-1 border-r border-border bg-card px-2 py-3 transition-[width] duration-100",
        collapsed ? "w-14" : "w-56",
      )}
    >
      <div className={cn("flex items-center px-1 pb-2", collapsed ? "justify-center" : "justify-between")}>
        {collapsed ? null : (
          <Link href="/" className="px-1 text-sm font-semibold tracking-tight">
            Baskfy
          </Link>
        )}
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-expanded={!collapsed}
          aria-controls={SIDEBAR_ID}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={onToggle}
        >
          {collapsed ? (
            <PanelLeftOpen aria-hidden="true" />
          ) : (
            <PanelLeftClose aria-hidden="true" />
          )}
        </Button>
      </div>

      {NAV_GROUPS.map((group, index) => (
        <div key={group.label ?? `group-${index}`} className="space-y-0.5">
          {group.label ? (
            <p
              className={cn(
                "px-2 pb-1 pt-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground",
                collapsed && "sr-only",
              )}
            >
              {group.label}
            </p>
          ) : null}
          <ul className="space-y-0.5">
            {group.items.map((item) => (
              <li key={item.href}>
                <NavEntry
                  item={item}
                  collapsed={collapsed}
                  active={pathname === item.href || pathname.startsWith(`${item.href}/`)}
                />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}
