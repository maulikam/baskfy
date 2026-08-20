import { ScreenDefinitionSchema, type ScreenDefinition } from "@decile/api-client";

import { defaultDefinition } from "@/lib/screens/defaults";

/**
 * The form state, mirrored into the URL — Prompt 9 deliverable 4 and docs/08 §"Screen editor":
 *
 *     "Entire form state is mirrored into the URL via `nuqs` → shareable, back-button-correct."
 *
 * **A sparse diff, not the whole object.** A full `ScreenDefinition` is around forty fields and
 * seven hundred characters of JSON; putting all of it in the query string would make every URL
 * unreadable and most of it noise, because a typical screen changes four things. `encodeState`
 * therefore writes only what differs from the screen's saved definition, and `decodeState` layers
 * that back on top. Round-tripping is exact by construction: the same base plus the same diff.
 *
 * The diff is compared against the *screen's own* definition rather than against
 * `defaultDefinition()`, so a shared link to an unmodified screen has no parameter at all, and one
 * to a modified screen carries exactly the modifications.
 */
export const STATE_PARAM = "f";

/** A structural view of a definition, for the generic diff/merge below. */
type Json = Record<string, unknown>;

function isPlainObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The parts of `candidate` that differ from `base`, one level into nested objects.
 *
 * Nested groups (`moving_average`, `positive_days`, …) diff field by field so that changing one
 * switch does not put the whole group in the URL. Arrays (`series`, `custom_filters`) are compared
 * whole, because a positional diff of an ordered list is a bug generator and both are short.
 */
export function diffDefinition(base: Json, candidate: Json): Json {
  const out: Json = {};
  for (const [key, value] of Object.entries(candidate)) {
    const original = base[key];
    if (isPlainObject(value) && isPlainObject(original)) {
      const nested = diffDefinition(original, value);
      if (Object.keys(nested).length > 0) out[key] = nested;
      continue;
    }
    if (JSON.stringify(value) !== JSON.stringify(original)) out[key] = value;
  }
  return out;
}

function mergeDeep(base: Json, patch: Json): Json {
  const out: Json = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    const original = out[key];
    out[key] = isPlainObject(value) && isPlainObject(original) ? mergeDeep(original, value) : value;
  }
  return out;
}

/** The URL value for `candidate` given `base`, or `null` when they are the same. */
export function encodeState(base: ScreenDefinition, candidate: ScreenDefinition): string | null {
  const diff = diffDefinition(base, candidate);
  return Object.keys(diff).length === 0 ? null : JSON.stringify(diff);
}

/**
 * `base` plus whatever the URL carried.
 *
 * Anything unparseable is discarded in favour of `base`. A hand-edited or truncated URL should
 * open the saved screen, not an error page — and a definition that failed validation here would
 * be rejected by the API a moment later anyway.
 */
export function decodeState(base: ScreenDefinition, raw: string | null): ScreenDefinition {
  if (!raw) return base;
  try {
    const patch: unknown = JSON.parse(raw);
    if (!isPlainObject(patch)) return base;
    const merged = mergeDeep(base, patch);
    const parsed = ScreenDefinitionSchema.safeParse(merged);
    return parsed.success ? parsed.data : base;
  } catch {
    return base;
  }
}

/** A definition parsed from an unknown server payload, falling back to the defaults. */
export function parseDefinition(raw: unknown): ScreenDefinition {
  const parsed = ScreenDefinitionSchema.safeParse(raw);
  return parsed.success ? parsed.data : defaultDefinition();
}

export function definitionsEqual(a: ScreenDefinition, b: ScreenDefinition): boolean {
  return (
    Object.keys(diffDefinition(a, b)).length === 0 &&
    Object.keys(diffDefinition(b, a)).length === 0
  );
}
