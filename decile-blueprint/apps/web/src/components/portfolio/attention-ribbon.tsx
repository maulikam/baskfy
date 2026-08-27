"use client";

import { X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { formatTradeDate } from "@/lib/format";
import { attentionLink, type Attention } from "@/lib/portfolio/overview";

/**
 * §6.4 — the needs-attention ribbon.
 *
 * *"Dismissible, each item deep-links to its resolution flow."* Both halves matter:
 *
 * * **Deep-link, not a label.** An item that only announces a problem leaves the reader hunting
 *   for the screen that fixes it. `AttentionKind` names no route on purpose — core naming a URL
 *   would be core knowing about the web app — so `attentionLink` resolves each kind against the
 *   route table, and every item ends in a link with a verb on it.
 * * **Dismissible, for this visit only.** Dismissal is component state and is not written
 *   anywhere. A reconciliation item that is still open tomorrow is still a number being withheld
 *   from the user (§4.3), and a ribbon that remembers "do not tell me" is a ribbon that stops
 *   telling them something true. Hiding it while they read the rest of the page is a courtesy;
 *   hiding it permanently would be the product taking a side.
 *
 * The dismissed count stays visible with a way back, so nothing disappears without a trace.
 */

/** The API's `message` already carries the count; the ribbon adds when the condition started. */
function since(item: Attention): string | null {
  return item.since ? `Since ${formatTradeDate(item.since)}` : null;
}

export function AttentionRibbon({ items }: { items: readonly Attention[] }) {
  const [dismissed, setDismissed] = useState<readonly string[]>([]);
  const shown = items.filter((item) => !dismissed.includes(item.kind));

  if (items.length === 0) return null;

  return (
    <section aria-label="Needs attention" data-testid="attention-ribbon" className="space-y-2">
      <ul className="space-y-2">
        {shown.map((item) => {
          const link = attentionLink(item.kind);
          const when = since(item);
          return (
            <li
              key={item.kind}
              data-testid="attention-item"
              data-kind={item.kind}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning/40 bg-warning-muted px-4 py-2.5"
            >
              <div className="min-w-0">
                <p className="text-sm text-foreground">{item.message}</p>
                {when ? <p className="mt-0.5 text-xs text-muted-foreground">{when}</p> : null}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button variant="outline" size="sm" asChild>
                  <Link href={link.href} data-testid="attention-link">
                    {link.action}
                  </Link>
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-8"
                  aria-label={`Dismiss: ${item.message}`}
                  data-testid="attention-dismiss"
                  onClick={() => setDismissed((current) => [...current, item.kind])}
                >
                  <X aria-hidden="true" className="size-4" />
                </Button>
              </div>
            </li>
          );
        })}
      </ul>

      {dismissed.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          {dismissed.length} hidden for now.{" "}
          <button
            type="button"
            data-testid="attention-restore"
            onClick={() => setDismissed([])}
            className="text-accent underline-offset-4 hover:underline"
          >
            Show them again
          </button>
        </p>
      ) : null}
    </section>
  );
}
