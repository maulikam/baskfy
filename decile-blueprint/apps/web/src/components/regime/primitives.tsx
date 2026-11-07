import type { ReactNode } from "react";

import type { TierKey } from "@/components/regime/types";
import { cn } from "@/lib/utils";

export const DASH = "—";

export function f(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? DASH : v.toFixed(d);
}

export function signed(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? DASH : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`;
}

export function grouped(v: number | null | undefined, d = 0): string {
  return v === null || v === undefined
    ? DASH
    : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function inr(v: number | null | undefined): string {
  return v === null || v === undefined ? DASH : `₹${grouped(v)}`;
}

export const TONE = {
  ok: "text-positive",
  bad: "text-negative",
  warn: "text-warning",
  hold: "text-info",
  na: "text-muted-foreground",
  none: "",
} as const;
export type Tone = keyof typeof TONE;

export const TIER_TEXT: Record<TierKey, string> = {
  on: "text-positive",
  caution: "text-warning",
  off: "text-negative",
  crash: "text-negative",
};

export const TIER_BG: Record<TierKey, string> = {
  on: "bg-positive",
  caution: "bg-warning",
  off: "bg-negative/70",
  crash: "bg-negative",
};

export function tierText(key: string | null | undefined): string {
  return key && key in TIER_TEXT ? TIER_TEXT[key as TierKey] : "";
}

export function tierBg(key: string | null | undefined): string {
  return key && key in TIER_BG ? TIER_BG[key as TierKey] : "bg-muted";
}

export function stateTone(state: string): string {
  if (state === "above") return TONE.ok;
  if (state === "below") return TONE.bad;
  return TONE.na;
}

const PILL: Record<"warn" | "hold" | "ok" | "mute" | "bad", string> = {
  warn: "border-warning/40 bg-warning-muted text-warning",
  hold: "border-info/40 bg-info-muted text-info",
  ok: "border-positive/40 bg-positive-muted text-positive",
  bad: "border-negative/40 bg-negative-muted text-negative",
  mute: "border-border bg-muted text-muted-foreground",
};

export function Pill({ kind, children }: { kind: keyof typeof PILL; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-1.5 py-px text-[10.5px] font-medium leading-4",
        PILL[kind],
      )}
    >
      {children}
    </span>
  );
}

export function Card({ title, children, className }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0 rounded-xl border border-border/70 bg-card p-4 sm:p-5", className)}>
      {title ? <h3 className="mb-3 text-sm font-semibold">{title}</h3> : null}
      {children}
    </div>
  );
}

export function Section({ title, id, children }: { title?: string; id?: string; children: ReactNode }) {
  return (
    <section id={id} className="flex scroll-mt-20 flex-col gap-3">
      {title ? <h2 className="text-base font-semibold">{title}</h2> : null}
      {children}
    </section>
  );
}

export function Kv({ label, children, tone }: { label: string; children: ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/50 py-1.5 text-sm last:border-0">
      <span className="text-muted-foreground">{label}</span>
      <b className={cn("text-right font-medium tabular-nums", tone)}>{children}</b>
    </div>
  );
}

export function Muted({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-xs leading-relaxed text-muted-foreground", className)}>{children}</p>;
}

export function DataTable({
  label,
  head,
  children,
}: {
  label: string;
  head?: readonly string[];
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table aria-label={label} className="w-full text-sm tabular-nums">
        {head ? (
          <thead>
            <tr className="border-b border-border text-left text-xs font-medium text-muted-foreground">
              {head.map((h) => (
                <th key={h} scope="col" className="whitespace-nowrap py-2 pl-2 pr-3 first:pl-0">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
        ) : null}
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Tr({ children }: { children: ReactNode }) {
  return <tr className="border-b border-border/50 align-top last:border-0">{children}</tr>;
}

export function Tc({
  children,
  tone,
  className,
  colSpan,
}: {
  children?: ReactNode;
  tone?: string;
  className?: string;
  colSpan?: number;
}) {
  return (
    <td colSpan={colSpan} className={cn("py-2 pl-2 pr-3 first:pl-0", tone, className)}>
      {children}
    </td>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[11.5px] text-muted-foreground">{children}</code>;
}
