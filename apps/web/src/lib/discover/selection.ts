/**
 * Which baskets the reader has picked to compare.
 *
 * Slugs only, never whole cards. A selection outlives the page it was made on — you add one on
 * Discover, another from a collection, and open Compare from a third place — so anything richer
 * than an identifier would be a stale copy of a payload by the time it was read back.
 *
 * Three is the cap, and it is a design decision rather than a technical one: a comparison table
 * of four baskets does not fit a laptop without a horizontal scroll, and the moment it scrolls
 * the labels leave the screen and the numbers stop meaning anything.
 */

import type { Route } from "next";

export const MAX_COMPARE = 3;

/** Versioned, so a future shape change can be recognised rather than mis-parsed. */
export const SELECTION_STORAGE_KEY = "baskfy.compare.v1";

/** The query parameter Compare reads. Repeated once per basket, in selection order. */
export const COMPARE_PARAM = "b";

export function isSelected(selection: readonly string[], slug: string): boolean {
  return selection.includes(slug);
}

export function isFull(selection: readonly string[]): boolean {
  return selection.length >= MAX_COMPARE;
}

/** True when this slug could be added — already-selected counts as addable (toggling removes). */
export function canToggle(selection: readonly string[], slug: string): boolean {
  return isSelected(selection, slug) || !isFull(selection);
}

/**
 * Add or remove one basket, preserving the order things were picked in.
 *
 * A full selection ignores an add rather than evicting the oldest. Silently dropping a basket the
 * reader chose, to make room for one they just clicked, is the kind of helpfulness that loses
 * work; the UI disables the control and says why instead.
 */
export function toggle(selection: readonly string[], slug: string): string[] {
  if (isSelected(selection, slug)) return selection.filter((item) => item !== slug);
  if (isFull(selection)) return [...selection];
  return [...selection, slug];
}

export function clear(): string[] {
  return [];
}

/** Normalise anything that comes back from storage or a URL into a usable selection. */
export function normalise(input: unknown): string[] {
  if (!Array.isArray(input)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of input) {
    if (typeof item !== "string") continue;
    const slug = item.trim();
    if (slug === "" || seen.has(slug)) continue;
    seen.add(slug);
    out.push(slug);
    if (out.length === MAX_COMPARE) break;
  }
  return out;
}

/**
 * Read the selection back.
 *
 * Every failure mode returns an empty selection rather than throwing: `localStorage` throws on
 * access in a private window and in some embedded viewers, the value may be absent, and it may be
 * anything at all because a person can type into it. A comparison bar is not worth a blank page.
 */
export function readSelection(storage: Storage | null | undefined): string[] {
  if (!storage) return [];
  try {
    const raw = storage.getItem(SELECTION_STORAGE_KEY);
    if (raw === null) return [];
    return normalise(JSON.parse(raw));
  } catch {
    return [];
  }
}

export function writeSelection(
  storage: Storage | null | undefined,
  selection: readonly string[],
): void {
  if (!storage) return;
  try {
    storage.setItem(SELECTION_STORAGE_KEY, JSON.stringify(normalise(selection)));
  } catch {
    /* A selection that cannot be remembered still works for this page. */
  }
}

/** `/discover/compare?b=one&b=two` — order preserved, so the table's columns match the picking.
 *  `Route` with one assertion: the path is a route this app owns, and `typedRoutes` cannot check
 *  a query string built from slugs chosen in the browser. */
export function compareHref(selection: readonly string[]): Route {
  const params = new URLSearchParams();
  for (const slug of normalise(selection)) params.append(COMPARE_PARAM, slug);
  const query = params.toString();
  return query ? (`/discover/compare?${query}` as Route) : "/discover/compare";
}

/** Read a selection out of a URL's search params, applying the same cap and de-duplication. */
export function selectionFromParams(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
): string[] {
  if (params instanceof URLSearchParams) return normalise(params.getAll(COMPARE_PARAM));
  const raw = params[COMPARE_PARAM];
  if (raw === undefined) return [];
  return normalise(Array.isArray(raw) ? raw : [raw]);
}

/** "2 of 3 baskets selected" — the sticky bar's line, and the only place it is worded. */
export function selectionSummary(selection: readonly string[]): string {
  const count = selection.length;
  return `${count} of ${MAX_COMPARE} baskets selected`;
}

/** Compare needs at least two things to compare. */
export const MIN_COMPARE = 2;

export function canCompare(selection: readonly string[]): boolean {
  return selection.length >= MIN_COMPARE;
}
