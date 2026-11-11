"use client";

import type { ReactNode } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/**
 * A flag or score with its meaning on hover and on keyboard focus (Maulik, 11 Nov 2026: "whenever I hover it,
 * it should show what the flag meaning is"). Its own provider, so it works in any table, with or without the
 * app shell; native `title` stays as the fallback for anything that cannot show the popup.
 */
export function FlagTip({
  label,
  meaning,
  className,
  children,
  testId,
}: {
  label: string;
  meaning?: string | undefined;
  className?: string | undefined;
  children: ReactNode;
  testId?: string | undefined;
}) {
  const text = meaning ? `${label}: ${meaning}` : label;
  return (
    <TooltipProvider delayDuration={60}>
      <Tooltip>
        <TooltipTrigger
          type="button"
          aria-label={text}
          title={text}
          data-testid={testId}
          className={`inline cursor-help rounded-sm bg-transparent p-0 font-[inherit] leading-[inherit] outline-none focus-visible:ring-2 focus-visible:ring-ring ${className ?? ""}`}
        >
          {children}
        </TooltipTrigger>
        <TooltipContent className="max-w-72">
          <p className="font-medium">{label}</p>
          {meaning ? <p className="mt-0.5 text-muted-foreground">{meaning}</p> : null}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
