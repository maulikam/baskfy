import { useId } from "react";

import { Input } from "@/components/ui/input";

export function NumberField({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  suffix,
}: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  step?: number;
  min?: number;
  max?: number;
  suffix?: string;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
        {suffix ? <span className="ml-1 text-quiet-foreground">({suffix})</span> : null}
      </label>
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        step={step}
        {...(min === undefined ? {} : { min })}
        {...(max === undefined ? {} : { max })}
        value={value === null || Number.isNaN(value) ? "" : value}
        onChange={(event) => {
          const raw = event.target.value;
          onChange(raw === "" ? null : Number(raw));
        }}
        className="tabular-nums"
      />
    </div>
  );
}

export function CheckboxGroup({
  legend,
  options,
  selected,
  onChange,
}: {
  legend: string;
  options: readonly { value: string; label: string }[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="flex flex-col gap-1">
      <legend className="mb-1 text-xs font-medium text-muted-foreground">{legend}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {options.map((option) => {
          const checked = selected.includes(option.value);
          return (
            <label key={option.value} className="inline-flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                className="size-4 accent-[var(--accent)]"
                checked={checked}
                onChange={() =>
                  onChange(
                    checked
                      ? selected.filter((value) => value !== option.value)
                      : [...selected, option.value],
                  )
                }
              />
              {option.label}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

export const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-card px-3 py-1 text-sm";
