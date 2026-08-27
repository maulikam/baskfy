"use client";

import { normalise, readSelection, writeSelection } from "@/lib/discover/selection";

/**
 * The compare selection as an external store, read through `useSyncExternalStore`.
 *
 * **Why not `useState` plus an effect.** The obvious shape — start empty, read `localStorage` in
 * an effect, `setSelection` — is what `react-hooks/set-state-in-effect` exists to catch, and the
 * rule is right: it renders once with the wrong answer and then again with the right one, and
 * every consumer sees a flash of "nothing selected". An external store hydrates *before* the
 * first subscriber's render is committed, so React reads the settled value.
 *
 * Two things fall out of it for free, and both are real:
 *
 * - **Cross-tab sync.** A `storage` event fires in every *other* tab of the same origin. Selecting
 *   a basket in one tab and opening Compare in another now agrees, which the `useState` version
 *   could not do at all.
 * - **One source of truth for many providers.** The selection is shared by the hub, the catalogue
 *   and each collection page; module state means those cannot drift apart.
 */

export interface SelectionSnapshot {
  selection: readonly string[];
  /** False until `localStorage` has been read. Controls stay inert rather than lying. */
  hydrated: boolean;
}

const EMPTY: SelectionSnapshot = Object.freeze({ selection: Object.freeze([]), hydrated: false });

/**
 * Cached, and it has to be: `useSyncExternalStore` compares snapshots by identity and calls
 * `getSnapshot` on every render. Building a fresh object each time is an infinite render loop.
 */
let snapshot: SelectionSnapshot = EMPTY;
const listeners = new Set<() => void>();

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // Private windows and some embedded viewers throw on the accessor itself.
    return null;
  }
}

function publish(selection: readonly string[], hydrated: boolean): void {
  snapshot = Object.freeze({ selection: Object.freeze([...selection]), hydrated });
  for (const listener of listeners) listener();
}

function hydrate(): void {
  if (snapshot.hydrated) return;
  publish(readSelection(storage()), true);
}

function onStorageEvent(event: StorageEvent): void {
  // `key === null` is a `clear()`, which affects us too.
  if (event.key !== null && !event.key.startsWith("baskfy.compare")) return;
  publish(readSelection(storage()), true);
}

export function subscribe(listener: () => void): () => void {
  const first = listeners.size === 0;
  listeners.add(listener);
  if (first) {
    hydrate();
    window.addEventListener("storage", onStorageEvent);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", onStorageEvent);
  };
}

export function getSnapshot(): SelectionSnapshot {
  return snapshot;
}

/**
 * The server renders an empty, un-hydrated selection — it cannot know what a reader's browser
 * holds, and guessing would be a hydration mismatch, which React resolves by discarding the
 * server HTML.
 */
export function getServerSnapshot(): SelectionSnapshot {
  return EMPTY;
}

export function setSelection(next: readonly string[]): void {
  const normalised = normalise([...next]);
  writeSelection(storage(), normalised);
  publish(normalised, true);
}

/** Test seam: forget everything, including the hydration flag. */
export function resetSelectionStore(): void {
  snapshot = EMPTY;
  listeners.clear();
}
