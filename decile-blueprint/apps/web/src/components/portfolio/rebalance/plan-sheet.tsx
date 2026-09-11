"use client";

import { Check, ClipboardCopy, FileText } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { planText, type RebalancePreview } from "@/lib/portfolio/rebalance-preview";

/**
 * Step five: the artefact.
 *
 * *"Generate an execution plan"* — and this is what one honestly is here. A document: the lists,
 * the rule that put each name on one, the target weights, the reader's own notes, and the list of
 * figures it does not carry with where each comes from instead. A person reads it, copies it, and
 * enters the lines at their broker.
 *
 * **There is no send control, and there is nothing for one to call.** Baskfy's web app has no
 * route to a broker order — the desk's order gateway is a separate service behind the desk's own
 * non-negotiables, reached from the desk, never from here. The weekly rebalancer in particular
 * has never had an automatic path and is not gaining one.
 *
 * The copy button writes to the clipboard and nothing else. Where the clipboard is unavailable —
 * an insecure origin, a browser that refuses — the text is on the page to select by hand, which
 * is why it is rendered rather than only offered.
 */
export function PlanSheet({
  preview,
  notes,
}: {
  preview: RebalancePreview;
  notes: ReadonlyMap<number, string>;
}) {
  const [copied, setCopied] = useState(false);
  const text = planText({ preview, notes });

  function copy() {
    const clipboard = navigator.clipboard;
    if (!clipboard) return;
    void clipboard.writeText(text).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  }

  return (
    <section aria-label="Plan" data-testid="plan-sheet" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <FileText aria-hidden="true" className="size-4" />
          Your plan
        </h3>
        <p className="text-xs text-muted-foreground">
          Take it to your broker. Nothing on this screen sends it anywhere.
        </p>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          data-testid="copy-plan"
          onClick={copy}
        >
          {copied ? (
            <Check aria-hidden="true" />
          ) : (
            <ClipboardCopy aria-hidden="true" />
          )}
          {copied ? "Copied" : "Copy plan"}
        </Button>
      </div>

      <pre
        data-testid="plan-text"
        className="max-h-[48vh] overflow-auto rounded-xl border border-border bg-muted/40 p-3 font-mono text-[0.6875rem] leading-relaxed"
      >
        {text}
      </pre>
    </section>
  );
}
