"use client";

import { useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DeskError } from "@/lib/desk-actions/client";
import { DeskBuyError } from "@/lib/opportunities/desk-buy";
import { cn } from "@/lib/utils";

export interface ActionResult {
  tone: "ok" | "bad";
  text: string;
  /** True leaves the button live (a BLOCKED guard refusal changes nothing at the broker). */
  retryable?: boolean;
}

/**
 * One desk POST behind an explicit confirm step. The trigger opens a dialog; only the dialog's
 * confirm button sends, once. No auto-submit, no retry.
 */
export function ConfirmAction({
  label,
  title,
  confirmLabel,
  danger,
  disabled,
  disabledReason,
  run,
  onDone,
  children,
}: {
  label: string;
  title: string;
  confirmLabel: string;
  danger?: boolean;
  disabled?: boolean;
  disabledReason?: string;
  run: () => Promise<ActionResult>;
  onDone?: () => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ActionResult | null>(null);
  const sent = useRef(false);
  const spent = result !== null && !result.retryable;

  async function send() {
    if (sent.current) return;
    sent.current = true;
    setBusy(true);
    let next: ActionResult;
    try {
      next = await run();
    } catch (e: unknown) {
      const status = e instanceof DeskError || e instanceof DeskBuyError ? e.status : null;
      next = {
        tone: "bad",
        text: `${status ? `${status} ` : ""}not executed — ${e instanceof Error ? e.message : "request failed"}`,
      };
    }
    setBusy(false);
    setResult(next);
    if (next.retryable) sent.current = false;
    onDone?.();
  }

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <Dialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o);
          if (o && result?.retryable) setResult(null);
        }}
      >
        <DialogTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant={danger ? "destructive" : "outline"}
            disabled={disabled || spent}
            title={disabled ? disabledReason : title}
          >
            {spent ? "done" : label}
          </Button>
        </DialogTrigger>
        <DialogContent className="max-h-[90vh] space-y-4 overflow-y-auto">
          <DialogTitle className="text-base font-semibold">{title}</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-2 text-sm text-muted-foreground">{children}</div>
          </DialogDescription>
          {result ? (
            <p className={cn("text-sm", result.tone === "bad" ? "text-negative" : "text-positive")} aria-live="polite">
              {result.text}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>
              {result ? "Close" : "Cancel"}
            </Button>
            {result && !result.retryable ? null : (
              <Button
                type="button"
                size="sm"
                variant={danger ? "destructive" : "secondary"}
                disabled={busy}
                onClick={() => void send()}
              >
                {busy ? "Sending…" : confirmLabel}
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
      {result ? (
        <span className={cn("text-xs", result.tone === "bad" ? "text-negative" : "text-muted-foreground")} aria-live="polite">
          {result.text}
        </span>
      ) : null}
    </span>
  );
}
