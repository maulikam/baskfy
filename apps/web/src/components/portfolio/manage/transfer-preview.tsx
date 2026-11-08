"use client";

import { ArrowRight, CircleAlert, Eye, Minus, Plus } from "lucide-react";

import { MetricLine, Notice } from "@/components/portfolio/manage/panel-chrome";
import type { AssignmentPreview, AssignmentSide } from "@/lib/portfolio/manage";
import { formatShares } from "@/lib/portfolio/organize";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * What a transfer is about to do, to **both** sides, before anything is written.
 *
 * THE RULE THIS COMPONENT IS
 * --------------------------
 * A move has two halves and only one of them is the half the user initiated. They opened
 * "Move holdings" because they want *Momentum* to have ITC; the portfolio that is about to get
 * smaller is the one they were not looking at. A preview that shows only the gain is a form that
 * has hidden half of what it does, and in a product whose capital portfolios are supposed to add
 * up to net worth to the paisa, the hidden half is where a wrong total comes from.
 *
 * So the source is not a footnote. It is the same size as the destination, it is rendered first —
 * what leaves, then what arrives — and it carries a minus sign as well as a colour, so the
 * direction survives greyscale and a reader who cannot separate the two hues.
 *
 * Every figure goes through {@link MetricLine}: a selection with no prices renders its reason,
 * never a dash and never a plausible zero.
 */

function SideCard({
  side,
  direction,
  testId,
}: {
  side: AssignmentSide;
  direction: "loses" | "gains" | "watches";
  testId: string;
}) {
  const Icon = direction === "loses" ? Minus : direction === "gains" ? Plus : Eye;
  const word = direction === "loses" ? "Leaves" : direction === "gains" ? "Arrives in" : "Watched by";
  return (
    <div
      data-testid={testId}
      data-side={direction}
      className="min-w-0 flex-1 space-y-2 rounded-lg border border-border bg-background p-3"
    >
      <p className="flex items-center gap-1.5 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        <Icon
          aria-hidden="true"
          className={cn(
            "size-3.5",
            direction === "loses" ? "text-negative" : direction === "gains" ? "text-positive" : "",
          )}
        />
        {word}
      </p>
      <p className="truncate text-sm font-semibold" title={side.label}>
        {side.label}
      </p>
      <MetricLine metric={side.value} />
      <p className="text-xs leading-snug text-muted-foreground">{side.sentence}</p>
    </div>
  );
}

export function TransferPreview({ preview }: { preview: AssignmentPreview }) {
  const { source, destination, lines, blocks } = preview;

  return (
    <section
      aria-label="What this will do"
      data-testid="transfer-preview"
      data-intent={preview.intent}
      data-can-commit={preview.canCommit ? "yes" : "no"}
      className="space-y-3 rounded-xl border border-border bg-card p-3"
    >
      <h4 className="text-sm font-semibold">Before anything is written</h4>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
        {source === null ? null : (
          <>
            <SideCard side={source} direction="loses" testId="preview-source" />
            <ArrowRight
              aria-hidden="true"
              className="hidden size-4 shrink-0 self-center text-muted-foreground sm:block"
            />
          </>
        )}
        <SideCard
          side={destination}
          direction={preview.intent === "WATCH" ? "watches" : "gains"}
          testId="preview-destination"
        />
      </div>

      {lines.length > 0 ? (
        <ul className="divide-y divide-border/60 text-xs" data-testid="preview-lines">
          {lines.map((line) => (
            <li key={line.keyId} className="flex flex-wrap items-baseline gap-x-2 py-1.5">
              <span className="font-medium">{line.symbol}</span>
              <span className="text-muted-foreground">{line.brokerLabel}</span>
              <span className="tabular-nums">
                {preview.intent === "WATCH"
                  ? "all of it — a view records names, not share counts"
                  : `${formatShares(line.quantity)} shares`}
              </span>
              <span className="ml-auto tabular-nums">
                {line.value === null ? (
                  /* Never a bare dash: the reason stands where the figure would. */
                  <span className="text-muted-foreground" title={line.unpricedReason ?? undefined}>
                    No price today
                  </span>
                ) : (
                  formatRupees(line.value)
                )}
              </span>
              {line.heldBy.length > 0 ? (
                <span className="w-full text-muted-foreground">
                  Currently{" "}
                  {line.heldBy
                    .map((holder) => `${formatShares(holder.quantity)} in ${holder.name}`)
                    .join(", ")}
                </span>
              ) : (
                <span className="w-full text-muted-foreground">Currently unallocated</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {blocks.length > 0 ? (
        <ul className="space-y-2" data-testid="preview-blocks">
          {blocks.map((block) => (
            <li
              key={block.keyId}
              data-testid={`preview-block-${block.symbol}`}
              className="flex items-start gap-2 rounded-lg border border-negative/40 bg-negative-muted px-3 py-2 text-xs leading-relaxed"
            >
              <CircleAlert aria-hidden="true" className="mt-px size-3.5 shrink-0 text-negative" />
              <span>
                {/* The word as well as the colour — §6.2 rule 3. */}
                <strong className="font-semibold">
                  {preview.intent === "MOVE" ? "Cannot be moved. " : "Cannot be assigned. "}
                </strong>
                {block.sentence} {block.remedy}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {preview.ownershipNotice === null ? null : (
        <Notice testId="ownership-notice">{preview.ownershipNotice}</Notice>
      )}
      <Notice testId="exclusivity-notice">{preview.exclusivityNotice}</Notice>
      <Notice testId="net-worth-notice">{preview.netWorthNotice}</Notice>
    </section>
  );
}
