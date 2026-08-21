import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * One numbered block of docs/01 §5, as a landmark.
 *
 * Every block is a `<section>` with a heading that labels it, so the page has a real document
 * outline — docs/11 §Accessibility asks for the surface to be navigable by structure, and a page
 * of eleven anonymous `<div>`s is not.
 */
export interface SectionProps {
  id: string;
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string | undefined;
}

export function Section({ id, title, description, action, children, className }: SectionProps) {
  return (
    <section
      aria-labelledby={`${id}-heading`}
      className={cn("rounded-lg border border-border bg-card p-4", className)}
    >
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <div>
          <h2 id={`${id}-heading`} className="text-sm font-semibold">
            {title}
          </h2>
          {description ? (
            <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
