"use client";

import { SleeveUnavailable } from "@/components/shell/sleeve-unavailable";

/**
 * A refusal, a degraded deployment or a 500 is not "nothing scanned" (`gates/sleeve-read-contract.md`
 * C7): `@/lib/options/fetch` throws `SleeveUnavailableError` for those, and this boundary says so
 * instead of letting the tab render its empty state over them.
 */
export default function OptionsError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return <SleeveUnavailable strategy="NIFTY options" reset={reset} />;
}
