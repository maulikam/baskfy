"use client";

import { Search } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * The visible way into the ⌘K palette.
 *
 * It exists because a keyboard shortcut nobody is told about is a feature nobody uses, and because
 * ⌘K is unreachable on a touch device. Split out of the top bar in M36 so the palette's two
 * triggers — this and the key binding — are not tangled up with the navigation's layout.
 */
export function SearchButton() {
  return (
    <Button
      variant="outline"
      size="sm"
      aria-label="Search stocks, indices, baskets, and screens"
      className="w-10 justify-center gap-2 px-0 font-normal text-muted-foreground sm:w-40 sm:justify-start sm:px-3 lg:w-56"
      onClick={() => {
        document.dispatchEvent(
          new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }),
        );
      }}
    >
      <Search aria-hidden="true" />
      <span className="hidden flex-1 text-left sm:inline">Search…</span>
      <kbd className="hidden rounded border border-border px-1 text-[10px] font-medium lg:inline">
        ⌘K
      </kbd>
    </Button>
  );
}
