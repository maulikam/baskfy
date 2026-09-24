"use client";

import { SleeveUnavailable } from "@/components/shell/sleeve-unavailable";

/**
 * A refusal, a degraded deployment or a 500 is not "nothing scanned" (`gates/sleeve-read-contract.md`
 * C7): `@/lib/fno/fetch` throws `SleeveUnavailableError` for those, and this boundary says so
 * instead of letting the tab render its empty state over them.
 */
export default function OvernightError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return <SleeveUnavailable strategy="Overnight F&O" reset={reset} />;
}
