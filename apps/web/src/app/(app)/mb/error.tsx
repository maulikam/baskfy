"use client";

import { SleeveUnavailable } from "@/components/shell/sleeve-unavailable";

/** A refusal, a 503 and a 500 land here rather than as the empty state (sleeve-read C7). */
export default function SleeveError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return <SleeveUnavailable strategy="Doubling candidates" reset={reset} />;
}
