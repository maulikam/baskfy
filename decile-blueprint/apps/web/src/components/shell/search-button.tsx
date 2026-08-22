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
      className="w-40 justify-start gap-2 font-normal text-muted-foreground lg:w-56"
      onClick={() => {
        document.dispatchEvent(
          new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }),
        );
      }}
    >
      <Search aria-hidden="true" />
      <span className="flex-1 text-left">Search any stock…</span>
      <kbd className="hidden rounded border border-border px-1 text-[10px] font-medium lg:inline">
        ⌘K
      </kbd>
    </Button>
  );
}
