import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every numeric field's own default must satisfy its own `min`/`step` (M42).
 *
 * ## The bug this exists to make impossible
 *
 * "Max weight (%)" shipped `min="0.1" step="0.5"` with a default of `10`. HTML counts `step` from
 * `min`, not from zero, so the valid values were 0.1, 0.6, 1.1 … 9.6, **10.1** — and 10 was not
 * one of them. The form therefore **could not be submitted in the state it loaded in**.
 *
 * What made it expensive rather than merely wrong is how it failed. `checkValidity()` fails before
 * React's `onSubmit` runs, so: no request, no error state, no console warning, nothing in the page
 * text. The only signal was a native browser tooltip that vanishes on the next click. Pressing
 * "Run backtest" did nothing at all, forever, and the interface had no way to say why.
 *
 * That is indistinguishable, from the user's chair, from the queue failure M42 fixed — which is
 * how it survived: the same symptom already had a cause everyone believed.
 *
 * ## Why this is asserted over the source
 *
 * The alternative is rendering the form and calling `checkValidity()`, which needs jsdom to
 * implement step validation — it does not. Parsing the attributes is the only check available
 * that is actually about the constraint rather than about a mock of it, and it catches the same
 * mistake in a field nobody has written yet.
 */
const SOURCE = readFileSync(join(__dirname, "..", "config-form.tsx"), "utf8");

interface NumericField {
  id: string;
  min: number;
  step: number;
  max: number | null;
  stateName: string;
}

/** `useState("10")` for each control, keyed by the variable the input is bound to. */
function defaults(): Map<string, string> {
  const found = new Map<string, string>();
  const pattern = /const \[(\w+), set\w+\] = useState(?:<[^>]*>)?\("([^"]*)"\)/g;
  for (const match of SOURCE.matchAll(pattern)) {
    found.set(match[1] as string, match[2] as string);
  }
  return found;
}

/** Every `<Input>` that declares a `step`, with the attributes that constrain it. */
function numericFields(): NumericField[] {
  const fields: NumericField[] = [];
  for (const chunk of SOURCE.split("<Input").slice(1)) {
    const element = chunk.split("/>")[0] ?? "";
    const step = /step="([^"]+)"/.exec(element);
    const id = /id="([^"]+)"/.exec(element);
    const bound = /value=\{(\w+)\}/.exec(element);
    if (!step || !id || !bound) continue;
    const min = /min="([^"]+)"/.exec(element);
    const max = /max="([^"]+)"/.exec(element);
    fields.push({
      id: id[1] as string,
      min: min ? Number(min[1]) : 0,
      step: Number(step[1]),
      max: max ? Number(max[1]) : null,
      stateName: bound[1] as string,
    });
  }
  return fields;
}

const FIELDS = numericFields();
const DEFAULTS = defaults();

describe("the backtest config form's numeric constraints", () => {
  it("finds the fields at all", () => {
    // A parser that silently matches nothing would make every assertion below vacuous.
    expect(FIELDS.length).toBeGreaterThanOrEqual(6);
    expect(FIELDS.map((field) => field.id)).toContain("bt-max");
  });

  it.each(FIELDS.map((field) => [field.id, field] as const))(
    "%s loads with a value its own step allows",
    (_id, field) => {
      const value = DEFAULTS.get(field.stateName);
      expect(value, `${field.id} is bound to ${field.stateName}, which has no default`).toBeDefined();

      const steps = (Number(value) - field.min) / field.step;
      expect(
        Math.abs(steps - Math.round(steps)),
        `${field.id} defaults to ${value}, but min=${field.min} step=${field.step} makes the ` +
          `nearest valid values ${field.min + field.step * Math.floor(steps)} and ` +
          `${field.min + field.step * (Math.floor(steps) + 1)}. The form cannot be submitted ` +
          "as it loads.",
      ).toBeLessThan(1e-9);
    },
  );

  it.each(FIELDS.map((field) => [field.id, field] as const))(
    "%s loads within its own min and max",
    (_id, field) => {
      const value = Number(DEFAULTS.get(field.stateName));
      expect(value).toBeGreaterThanOrEqual(field.min);
      if (field.max !== null) expect(value).toBeLessThanOrEqual(field.max);
    },
  );
});
