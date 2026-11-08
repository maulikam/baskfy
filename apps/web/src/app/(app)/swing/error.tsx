"use client";

import { SleeveUnavailable } from "@/components/shell/sleeve-unavailable";

/**
 * The route segment's error boundary — `gates/sleeve-read-contract.md` C7.
 *
 * `@/lib/swing/fetch` answers `null` only when the API answered and there was nothing there.
 * A refusal, a 503 and a 500 now leave it as `SleeveUnavailableError` and land here, so this
 * page can no longer render any of them as its empty state.
 */
export default function SleeveError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return <SleeveUnavailable strategy="Swing" reset={reset} />;
}
