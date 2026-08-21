/**
 * Fails the build if the Zod ScreenDefinition drifts from the Pydantic one
 * (PROMPTS.md Prompt 1 deliverable 6).
 *
 * Two independent checks, because either alone has a blind spot:
 *
 * 1. STRUCTURAL — both sides are reduced to JSON Schema and flattened into one digest per field
 *    path (types, required, default, enum). This catches a field added, removed, renamed,
 *    re-typed, made required, or given a different default on one side only.
 *
 *    The digest deliberately ignores `pattern`, `minLength`, `maxLength` and `format`: Pydantic
 *    and Zod express those differently even when they mean the same thing, so comparing them
 *    would produce noise rather than signal. Those constraints are covered by check 2 instead.
 *    For the same reason it ignores `default` on object and array nodes — Pydantic omits
 *    `default_factory` results from its JSON Schema entirely while Zod emits the un-expanded
 *    input (`{}`) — and check 3 compares the materialised objects instead, which is the thing
 *    that actually has to match.
 *
 * 3. MATERIALISED — every accepted corpus case is parsed here and must produce byte-identical
 *    output to what Pydantic produced for the same input. This catches a default that differs
 *    in value rather than in presence.
 *
 * 2. BEHAVIOURAL — every case in tests/fixtures/screen-definition-corpus.json is validated here
 *    and must produce the same accept/reject verdict Pydantic produced in
 *    packages/core/tests/test_screen_definition_parity.py. This is what catches a *validator*
 *    that only one side implements: the sentinel bounds, the three-slot limit, the
 *    above/below-MA contradiction, the factor-three ordering rule.
 *
 * Regenerate the Python-side inputs with `make schema` and
 * `uv run python -m baskfy_core.screen_definition_corpus`.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { z } from "zod";

import { ScreenDefinitionSchema } from "../src/screen-definition.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../../..");

type JsonSchema = Record<string, unknown>;

interface FieldDigest {
  types: string[];
  required: boolean;
  default: string | undefined;
  enum: string | undefined;
}

const readJson = (path: string): unknown => JSON.parse(readFileSync(path, "utf8"));

const isObject = (value: unknown): value is JsonSchema =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Follow `$ref` into `$defs`, and unwrap the single-branch wrappers both generators emit. */
const deref = (node: JsonSchema, root: JsonSchema): JsonSchema => {
  let current = node;
  for (let hops = 0; hops < 10; hops += 1) {
    const ref = current["$ref"];
    if (typeof ref !== "string") break;
    const key = ref.replace(/^#\/\$defs\//, "");
    const defs = root["$defs"];
    if (!isObject(defs) || !isObject(defs[key])) {
      throw new Error(`unresolvable $ref: ${ref}`);
    }
    // Preserve sibling keywords (e.g. `default`) that sit alongside the $ref.
    const { $ref: _dropped, ...siblings } = current;
    current = { ...defs[key], ...siblings };
  }
  // Zod wraps refined objects in allOf; Pydantic never does. Flatten a single-branch allOf.
  const allOf = current["allOf"];
  if (Array.isArray(allOf) && allOf.length === 1 && isObject(allOf[0])) {
    const { allOf: _dropped, ...siblings } = current;
    return deref({ ...allOf[0], ...siblings }, root);
  }
  return current;
};

/** The set of JSON types a node accepts, flattening anyOf/oneOf unions. */
const typesOf = (node: JsonSchema, root: JsonSchema): string[] => {
  const resolved = deref(node, root);
  const branches = resolved["anyOf"] ?? resolved["oneOf"];
  if (Array.isArray(branches)) {
    const collected = branches.flatMap((branch) =>
      isObject(branch) ? typesOf(branch, root) : [],
    );
    return [...new Set(collected)].sort();
  }
  const type = resolved["type"];
  if (typeof type === "string") return [type];
  if (Array.isArray(type)) return [...new Set(type.map(String))].sort();
  if (resolved["enum"] !== undefined) return ["string"];
  return ["unknown"];
};

const enumOf = (node: JsonSchema, root: JsonSchema): string | undefined => {
  const resolved = deref(node, root);
  const values = resolved["enum"];
  if (!Array.isArray(values)) return undefined;
  return JSON.stringify([...values].map(String).sort());
};

/** path -> digest, for every leaf and every object node, recursively. */
const digest = (root: JsonSchema): Map<string, FieldDigest> => {
  const out = new Map<string, FieldDigest>();

  const walk = (node: JsonSchema, prefix: string): void => {
    const resolved = deref(node, root);
    const properties = resolved["properties"];
    if (!isObject(properties)) return;
    const required = new Set(
      Array.isArray(resolved["required"]) ? resolved["required"].map(String) : [],
    );
    for (const [name, raw] of Object.entries(properties)) {
      if (!isObject(raw)) continue;
      const path = prefix === "" ? name : `${prefix}.${name}`;
      const resolvedChild = deref(raw, root);
      const types = typesOf(raw, root);
      // Container defaults are compared by materialised value (check 3), not by keyword.
      const isContainer = types.includes("object") || types.includes("array");
      out.set(path, {
        types,
        required: required.has(name),
        default:
          isContainer || resolvedChild["default"] === undefined
            ? undefined
            : JSON.stringify(resolvedChild["default"]),
        enum: enumOf(raw, root),
      });
      walk(raw, path);
      // Recurse through array item schemas too, e.g. custom_filters[].op.
      const items = resolvedChild["items"];
      if (isObject(items)) walk(items, `${path}[]`);
    }
  };

  walk(root, "");
  return out;
};

describe("ScreenDefinition parity: Zod vs Pydantic", () => {
  const pythonSchema = readJson(
    resolve(here, "../src/screen-definition.schema.json"),
  ) as JsonSchema;
  const zodSchema = z.toJSONSchema(ScreenDefinitionSchema, { io: "input" }) as JsonSchema;

  const fromPython = digest(pythonSchema);
  const fromZod = digest(zodSchema);

  it("declares the same field paths", () => {
    const python = [...fromPython.keys()].sort();
    const zod = [...fromZod.keys()].sort();
    expect(zod).toEqual(python);
  });

  it.each([...fromPython.keys()].sort())("agrees on the shape of %s", (path) => {
    expect({ path, ...fromZod.get(path) }).toEqual({ path, ...fromPython.get(path) });
  });

  interface Case {
    name: string;
    reason: string;
    valid: boolean;
    value: unknown;
    materialised: unknown;
  }

  const corpus = readJson(
    resolve(repoRoot, "tests/fixtures/screen-definition-corpus.json"),
  ) as Case[];

  it("has a corpus to check", () => {
    expect(corpus.length).toBeGreaterThan(20);
  });

  it.each(corpus)("$name: $reason", ({ valid, value }: Case) => {
    expect(ScreenDefinitionSchema.safeParse(value).success).toBe(valid);
  });

  it.each(corpus.filter((c) => c.valid))(
    "materialises $name exactly as Pydantic does",
    ({ value, materialised }: Case) => {
      const parsed = ScreenDefinitionSchema.parse(value);
      expect(parsed).toEqual(materialised);
    },
  );
});
