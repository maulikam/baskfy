"use client";

import type * as React from "react";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * The screen editor's filter groups — docs/08 §"Screen editor":
 *
 *     "Each accordion header shows a **count badge** of active filters inside it, so a collapsed
 *      group never hides state."
 *
 * That badge is the whole reason this component exists rather than a bare `<Accordion>`. A
 * twenty-field form with collapsible groups is calm right up until a user cannot tell which
 * collapsed group is silently removing two thousand rows from their results.
 *
 * The count is supplied per section rather than derived here: only the caller knows which of its
 * fields are at their sentinel (see `SentinelNumberInput`), and duplicating that logic in two
 * places is how the badge and the query drift apart.
 */
export interface FilterSection {
  id: string;
  title: string;
  /** How many filters inside this group are currently doing something. */
  activeCount: number;
  content: React.ReactNode;
}

export interface FilterAccordionProps {
  sections: readonly FilterSection[];
  /** Ids open on first render. Groups with active filters should generally start open. */
  defaultOpen?: readonly string[];
  className?: string | undefined;
}

export function FilterAccordion({
  sections,
  defaultOpen = [],
  className,
}: FilterAccordionProps) {
  return (
    <Accordion
      type="multiple"
      defaultValue={[...defaultOpen]}
      className={cn("w-full", className)}
    >
      {sections.map((section) => (
        <AccordionItem key={section.id} value={section.id}>
          <AccordionTrigger>
            <span className="flex items-center gap-2">
              {section.title}
              {section.activeCount > 0 ? (
                <Badge variant="accent">
                  {section.activeCount}
                  <span className="sr-only">
                    {section.activeCount === 1 ? " active filter" : " active filters"}
                  </span>
                </Badge>
              ) : null}
            </span>
          </AccordionTrigger>
          <AccordionContent className="space-y-4">{section.content}</AccordionContent>
        </AccordionItem>
      ))}
    </Accordion>
  );
}
