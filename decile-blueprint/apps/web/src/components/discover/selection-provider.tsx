"use client";

import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";

import {
  MAX_COMPARE,
  canToggle as canToggleSelection,
  toggle as toggleSelection,
} from "@/lib/discover/selection";
import {
  getServerSnapshot,
  getSnapshot,
  setSelection,
  subscribe,
} from "@/lib/discover/selection-store";

/**
 * The compare selection, shared by every surface that can add to it.
 *
 * A reader picks one basket on Discover, another inside a collection, and opens Compare from the
 * sticky bar — three routes, one selection. It lives in a module-level external store
 * (`lib/discover/selection-store.ts`) rather than in this component's state, which is what lets
 * it hydrate before the first commit and stay in step across browser tabs.
 *
 * The context itself carries no state now; it exists so a control can tell whether it is on a
 * surface that opted into comparing at all. `CompareToggle` renders nothing outside a provider,
 * because a permanently disabled button is a promise the page cannot keep.
 */

interface SelectionContextValue {
  selection: readonly string[];
  toggle: (slug: string) => void;
  clear: () => void;
  isSelected: (slug: string) => boolean;
  canToggle: (slug: string) => boolean;
  hydrated: boolean;
  max: number;
}

const SelectionContext = createContext<SelectionContextValue | null>(null);

export function SelectionProvider({ children }: { children: React.ReactNode }) {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const toggle = useCallback((slug: string) => {
    setSelection(toggleSelection(getSnapshot().selection, slug));
  }, []);

  const clear = useCallback(() => setSelection([]), []);

  const value = useMemo<SelectionContextValue>(
    () => ({
      selection: snapshot.selection,
      toggle,
      clear,
      isSelected: (slug: string) => snapshot.selection.includes(slug),
      canToggle: (slug: string) => canToggleSelection(snapshot.selection, slug),
      hydrated: snapshot.hydrated,
      max: MAX_COMPARE,
    }),
    [snapshot, toggle, clear],
  );

  return <SelectionContext.Provider value={value}>{children}</SelectionContext.Provider>;
}

/**
 * Read the selection.
 *
 * Returns an inert selection outside a provider rather than throwing: a basket card is rendered
 * on surfaces that have nothing to do with comparing — home, a manager page — and none of them
 * should crash because they did not opt into a feature.
 */
export function useSelection(): SelectionContextValue {
  const value = useContext(SelectionContext);
  return (
    value ?? {
      selection: [],
      toggle: () => undefined,
      clear: () => undefined,
      isSelected: () => false,
      canToggle: () => false,
      hydrated: false,
      max: MAX_COMPARE,
    }
  );
}

/** True when a provider is above this component — controls hide rather than render dead. */
export function useHasSelection(): boolean {
  return useContext(SelectionContext) !== null;
}
