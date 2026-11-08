"use client";

import { useSyncExternalStore } from "react";

/**
 * True once the client has hydrated, false during server rendering.
 *
 * The usual spelling of this is `useState(false)` plus `useEffect(() => setMounted(true))`, which
 * schedules a second render for every component that uses it and is exactly what
 * `react-hooks/set-state-in-effect` warns about. `useSyncExternalStore` answers the same question
 * with the mechanism React provides for it: a server snapshot of `false`, a client snapshot of
 * `true`, and no subscription because the answer never changes again.
 *
 * Needed wherever the first client render must differ from the server's — reading `localStorage`,
 * or reading the resolved theme, both of which are unknowable during SSR.
 */
const subscribe = () => () => undefined;
const getSnapshot = () => true;
const getServerSnapshot = () => false;

export function useIsMounted(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
