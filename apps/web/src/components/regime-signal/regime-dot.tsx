"use client";

import { useState, type MouseEvent } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { formatNumber } from "@/lib/format";
import { FALLBACK_LINE, type RegimeSignal } from "@/lib/regime-signal/types";
import { cn } from "@/lib/utils";

/** The per-stock market regime: a green/red dot; amber ring when it fell back to the Smallcap rule. */
export function RegimeDot({ signal }: { signal: RegimeSignal | null | undefined }) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);

  if (!signal) {
    return (
      <span
        className="text-xs text-muted-foreground"
        aria-label="Market regime: not computed yet"
        data-testid="regime-dot"
        data-signal="none"
      >
        —
      </span>
    );
  }

  const green = signal.signal === "GREEN";
  const word = green ? "green" : "red";
  const triggered = new Set(signal.triggered);

  // Hover opens; a click or tap pins it open until clicked again or dismissed.
  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const next = !pinned;
    setPinned(next);
    setOpen(next);
  };

  return (
    <Popover
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (!value) setPinned(false);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Market regime: ${word}${signal.fallback ? " (Smallcap fallback)" : ""}`}
          data-testid="regime-dot"
          data-signal={signal.signal}
          data-fallback={signal.fallback ? "true" : undefined}
          className="inline-grid size-6 place-items-center rounded-full align-middle hover:bg-muted"
          onClick={onClick}
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => {
            if (!pinned) setOpen(false);
          }}
        >
          <span
            aria-hidden="true"
            className={cn(
              "block size-2.5 rounded-full",
              green ? "bg-positive" : "bg-negative",
              signal.fallback && "ring-2 ring-warning ring-offset-1 ring-offset-background",
            )}
          />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="right"
        className="w-72 space-y-2 p-3 text-xs"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <p className="font-medium">
          Market regime: <span className={green ? "text-positive" : "text-negative"}>{word}</span>
        </p>
        {signal.fallback ? <p className="text-warning">{FALLBACK_LINE}</p> : null}
        {signal.indices.length > 0 ? (
          <ul className="space-y-1.5">
            {signal.indices.map((index) => (
              <li key={`${index.via}-${index.name}`} className="space-y-0.5">
                <span className="flex items-center justify-between gap-2">
                  <span className="font-medium">{index.name}</span>
                  <span className="text-muted-foreground">{index.via}</span>
                </span>
                <span className="flex flex-wrap items-center gap-x-2 tabular-nums text-muted-foreground">
                  EMA10 {formatNumber(index.ema10, { decimals: 2 })} vs EMA20{" "}
                  {formatNumber(index.ema20, { decimals: 2 })}
                  <span className={index.up ? "text-positive" : "text-negative"}>
                    {index.up ? "up" : "down"}
                  </span>
                  {triggered.has(index.name) ? (
                    <span className="font-medium text-positive">✓ made it green</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
