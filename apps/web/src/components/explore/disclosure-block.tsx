import { Info } from "lucide-react";

import { cn } from "@/lib/utils";

export type DisclosureVariant =
  | "performance-not-verified"
  | "registration-pending"
  | "history-caveat";

const COPY: Record<DisclosureVariant, { title: string; body: string }> = {
  "performance-not-verified": {
    title: "Performance not verified",
    body:
      "Past returns shown here are computed from published closes and basket weights. They are " +
      "not audited by a third party and are not a forecast of future results.",
  },
  "registration-pending": {
    title: "Registration pending",
    body:
      "The manager line is shown as recorded. SEBI registration is pending or not yet supplied — " +
      "this is not a claim that the strategy is a registered product.",
  },
  "history-caveat": {
    title: "Short history",
    body:
      "This basket is younger than some return windows. Missing CAGR figures mean the window " +
      "has not elapsed yet, not that the return is zero.",
  },
};

/**
 * Catalog disclosures — components, not footers (house rule 9 / 05-ui-spec).
 */
export function DisclosureBlock({
  variant,
  className,
}: {
  variant: DisclosureVariant;
  className?: string;
}) {
  const copy = COPY[variant];
  return (
    <aside
      aria-label={copy.title}
      className={cn(
        "flex gap-2 rounded-md border border-border bg-muted/50 p-3 text-xs leading-relaxed text-muted-foreground",
        className,
      )}
    >
      <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <div className="space-y-1">
        <p className="font-medium text-foreground">{copy.title}</p>
        <p>{copy.body}</p>
      </div>
    </aside>
  );
}
