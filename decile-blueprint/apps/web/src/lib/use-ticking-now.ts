"use client";

import { useSyncExternalStore } from "react";

/**
 * The browser's clock, `null` while the server is rendering — the reading a relative phrase like
 * "12 minutes ago" is computed from.
 *
 * WHY IT IS NOT `Date.now()` IN THE COMPONENT
 * -------------------------------------------
 * Two reasons, and the first is correctness. These sleeve pages are server components; the
 * control that says "Last scanned 12 minutes ago" is a client component under them. A reading
 * taken inside render happens twice — once on the server, once as the browser hydrates — and at a
 * bucket boundary the two disagree, which React reports as a text mismatch. `null` on the server
 * makes the first pass the absolute IST stamp, which cannot mismatch, and the relative phrase
 * appears on the pass after hydration.
 *
 * The second is that "12 minutes ago" should become "13 minutes ago" while somebody is looking at
 * it. A single shared interval does that for every consumer on the page.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect` for the reason `use-is-mounted.ts`
 * gives: it is the mechanism React provides for "the server's answer differs from the client's",
 * and it does not trip `react-hooks/set-state-in-effect`.
 */

/** Half a minute: fine enough that a minute never looks wrong, coarse enough to cost nothing. */
const TICK_MS = 30_000;

let current = Date.now();
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void): () => void {
  // A tab that sat with no subscriber has a frozen reading; take a fresh one before anybody
  // renders from it, rather than showing an age that stopped counting an hour ago.
  current = Date.now();
  listeners.add(listener);
  timer ??= setInterval(() => {
    current = Date.now();
    for (const notify of listeners) notify();
  }, TICK_MS);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = (): number | null => current;
const getServerSnapshot = (): number | null => null;

export function useTickingNow(): number | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
