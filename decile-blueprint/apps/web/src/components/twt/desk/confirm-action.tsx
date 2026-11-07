"use client";

import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DeskError } from "@/lib/desk-actions/client";

/** Button → dialog → explicit second press → one POST. No retry; the result is shown as-is. */
export function ConfirmAction({
  label,
  title,
  body,
  confirmLabel,
  danger = false,
  run,
  onDone,
  testId,
}: {
  label: string;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  run: () => Promise<string>;
  onDone?: () => void;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function send() {
    if (pending || result) return;
    setPending(true);
    try {
      setResult({ ok: true, text: await run() });
    } catch (e) {
      const text = e instanceof DeskError ? `${e.status}: ${e.message}` : "The desk could not be reached.";
      setResult({ ok: false, text });
    } finally {
      setPending(false);
      onDone?.();
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setResult(null);
      }}
    >
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant={danger ? "destructive" : "outline"} data-testid={testId}>
          {label}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg space-y-4">
        <DialogTitle className="text-base font-semibold">{title}</DialogTitle>
        <DialogDescription asChild>
          <div className="space-y-2 text-sm text-muted-foreground">{body}</div>
        </DialogDescription>
        {result ? (
          <p role="status" className={result.ok ? "text-sm" : "text-sm text-negative"}>
            {result.text}
          </p>
        ) : (
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              variant={danger ? "destructive" : "primary"}
              disabled={pending}
              onClick={() => void send()}
            >
              {pending ? "Sending…" : confirmLabel}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
