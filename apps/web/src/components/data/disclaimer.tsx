import { Info } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * docs/11 §"Compliance & legal (India)":
 *
 *     "**Not a SEBI-registered investment adviser.** A `<Disclaimer/>` component on every
 *      analytics surface, in the footer, and on checkout."
 *
 * docs/08 §"Design principles" repeats it: "Every analytics surface renders the `<Disclaimer/>`
 * component." CLAUDE.md house rule 9 states the same thing as an engineering rule —
 * "Disclaimers are components, not footers" — which is why this is a component with a required
 * place in the layout rather than a string in a footer partial that a new page can forget.
 *
 * The wording is fixed. It is a regulatory statement, not copy to be tuned.
 */
export const DISCLAIMER_TEXT =
  "Decile is not a SEBI-registered investment adviser. Everything here is factual analysis of " +
  "published market data, not investment advice, and no output is a recommendation to buy or " +
  "sell any security.";

export type DisclaimerVariant = "inline" | "block";

export interface DisclaimerProps {
  variant?: DisclaimerVariant;
  className?: string | undefined;
}

export function Disclaimer({ variant = "inline", className }: DisclaimerProps) {
  return (
    <aside
      aria-label="Regulatory disclaimer"
      className={cn(
        "flex gap-2 text-xs leading-relaxed text-muted-foreground",
        variant === "block" && "rounded-md border border-border bg-muted/50 p-3",
        className,
      )}
    >
      <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <p>{DISCLAIMER_TEXT}</p>
    </aside>
  );
}
