"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { errorText, readDeskPage } from "@/lib/desk-ops/client";
import { cn } from "@/lib/utils";

export function StaffOnly({ what }: { what: string }) {
  return (
    <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
      <p className="max-w-[46ch] text-sm leading-relaxed text-muted-foreground">
        {what} is an operator page. It is shown to staff accounts only.
      </p>
    </div>
  );
}

export type BannerTone = "ok" | "bad" | "warn" | "info";

const TONES: Record<BannerTone, string> = {
  ok: "border-positive/35 bg-positive-muted",
  bad: "border-negative/35 bg-negative-muted",
  warn: "border-warning/35 bg-warning-muted",
  info: "border-border/70 bg-card",
};

export function Banner({
  tone,
  tag,
  children,
}: {
  tone: BannerTone;
  tag?: string;
  children: ReactNode;
}) {
  return (
    <div role="status" className={cn("rounded-xl border p-3.5 text-sm leading-relaxed", TONES[tone])}>
      {tag ? <span className="eyebrow mr-2">{tag}</span> : null}
      {children}
    </div>
  );
}

export function Card({ title, children, className }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-xl border border-border/70 bg-card p-4", className)}>
      {title ? <h2 className="mb-3 text-base font-semibold">{title}</h2> : null}
      {children}
    </section>
  );
}

export function Muted({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-xs leading-relaxed text-muted-foreground", className)}>{children}</p>;
}

/** Read one desk page's JSON; `reload` re-reads, `every` polls while true. */
export function useDeskPage<T>(path: string, every?: (data: T) => number | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const next = await readDeskPage<T>(path);
      setData(next);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [path]);

  useEffect(() => {
    let live = true;
    readDeskPage<T>(path).then(
      (next) => {
        if (!live) return;
        setData(next);
        setError(null);
      },
      (e: unknown) => {
        if (live) setError(errorText(e));
      },
    );
    return () => {
      live = false;
    };
  }, [path]);

  const interval = data && every ? every(data) : null;
  useEffect(() => {
    if (!interval) return;
    const id = window.setInterval(() => void reload(), interval);
    return () => window.clearInterval(id);
  }, [interval, reload]);

  return { data, error, reload };
}

/**
 * Two-step action: the first click arms it and says what will happen, the second sends.
 * No retry: a failed send shows the desk's answer and returns to idle.
 */
export function ConfirmAction({
  label,
  confirmLabel,
  warning,
  disabled,
  variant = "outline",
  onConfirm,
}: {
  label: string;
  confirmLabel: string;
  warning: ReactNode;
  disabled?: boolean;
  variant?: "outline" | "primary" | "destructive";
  onConfirm: () => Promise<void>;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!armed) {
    return (
      <Button type="button" size="sm" variant={variant} disabled={disabled || busy} onClick={() => setArmed(true)}>
        {label}
      </Button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning-muted p-2 text-xs">
      <span className="min-w-0 flex-1">{warning}</span>
      <Button
        type="button"
        size="sm"
        variant={variant === "outline" ? "primary" : variant}
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void onConfirm().finally(() => {
            setBusy(false);
            setArmed(false);
          });
        }}
      >
        {busy ? "Sending…" : confirmLabel}
      </Button>
      <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setArmed(false)}>
        Cancel
      </Button>
    </div>
  );
}

/** A labelled control; `id` ties the label to it. */
export function Field({ id, label, className, children }: { id: string; label: string; className?: string; children: ReactNode }) {
  return (
    <div className={cn("flex flex-col gap-1 text-xs", className)}>
      <label htmlFor={id}>{label}</label>
      {children}
    </div>
  );
}

export function fmtInr(value: number | string | null | undefined, digits = 0): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}
