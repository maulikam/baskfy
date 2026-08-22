import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The top of every page, so every page has the same one (M36).
 *
 * Before this, each route wrote its own `<header>` and they had drifted apart — different heading
 * sizes, some with a description and some without, the actions sometimes above the title and
 * sometimes below. Maulik's note was that moving between the screener and the desk felt like
 * *"jumping from one to another"*, and an inconsistent page head is most of that feeling.
 *
 * Three parts, always in this order:
 *
 *   title   the serif display face, one per page, always an `<h1>`
 *   blurb   one plain sentence saying what the page answers — from `lib/vocabulary`
 *   actions whatever this page can do, right-aligned on a wide screen and wrapped below on a
 *           narrow one
 *
 * `meta` is the small print that belongs to the data rather than to the page — an as-of date, a
 * row count. It sits under the actions so it can never push the title off a phone.
 */
export interface PageHeaderProps {
  title: string;
  blurb?: string;
  actions?: ReactNode;
  meta?: ReactNode;
  className?: string;
}

export function PageHeader({ title, blurb, actions, meta, className }: PageHeaderProps) {
  return (
    <header className={cn("flex flex-col gap-4", className)}>
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 space-y-1.5">
          <h1 className="text-[2rem] leading-[1.05] sm:text-[2.5rem]">{title}</h1>
          {blurb ? (
            <p className="max-w-[62ch] text-sm leading-relaxed text-muted-foreground">{blurb}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {meta ? <div className="text-xs text-muted-foreground">{meta}</div> : null}
    </header>
  );
}
