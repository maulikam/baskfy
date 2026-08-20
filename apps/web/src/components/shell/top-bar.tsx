"use client";

import { Search } from "lucide-react";

import { CommandPalette } from "@/components/shell/command-palette";
import { FreshnessPill } from "@/components/shell/freshness-pill";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { UserMenu, type UserMenuProps } from "@/components/shell/user-menu";
import { Button } from "@/components/ui/button";

/**
 * docs/08 §"App shell": "Top bar: global instrument search (`⌘K`), data-freshness pill
 * (`Data: 19 Aug 2026`), theme toggle, user menu."
 *
 * The visible search button exists because a keyboard shortcut nobody is told about is a feature
 * nobody uses — and because ⌘K is unreachable on a touch device. It opens the same palette.
 */
export function TopBar({ user }: { user: UserMenuProps }) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4">
      <Button
        variant="outline"
        size="sm"
        className="w-56 justify-start gap-2 font-normal text-muted-foreground"
        onClick={() => {
          document.dispatchEvent(
            new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }),
          );
        }}
      >
        <Search aria-hidden="true" />
        <span className="flex-1 text-left">Search instruments…</span>
        <kbd className="rounded border border-border px-1 text-[10px] font-medium">⌘K</kbd>
      </Button>

      <div className="flex-1" />

      <FreshnessPill />
      <ThemeToggle />
      <UserMenu {...user} />
      <CommandPalette />
    </header>
  );
}
