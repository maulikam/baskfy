"use client";

import { ChevronDown, Gauge } from "lucide-react";
import { usePathname } from "next/navigation";

import { sectionTabIsActive } from "@/components/shell/section-tab-active";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { DESK_MENU, isDeskSection, type DeskMenuLink } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * Desk on the primary bar. Items are the desk console at `/desk`.
 */
export function DeskMenu({
  align = "start",
  variant = "pill",
  className,
}: {
  align?: "start" | "end";
  /** `pill` sits in the desktop bar. `tab` is the mobile bottom-bar item and opens upward. */
  variant?: "pill" | "tab";
  className?: string;
}) {
  const pathname = usePathname();
  const active = isDeskSection(pathname);

  return (
    <div className={cn(variant === "tab" && "flex w-full", className)}>
      <DropdownMenu>
        <DropdownMenuTrigger
          className={cn(
            "cursor-pointer border-0 bg-transparent font-inherit",
            variant === "pill"
              ? cn(
                  "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-3 py-1.5 text-[15px] transition-colors duration-150",
                  active
                    ? "bg-accent font-medium text-accent-foreground"
                    : "text-foreground/70 hover:bg-foreground/5 hover:text-foreground data-[state=open]:bg-foreground/5 data-[state=open]:text-foreground",
                )
              : cn(
                  "flex w-full flex-col items-center justify-center gap-0.5 rounded-md text-[11px] font-medium",
                  active ? "text-accent" : "text-muted-foreground",
                ),
          )}
          aria-current={active ? "page" : undefined}
        >
          {variant === "tab" ? (
            <span
              className={cn("grid size-8 place-items-center rounded-full", active && "bg-brand/15")}
            >
              <Gauge
                aria-hidden="true"
                className={cn("size-5", active && "fill-brand/20")}
                strokeWidth={active ? 2.25 : 1.75}
              />
            </span>
          ) : null}
          Desk
          {variant === "pill" ? (
            <ChevronDown aria-hidden="true" className="size-3.5 opacity-70" />
          ) : null}
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={align}
          side={variant === "tab" ? "top" : "bottom"}
          className="min-w-52"
        >
          {DESK_MENU.map((group, index) => (
            <div key={group[0]?.href ?? index}>
              {index > 0 ? <DropdownMenuSeparator className="my-1 h-px bg-border" /> : null}
              {group.map((item) => (
                <DeskEntry key={item.href} item={item} pathname={pathname} />
              ))}
            </div>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

function DeskEntry({ item, pathname }: { item: DeskMenuLink; pathname: string }) {
  if (!item.children || item.children.length === 0) {
    const active = sectionTabIsActive(pathname, item.href, [item.href]);
    return (
      <DropdownMenuItem asChild className={cn(active && "bg-muted font-medium")}>
        <a href={item.href} aria-current={active ? "page" : undefined}>
          {item.label}
        </a>
      </DropdownMenuItem>
    );
  }

  const hrefs = item.children.map((child) => child.href);
  const groupActive = hrefs.some((href) => sectionTabIsActive(pathname, href, hrefs));

  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger className={cn(groupActive && "font-medium")}>
        {item.label}
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent>
        {item.children.map((child) => {
          const active = sectionTabIsActive(pathname, child.href, hrefs);
          return (
            <DropdownMenuItem key={child.href} asChild className={cn(active && "bg-muted font-medium")}>
              <a href={child.href} aria-current={active ? "page" : undefined}>
                {child.label}
              </a>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}
