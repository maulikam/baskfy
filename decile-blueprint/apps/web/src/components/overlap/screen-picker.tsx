"use client";

import { useRouter } from "next/navigation";

import type { ScreenOption } from "@/lib/overlap/overlap";

/**
 * Which screens feed the matrix columns. The choice lives in repeated `?screen=` params so the
 * page stays a link — shareable, bookmarkable, and checkable against a bug report.
 *
 * Three dropdowns when three screens exist: one column each, every screen listed in each.
 */
export function ScreenPicker({
  screens,
  selected,
}: {
  screens: readonly ScreenOption[];
  selected: readonly string[];
}) {
  const router = useRouter();

  if (screens.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="overlap-screen-none">
        No screens are available to intersect yet.
      </p>
    );
  }

  const templates = screens.filter((screen) => screen.isExample);
  const mine = screens.filter((screen) => !screen.isExample);

  function pushSelection(next: readonly string[]) {
    const query = next
      .map((id) => `screen=${encodeURIComponent(id)}`)
      .join("&");
    router.push(query ? `/build/overlap?${query}` : "/build/overlap");
  }

  return (
    <fieldset
      className="flex flex-wrap items-center gap-2 text-sm"
      data-testid="overlap-screen-picker"
    >
      <legend className="sr-only">Screens to include</legend>
      <span className="text-muted-foreground">Screens</span>
      {selected.map((value, index) => {
        const taken = new Set(selected.filter((_, slot) => slot !== index));
        return (
          <label key={`${value}-${index}`} className="flex items-center gap-2">
            <span className="sr-only">{`Screen ${index + 1}`}</span>
            <select
              aria-label={`Screen ${index + 1}`}
              className="max-w-[16rem] rounded-md border border-border bg-background px-2 py-1.5 text-sm"
              data-testid={`overlap-screen-picker-${index}`}
              value={value}
              onChange={(event) => {
                const next = [...selected];
                next[index] = event.target.value;
                pushSelection(next);
              }}
            >
              {templates.length > 0 ? (
                <optgroup label="Templates">
                  {templates.map((screen) => (
                    <option
                      key={screen.publicId}
                      value={screen.publicId}
                      disabled={taken.has(screen.publicId)}
                    >
                      {screen.name}
                    </option>
                  ))}
                </optgroup>
              ) : null}
              {mine.length > 0 ? (
                <optgroup label="Your screens">
                  {mine.map((screen) => (
                    <option
                      key={screen.publicId}
                      value={screen.publicId}
                      disabled={taken.has(screen.publicId)}
                    >
                      {screen.name}
                    </option>
                  ))}
                </optgroup>
              ) : null}
            </select>
          </label>
        );
      })}
    </fieldset>
  );
}
