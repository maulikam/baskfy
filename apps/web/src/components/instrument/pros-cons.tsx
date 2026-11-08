import { Check, HelpCircle, X } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * docs/01 §5 block 3, rendered per docs/08: "**PROS/CONS** — green/red chips with icons,
 * generated from the rule table in `05 §16`."
 *
 * The rule table lives in `packages/core/src/baskfy_core/pros_cons.py` and reaches this component
 * as finished sentences in the payload — docs/05 §16: "Keep the rule table in one module so it
 * stays consistent between API and UI." Rewording anything here would break that.
 *
 * `undecided` has no counterpart in the reference product and is the honest half of the same
 * rule table: a rule whose input is NULL yields neither a PRO nor a CON, and a four-month-old
 * listing showing two chips instead of eight should say why rather than look broken.
 */
export interface ProsConsProps {
  pros: readonly string[];
  cons: readonly string[];
  undecidedCount: number;
}

function Chip({ tone, children }: { tone: "pro" | "con"; children: string }) {
  const Icon = tone === "pro" ? Check : X;
  return (
    <li
      className={cn(
        "flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-sm",
        tone === "pro"
          ? "border-positive/30 bg-positive-muted text-positive"
          : "border-negative/30 bg-negative-muted text-negative",
      )}
    >
      <Icon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span className="text-foreground">{children}</span>
    </li>
  );
}

export function ProsCons({ pros, cons, undecidedCount }: ProsConsProps) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <section aria-labelledby="pros-heading">
        <h3 id="pros-heading" className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Pros
        </h3>
        {pros.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {pros.map((line) => (
              <Chip key={line} tone="pro">
                {line}
              </Chip>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No rule is satisfied for this instrument.</p>
        )}
      </section>

      <section aria-labelledby="cons-heading">
        <h3 id="cons-heading" className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Cons
        </h3>
        {cons.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {cons.map((line) => (
              <Chip key={line} tone="con">
                {line}
              </Chip>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No rule is violated for this instrument.</p>
        )}
      </section>

      {undecidedCount > 0 ? (
        <p className="flex items-start gap-2 text-xs text-muted-foreground sm:col-span-2">
          <HelpCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {undecidedCount} {undecidedCount === 1 ? "rule needs" : "rules need"} data this
            instrument does not have yet, so {undecidedCount === 1 ? "it is" : "they are"} shown as
            neither a pro nor a con.
          </span>
        </p>
      ) : null}
    </div>
  );
}
