"use client";

import { useEffect, useState } from "react";

/**
 * A value that settles `delay` milliseconds after it stops changing.
 *
 * docs/08 §"Screen editor": "**Debounced live preview** (400 ms) hitting `POST /screens/preview`".
 * Debouncing the *value* rather than the request is what lets TanStack Query key its cache on the
 * settled definition — so returning to a configuration you have already previewed is instant and
 * costs no request, which a debounced fetch call could not do.
 */
export const PREVIEW_DEBOUNCE_MS = 400;

export function useDebounced<T>(value: T, delay: number = PREVIEW_DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return settled;
}
