"use client";

import { SleeveUnavailable } from "@/components/shell/sleeve-unavailable";

export default function OpportunitiesError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return <SleeveUnavailable strategy="Opportunities" reset={reset} />;
}
