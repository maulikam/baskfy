"use client";

import { useCallback, useRef, type KeyboardEvent } from "react";

import { DETAIL_TABS, type DetailTabId } from "@/lib/portfolio/detail-tabs";
import { cn } from "@/lib/utils";

/**
 * The eight-tab strip, as an ARIA tablist with the keyboard behaviour the pattern requires.
 *
 * ## Why this is hand-built rather than a Radix `Tabs`
 *
 * `@radix-ui/react-tabs` is not in this app's dependency list, and house rule 1 says nothing
 * outside the locked stack without saying why first. The whole of what it would give us is
 * below: roving tabindex, arrow keys wrapping at both ends, Home and End, and a panel bound to
 * its tab in both directions. Twenty lines against a dependency is not a trade worth making.
 *
 * ## The keyboard contract, which is the half a mouse never exercises
 *
 * Exactly one tab is in the tab order at a time (`tabIndex` 0 on the selected one, −1 on the
 * rest), so Tab moves *past* the strip into the panel rather than through eight stops. Left and
 * Right move between tabs and select as they go — automatic activation, which is the right
 * choice here because every panel is already rendered from data in memory and selecting one
 * costs nothing. Home and End jump to the ends. The wrap at both ends is deliberate: a strip
 * that stops dead at "Settings" makes the reader Shift-Tab back through seven tabs to reach
 * "Overview".
 *
 * Each tab's accessible name is its visible label, which is the only version that cannot drift
 * from what a sighted reader sees.
 */

export interface TabStripProps {
  active: DetailTabId;
  onSelect: (id: DetailTabId) => void;
  /** Rendered under each tab where the tab has something worth counting, e.g. holdings. */
  counts?: Partial<Record<DetailTabId, number>>;
  /** Tabs carrying a problem get a marker: a dot AND a count, never colour alone. */
  alerts?: Partial<Record<DetailTabId, number>>;
}

export function tabId(id: DetailTabId): string {
  return `portfolio-tab-${id}`;
}

export function panelId(id: DetailTabId): string {
  return `portfolio-panel-${id}`;
}

/** What a screen reader hears after the tab's name. Empty when there is nothing to add. */
function announce(count: number | undefined, alert: number | undefined): string {
  const parts: string[] = [];
  if (count !== undefined) parts.push(`${count} item${count === 1 ? "" : "s"}`);
  if (alert !== undefined && alert > 0) {
    parts.push(`${alert} needing attention`);
  }
  return parts.length === 0 ? "" : `, ${parts.join(", ")}`;
}

export function TabStrip({ active, onSelect, counts, alerts }: TabStripProps) {
  const strip = useRef<HTMLDivElement>(null);

  const move = useCallback(
    (nextIndex: number) => {
      const next = DETAIL_TABS[nextIndex];
      if (next === undefined) return;
      onSelect(next.id);
      const button = strip.current?.querySelector<HTMLButtonElement>(`#${tabId(next.id)}`);
      button?.focus();
    },
    [onSelect],
  );

  /* The handler sits on each tab rather than on the tablist. A `tablist` is not itself in the tab
     order — the roving tabindex puts exactly one of its tabs there — so a key listener on the
     container is a listener on an element that can never be focused, which `jsx-a11y` is right to
     refuse. The focused tab is what receives the key, and it is what handles it. */
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      const index = DETAIL_TABS.findIndex((tab) => tab.id === active);
      if (index < 0) return;
      const last = DETAIL_TABS.length - 1;
      if (event.key === "ArrowRight") {
        event.preventDefault();
        move(index === last ? 0 : index + 1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        move(index === 0 ? last : index - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        move(0);
      } else if (event.key === "End") {
        event.preventDefault();
        move(last);
      }
    },
    [active, move],
  );

  const activeTab = DETAIL_TABS.find((tab) => tab.id === active);

  return (
    <div className="rounded-xl border border-border bg-card">
      {/* `overflow-x-auto` on the strip only: eight labels do not fit a phone, and a wrapping
          two-row strip moves the panel down by a line every time the reader picks a longer tab. */}
      <div
        ref={strip}
        role="tablist"
        aria-label="Portfolio detail sections"
        aria-orientation="horizontal"
        data-testid="detail-tablist"
        className="flex gap-1 overflow-x-auto px-2 pt-2"
      >
        {DETAIL_TABS.map((tab) => {
          const selected = tab.id === active;
          const count = counts?.[tab.id];
          const alert = alerts?.[tab.id];
          return (
            <button
              key={tab.id}
              id={tabId(tab.id)}
              role="tab"
              type="button"
              aria-selected={selected}
              aria-controls={panelId(tab.id)}
              tabIndex={selected ? 0 : -1}
              onClick={() => onSelect(tab.id)}
              onKeyDown={onKeyDown}
              data-testid={`detail-tab-${tab.id}`}
              className={cn(
                "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors duration-150",
                selected
                  ? "border-brand text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {tab.label}
              {count !== undefined ? (
                <span
                  aria-hidden="true"
                  className="rounded-full bg-muted px-1.5 text-[0.6875rem] tabular-nums text-muted-foreground"
                >
                  {count}
                </span>
              ) : null}
              {alert !== undefined && alert > 0 ? (
                /* A dot alone would be colour carrying meaning, so the count rides beside it. */
                <span
                  aria-hidden="true"
                  className="flex items-center gap-1 text-[0.6875rem] font-semibold text-negative"
                >
                  <span className="size-1.5 rounded-full bg-negative" />
                  <span className="tabular-nums">{alert}</span>
                </span>
              ) : null}
              {/* The visible chips are decoration of the label; this is the sentence a screen
                  reader hears, and it comes AFTER the label so the accessible name still begins
                  with the tab's own name. */}
              <span className="sr-only">{announce(count, alert)}</span>
            </button>
          );
        })}
      </div>
      {activeTab ? (
        <p
          data-testid="detail-tab-question"
          className="border-t border-border px-4 py-2 text-xs text-muted-foreground"
        >
          {activeTab.question}
        </p>
      ) : null}
    </div>
  );
}

/** The panel half of the pattern: labelled by its tab, and focusable so Tab lands in it. */
export function TabPanel({
  id,
  active,
  children,
}: {
  id: DetailTabId;
  active: DetailTabId;
  children: React.ReactNode;
}) {
  if (id !== active) return null;
  return (
    <div
      role="tabpanel"
      id={panelId(id)}
      aria-labelledby={tabId(id)}
      tabIndex={0}
      data-testid={`detail-panel-${id}`}
      className="flex flex-col gap-4 focus-visible:outline-none"
    >
      {children}
    </div>
  );
}
