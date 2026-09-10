import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `docs/vbt/05` §2's read-only assertion, VB8 — and it is a stronger claim than the swing hub's.
 *
 * The swing tree has six server actions that "change no money". **This tree has none at all.**
 * `05` §2 sketched a per-row *Dismiss* note; it was not built, because `03`'s data model has no
 * table to put a note in and inventing one to hold a mock-up's affordance would have been a
 * migration in service of a drawing (DECISIONS-VB VB8.4). So the assertion here is the simplest
 * one available: nothing under `/vbt` is a `use server` module, and nothing under it names a
 * route, a package or a verb that could reach an order.
 *
 * Structural, like `services/api/tests/test_vbt_readonly.py` on the other side of the wire: it
 * walks the files, so an action added in a new file fails the moment it exists rather than the
 * moment somebody notices.
 */

const APP = join(__dirname, "..", "..");
const TREE = join(APP, "vbt");
const LIB = join(__dirname, "..", "..", "..", "..", "lib", "vbt");

/** The routes and the verbs that reach an order, in either codebase's spelling. */
const FORBIDDEN_WORDS = [
  "/desk/",
  "/vbt/execute",
  "/execute",
  "/reconcile",
  "place_order",
  "placeorder",
  "place_gtt",
  "delete_gtt",
  "modify_gtt",
  "arm_gtt",
  "kiteconnect",
  "kite_client",
  "ordergateway",
  "baskfy_execution",
  "confirm=true",
  "confirm all",
  "confirm-all",
  "auto_execute",
  "auto-execute",
];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const SOURCES = [...filesUnder(TREE), ...filesUnder(LIB)].filter(
  (path) =>
    (path.endsWith(".ts") || path.endsWith(".tsx")) &&
    !path.includes("__tests__"),
);

describe("the volume-breakout hub cannot write anything at all", () => {
  it("finds the pages", () => {
    expect(SOURCES.length).toBeGreaterThanOrEqual(6);
  });

  it("has no use-server module anywhere under the tree", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} is a server-action module`).not.toMatch(
        /^\s*["']use server["'];?/m,
      );
    }
  });

  it("names no execution package, broker or order-shaped verb", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN_WORDS) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("declares no form at all — there is nothing here to submit", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} renders a form`).not.toMatch(/<form[\s>]/);
      expect(source, `${path} renders a submit button`).not.toMatch(
        /type="submit"/,
      );
    }
  });

  it("reaches the API only through the one server-only read helper", () => {
    for (const path of SOURCES) {
      if (path.startsWith(LIB)) continue;
      const source = readFileSync(path, "utf8");
      expect(source, `${path} calls fetch itself`).not.toMatch(/\bfetch\s*\(/);
      expect(source, `${path} reads a token`).not.toMatch(
        /accessToken|Authorization/,
      );
    }
  });

  it("keeps the read helper server-only and free of any write verb", () => {
    const source = readFileSync(join(LIB, "fetch.ts"), "utf8");
    expect(source).toContain('import "server-only"');
    expect(source).not.toMatch(/method:\s*["'](POST|PATCH|PUT|DELETE)["']/);
  });
});
