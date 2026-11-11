import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * PM7, asserted structurally: the MB pages have no server action, no write and no order path
 * (Track C, `docs/portfolio-ml/README.md`). A use-server file or a write verb added anywhere under
 * the tree fails here the moment it exists.
 */
const APP_TREE = join(__dirname, "..");
const LIB_TREE = join(__dirname, "..", "..", "..", "..", "lib", "mb");
const COMPONENT_TREE = join(__dirname, "..", "..", "..", "..", "components", "mb");

const FORBIDDEN = [
  "use server",
  "method: \"post\"",
  "method: \"put\"",
  "method: \"patch\"",
  "method: \"delete\"",
  "/execute",
  "/analyze",
  "place_order",
  "placeorder",
  "gtt",
  "kite_client",
  "baskfy_execution",
  "auto_execute",
  "auto-execute",
];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const SOURCES = [...filesUnder(APP_TREE), ...filesUnder(LIB_TREE), ...filesUnder(COMPONENT_TREE)].filter(
  (path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

describe("the MB pages are read-only", () => {
  it("finds the pages, the reads and the components", () => {
    expect(SOURCES.length).toBeGreaterThanOrEqual(6);
  });

  it("no source names a write, an order or an action", () => {
    for (const path of SOURCES) {
      const text = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN) expect(text.includes(word), `${path}: ${word}`).toBe(false);
    }
  });
});
