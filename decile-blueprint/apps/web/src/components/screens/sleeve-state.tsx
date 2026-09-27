"use client";

import { QueryClient, QueryClientContext, QueryClientProvider } from "@tanstack/react-query";
import { useContext, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import {
  STATE_LABEL,
  stateTone,
  useSleeveStates,
  type SleeveName,
  type SleeveState,
} from "@/lib/screens/sleeve-state";

/**
 * The state chip beside each sleeve's Scan button (LV4): what this book is doing right now —
 * waiting for a login, scanning, monitoring, plan ready, missed window, blocked, idle, closed —
 * with the reason, the next step and what its Scan button actually does on hover, so a
 * re-detection of a closed session is never read as a live scan.
 */

/** The presentational chip: a word, a tone, the reason and what Scan means on hover. */
export function SleeveStateChip({ state }: { state: SleeveState }) {
  const title = `${state.reason}. Next: ${state.next}. ${state.scanMeans}.`;
  return (
    <Badge
      variant={stateTone(state.state)}
      title={title}
      data-testid={`sleeve-state-${state.sleeve}`}
      data-state={state.state}
      className="cursor-help"
    >
      {STATE_LABEL[state.state]}
    </Badge>
  );
}

function BadgeBody({ sleeve }: { sleeve: SleeveName }) {
  const states = useSleeveStates();
  const mine = states.find((row) => row.sleeve === sleeve);
  if (!mine) return null;
  return <SleeveStateChip state={mine} />;
}

/**
 * Renders inside whatever QueryClient the page already has, or its own when there is none: the
 * chip sits in three server-rendered page headers, and a header must not depend on a provider
 * being mounted above it to say what the book is doing.
 */
function WithQueryClient({ children }: { children: ReactNode }) {
  const existing = useContext(QueryClientContext);
  const [own] = useState(() => new QueryClient());
  if (existing) return <>{children}</>;
  return <QueryClientProvider client={own}>{children}</QueryClientProvider>;
}

/** The chip for one sleeve, fed by the shared poll; renders nothing until the first answer. */
export function SleeveStateBadge({ sleeve }: { sleeve: SleeveName }) {
  return (
    <WithQueryClient>
      <BadgeBody sleeve={sleeve} />
    </WithQueryClient>
  );
}
