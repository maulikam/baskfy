"use client";

import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef, useState } from "react";

import {
  BumpinessDots,
  RankBadge,
  ReturnChip,
  ScoreBar,
} from "@/components/screens/cell-encodings";
import type { ResultRow } from "@/components/screens/result-columns";
import { formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The mobile ranked feed — the screen-page redesign brief §3.5, "Mobile = cards, not a shrunken
 * table": rank badge, symbol and name, score bar, return chip, tap to open the peek drawer.
 *
 * Why it is virtualised
 * ---------------------
 * It was not, and a 271-row screen put 271 cards in the DOM on a phone — measured at 390×844 with
 * `document.querySelectorAll('ul[aria-label] > li').length`, which returned 271 while the desktop
 * grid held ~26 nodes for the same result. That is the one surface in the results panel that a
 * low-powered device actually renders, and it was the only one paying full price for the row set.
 *
 * Why the *window* and not an inner scroller
 * ------------------------------------------
 * `DataTable` virtualises against its own 560px scroll container, which is right on a desktop where
 * the grid is one panel among several. A phone is the opposite case: the card feed *is* the page.
 * Putting a second scroller inside a 844px viewport would cost a fixed slice of the screen to the
 * container's own chrome, trap momentum scrolling and pull-to-refresh inside a box the user cannot
 * see the edges of, and give the browser two scroll positions to restore instead of one. So the
 * feed scrolls with the document and `useWindowVirtualizer` reads the window's offset — the same
 * `@tanstack/react-virtual` primitive the table uses, pointed at the scroller that already exists.
 * `scrollMargin` is what reconciles the two: it is the list's own offset down the document, so the
 * virtualiser's item offsets stay absolute and the header above the feed keeps scrolling away
 * normally.
 *
 * Why a fixed card height and not dynamic measurement
 * --------------------------------------------------
 * A card whose symbol and name could wrap would need `measureElement`, and here that would be the
 * wrong trade twice over. First, nothing in a card wraps: every line truncates, and the height is
 * therefore a constant of the design rather than a property of the data — an "estimate" that is
 * exact cannot drift, so measuring would buy nothing. Second, `ResultsPanel` renders both branches
 * and hides one with `min-[700px]:hidden`, so above 700px this list is `display: none` and every
 * `ResizeObserver` measurement of it would be zero; a dynamic virtualiser would cache a total size
 * of 0 and only recover on the resize that crosses the breakpoint. A fixed size has no such state
 * to get wrong.
 *
 * The height is declared in rem and resolved against the root font size, not hard-coded in pixels,
 * so a reader who has raised their browser's default text size gets taller cards instead of clipped
 * ones (WCAG 1.4.4). Page zoom scales px and rem alike and needs nothing.
 *
 * Keeping the list a list
 * -----------------------
 * The usual virtualisation shape — a spacer `<div>` sized to the total, holding the rendered
 * children — would put a `<div>` between the `<ul>` and its `<li>`s and cost the list its
 * semantics. The `<ul>` is the spacer instead: it carries the total height and the positioning
 * context, and the `<li>`s are absolutely positioned directly inside it, so `ul > li` still holds.
 * `aria-setsize` and `aria-posinset` then tell assistive technology the true total and each card's
 * true place in it, which is the same job `aria-rowcount` and `aria-rowindex` do for the grid.
 */

/**
 * The card box and the gap beneath it, in rem.
 *
 * 7.25rem is what the contents add up to at the default root size: 0.75rem of padding and a 1px
 * border on each edge, around a 1.25rem symbol line, a 1rem name line, a 1rem score-bar row and a
 * 1.25rem chip row with 0.5rem between them. Change any of those and change this.
 */
export const CARD_HEIGHT_REM = 7.25;

/** The `gap-2` the un-virtualised stack used, now carried inside each item's slot. */
export const CARD_GAP_REM = 0.5;

/**
 * Cards rendered above and below the viewport. `DataTable` overscans 8 rows of 34px (~272px); six
 * cards is ~700px in each direction, which is the same idea sized for a flick on a touchscreen
 * rather than a wheel.
 */
export const CARD_OVERSCAN = 6;

/**
 * How many cards get the staggered entrance.
 *
 * The stagger is a reveal of the first result, not a property of a card: applying it to every index
 * would re-run a fade on each card that scrolls into the window, which is both distracting and work
 * done during the frames that can least afford it.
 */
const REVEAL_LIMIT = 8;

const DEFAULT_ROOT_FONT_SIZE = 16;

export interface ResultCardsProps {
  rows: readonly ResultRow[];
  onActivate: (row: ResultRow) => void;
  className?: string | undefined;
}

function num(row: ResultRow, key: string): number | null {
  const raw = row[key];
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  return null;
}

interface ListMetrics {
  /** Pixels per card slot: the card box plus the gap beneath it. */
  stride: number;
  /** The gap beneath each card, in pixels. */
  gap: number;
  /** How far the list starts down the document, which is what the window offset is measured from. */
  scrollMargin: number;
}

function readRootFontSize(): number {
  if (typeof document === "undefined") return DEFAULT_ROOT_FONT_SIZE;
  const parsed = Number.parseFloat(getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_ROOT_FONT_SIZE;
}

/**
 * Card geometry, re-read on resize.
 *
 * Resize is the right trigger for both halves: it is when the root font size can have changed under
 * a zoom, and it is when this list crosses the 700px breakpoint and stops being `display: none` —
 * which is the moment its document offset becomes a real number rather than zero.
 */
function useListMetrics(list: React.RefObject<HTMLUListElement | null>, rowCount: number) {
  const [metrics, setMetrics] = useState<ListMetrics>(() => ({
    stride: (CARD_HEIGHT_REM + CARD_GAP_REM) * DEFAULT_ROOT_FONT_SIZE,
    gap: CARD_GAP_REM * DEFAULT_ROOT_FONT_SIZE,
    scrollMargin: 0,
  }));

  useEffect(() => {
    const read = () => {
      const root = readRootFontSize();
      const node = list.current;
      const scrollMargin = node ? node.getBoundingClientRect().top + window.scrollY : 0;
      const next: ListMetrics = {
        stride: (CARD_HEIGHT_REM + CARD_GAP_REM) * root,
        gap: CARD_GAP_REM * root,
        scrollMargin,
      };
      setMetrics((current) =>
        current.stride === next.stride &&
        current.gap === next.gap &&
        current.scrollMargin === next.scrollMargin
          ? current
          : next,
      );
    };
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, [list, rowCount]);

  return metrics;
}

/** Mobile ranked feed — below ~700px (§3.5). */
export function ResultCards({ rows, onActivate, className }: ResultCardsProps) {
  const listRef = useRef<HTMLUListElement>(null);
  const { stride, gap, scrollMargin } = useListMetrics(listRef, rows.length);

  const virtualizer = useWindowVirtualizer({
    count: rows.length,
    estimateSize: () => stride,
    overscan: CARD_OVERSCAN,
    scrollMargin,
  });

  const virtualItems = virtualizer.getVirtualItems();

  return (
    <ul
      ref={listRef}
      data-testid="result-cards"
      className={cn("relative w-full", className)}
      aria-label="Screen results as cards"
      style={{ height: virtualizer.getTotalSize() }}
    >
      {virtualItems.map((item) => {
        const row = rows[item.index];
        if (!row) return null;

        const symbol = typeof row.symbol === "string" ? row.symbol : "—";
        const name = typeof row.name === "string" ? row.name : "";
        const score = num(row, "sorting_factor");
        const ret = num(row, "ret_12m");
        const vol = num(row, "vol_12m");
        const close = num(row, "close_raw");
        const rank = typeof row.rank === "number" ? row.rank : item.index + 1;
        const revealed = item.index < REVEAL_LIMIT;

        return (
          <li
            key={`${symbol}-${rank}`}
            data-testid="result-card"
            data-index={item.index}
            aria-setsize={rows.length}
            aria-posinset={item.index + 1}
            className="absolute left-0 top-0 w-full"
            style={{
              height: stride,
              paddingBottom: gap,
              transform: `translateY(${item.start - scrollMargin}px)`,
            }}
          >
            <button
              type="button"
              onClick={() => onActivate(row)}
              className={cn(
                "flex h-full w-full items-start gap-3 overflow-hidden p-3.5 text-left vaaya-card",
                "outline-none focus-visible:ring-2 focus-visible:ring-ring",
                revealed &&
                  "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 motion-safe:duration-200",
              )}
              style={
                revealed
                  ? {
                      animationDelay: `min(${item.index * 18}ms, 300ms)`,
                      animationFillMode: "both",
                    }
                  : undefined
              }
            >
              <div className="pt-0.5">
                <RankBadge rank={rank} />
              </div>
              {/* Every line below is single-line and truncated. That is what makes the card a
                  fixed height, which is what makes the virtualiser's size exact. */}
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <div>
                  <div className="truncate text-sm font-medium">{symbol}</div>
                  {name ? (
                    <div className="truncate text-xs text-muted-foreground">{name}</div>
                  ) : null}
                </div>
                {score !== null ? <ScoreBar value={score} /> : null}
                <div className="flex min-w-0 items-center gap-2 overflow-hidden">
                  {ret !== null ? (
                    <span className="shrink-0">
                      <ReturnChip text={formatPercent(ret)} value={ret} />
                    </span>
                  ) : null}
                  {vol !== null ? (
                    <span className="shrink-0">
                      <BumpinessDots value={vol} />
                    </span>
                  ) : null}
                  {close !== null ? (
                    <span className="truncate text-xs tabular-nums text-muted-foreground">
                      ₹{formatNumber(close, { decimals: 2 })}
                    </span>
                  ) : null}
                </div>
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
