"use client";

import { useState, type ComponentProps } from "react";

import { Input } from "@/components/ui/input";

/**
 * A number input that keeps what the user typed while the definition refuses it.
 *
 * The Ranking and Factor Ranges sections only patch a valid screen (`checkPatch`). A plain
 * controlled input bound to the definition would snap back the moment a keystroke made the screen
 * invalid — clearing the last bound of a range, typing "-" before a digit — and the user could
 * never get through the intermediate state. This holds the text locally, reports each parsable
 * value (blank is `null`), and follows the definition again whenever it changes from outside.
 */
export interface DraftNumberInputProps
  extends Omit<ComponentProps<"input">, "value" | "onChange" | "type"> {
  value: number | null;
  onValue: (value: number | null) => void;
}

function toText(value: number | null): string {
  return value === null ? "" : String(value);
}

function parse(text: string): number | null | undefined {
  if (text.trim() === "") return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : undefined;
}

export function DraftNumberInput({ value, onValue, className, ...props }: DraftNumberInputProps) {
  const [text, setText] = useState(toText(value));
  const [seen, setSeen] = useState(value);

  // Follow an outside change during render (React's "adjusting state when a prop changes").
  if (seen !== value) {
    setSeen(value);
    if (parse(text) !== value) setText(toText(value));
  }

  return (
    <Input
      {...props}
      type="number"
      inputMode="decimal"
      className={className ?? "tnum"}
      value={text}
      onChange={(event) => {
        const next = event.currentTarget.value;
        setText(next);
        const parsed = parse(next);
        if (parsed !== undefined) onValue(parsed);
      }}
    />
  );
}
