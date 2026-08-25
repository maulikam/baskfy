import type { ScreenDefinition } from "@baskfy/api-client";

/** Count leaf-level field changes between two definitions (for the apply pill). */
export function countDefinitionChanges(
  base: ScreenDefinition,
  candidate: ScreenDefinition,
): number {
  return countJsonDiff(base, candidate);
}

function countJsonDiff(base: Record<string, unknown>, candidate: Record<string, unknown>): number {
  let count = 0;
  const keys = new Set([...Object.keys(base), ...Object.keys(candidate)]);

  for (const key of keys) {
    const left = base[key];
    const right = candidate[key];

    if (isPlainObject(left) && isPlainObject(right)) {
      count += countJsonDiff(left, right);
      continue;
    }
    if (JSON.stringify(left) !== JSON.stringify(right)) count += 1;
  }

  return count;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
