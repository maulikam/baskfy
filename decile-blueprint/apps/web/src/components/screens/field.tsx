"use client";

import { useId, type ReactNode } from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * A labelled form field with optional explanatory microcopy.
 *
 * docs/08 §"Accessibility & quality bar": "Every form field has a real `<label>`; sentinel
 * explanations use `aria-describedby`." This is the shape that makes that true by default —
 * `render` receives the ids it must wire up, so a field cannot be added without them.
 */
export interface FieldProps {
  label: string;
  /** The reference product's own microcopy, where it has any. */
  hint?: string | undefined;
  /** Shown in the accent colour when the field is doing nothing. */
  off?: boolean;
  className?: string | undefined;
  render: (ids: { id: string; describedBy: string | undefined }) => ReactNode;
}

export function Field({ label, hint, off = false, className, render }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id}>{label}</Label>
        {off ? (
          <span
            aria-hidden="true"
            className="rounded-full border border-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
          >
            Off
          </span>
        ) : null}
      </div>
      {render({ id, describedBy: hintId })}
      {hint ? (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
          {off ? " This filter is currently off." : ""}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A labelled switch row — the shape docs/01 §2.3, §2.8 and §2.10 use for their group toggles.
 *
 * `render` receives the id rather than the row cloning its child, because cloning to inject props
 * silently does nothing when the child forwards them somewhere unexpected — and a switch with no
 * label is exactly the defect this component exists to prevent.
 */
export function SwitchRow({
  label,
  hint,
  render,
}: {
  label: string;
  hint?: string | undefined;
  render: (ids: { id: string; describedBy: string | undefined }) => ReactNode;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="space-y-0.5">
        <Label htmlFor={id}>{label}</Label>
        {hint ? (
          <p id={hintId} className="text-xs text-muted-foreground">
            {hint}
          </p>
        ) : null}
      </div>
      <div className="pt-0.5">{render({ id, describedBy: hintId })}</div>
    </div>
  );
}
