"use client";

import { useCallback, useEffect, useState } from "react";

const POLL_MS = 30_000;

/** Polls a desk GET; keeps the last good view when a read fails. */
export function useDeskView<T>(load: () => Promise<T>) {
  const [view, setView] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [readAt, setReadAt] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      setView(await load());
      setError(null);
      setReadAt(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [load]);

  useEffect(() => {
    const first = setTimeout(() => void refresh(), 0);
    const id = setInterval(() => void refresh(), POLL_MS);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [refresh]);

  return { view, error, readAt, refresh };
}
