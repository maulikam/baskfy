import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Prompt 8 acceptance criterion: "No layout shift on theme toggle or on data load (skeletons
 * reserve space)." A skeleton is therefore never a spinner and never `h-auto`: it occupies
 * exactly the box its content will, so the swap moves nothing.
 */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}
