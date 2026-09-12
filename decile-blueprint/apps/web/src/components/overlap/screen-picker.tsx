"use client";

import { useRouter } from "next/navigation";

import type { ScreenOption } from "@/lib/overlap/overlap";

/**
 * Which screen feeds the three-way overlap. The choice lives in `?screen=` so the page stays a
 * link — shareable, bookmarkable, and checkable against a bug report.
 */
export function ScreenPicker({
  screens,
  selected,
}: {
  screens: readonly ScreenOption[];
  selected: string;
}) {
  const router = useRouter();

  if (screens.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="overlap-screen-none">
        No screens are available to intersect yet.
      </p>
    );
  }

  return (
    <label className="flex flex-wrap items-center gap-2 text-sm" data-testid="overlap-screen-picker">
      <span className="text-muted-foreground">Screen</span>
      <select
        className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
        value={selected}
        onChange={(event) => {
          const next = event.target.value;
          router.push(`/build/overlap?screen=${encodeURIComponent(next)}`);
        }}
      >
        {screens.map((screen) => (
          <option key={screen.publicId} value={screen.publicId}>
            {screen.name}
          </option>
        ))}
      </select>
    </label>
  );
}
