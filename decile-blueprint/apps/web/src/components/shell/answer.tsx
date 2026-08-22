import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * What this page found, said in a sentence, before any of the evidence (M36).
 *
 * ## Why a screener needs this more than most software does
 *
 * The reference product — and every version of this one until now — opens each page with the
 * measurements: four dials, a row of figures, a table. That is the right *material* and the wrong
 * *order*. "56.4%" on a dial labelled "Above 200 DMA" requires the reader to know what a 200-day
 * moving average is, work out whether 56.4 is a lot, and only then arrive at the thought the page
 * exists to deliver, which is "most companies are rising, not just the big ones".
 *
 * So the page says that first, in words, at a size you cannot skim past — and the dials become
 * what they always should have been, which is the working underneath the answer.
 *
 * This is the single component that most distinguishes the redesign. Anybody can restyle a
 * dashboard; putting the conclusion above the fold and the measurement below it changes what the
 * page is for.
 *
 * ## What it may and may not say
 *
 * A statement of fact about stored rows. **Never a recommendation** — "6 in every 10 companies are
 * above their one-year trend" is an observation; "so it is a good time to buy" is advice, which
 * docs/11 §Compliance forbids on every surface of this product.
 *
 * `highlight` is the few words carrying the number, marked with the chartreuse. One per sentence:
 * a highlighter that marks three phrases has marked nothing.
 */
export interface AnswerProps {
  /** The sentence, with the key phrase wrapped in `<Mark/>`. */
  children: ReactNode;
  /** Where the number came from, or what it excludes. Small, under the sentence. */
  footnote?: ReactNode;
  className?: string;
}

export function Answer({ children, footnote, className }: AnswerProps) {
  return (
    <section
      aria-label="In short"
      className={cn("border-y border-border/70 py-7 animate-rise", className)}
    >
      <p className="max-w-[26ch] font-display text-[1.75rem] font-semibold leading-[1.18] tracking-[-0.03em] sm:max-w-[34ch] sm:text-[2.125rem]">
        {children}
      </p>
      {footnote ? (
        <p className="mt-3 max-w-[70ch] text-sm leading-relaxed text-muted-foreground">
          {footnote}
        </p>
      ) : null}
    </section>
  );
}

/**
 * The highlighter stroke. `box-shadow` rather than padding, so the mark bleeds a little past the
 * word the way a marker pen does and the line height does not change when it is applied.
 */
export function Mark({ children }: { children: ReactNode }) {
  return <span className="marker rounded-[2px]">{children}</span>;
}
