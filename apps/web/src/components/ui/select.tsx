import { ChevronDown } from "lucide-react";
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A styled native `<select>`.
 *
 * Native rather than Radix Select: it is keyboard- and screen-reader-correct in every browser
 * without a roving-focus implementation to get wrong, it type-aheads for free, and on a phone it
 * opens the platform picker. docs/08 §"Screen editor" reserves the searchable combobox for the
 * one control that needs it — `Sort By`, with its 64 options — and every other select here has
 * eight or fewer.
 */
export function Select({ className, children, ...props }: React.ComponentProps<"select">) {
  return (
    <div className="relative">
      <select
        className={cn(
          "h-9 w-full appearance-none rounded-md border border-input bg-card px-3 pr-8 text-sm",
          "transition-colors duration-100 disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
      />
    </div>
  );
}
