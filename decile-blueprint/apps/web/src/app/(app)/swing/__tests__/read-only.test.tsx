import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `docs/swing/05` §2's read-only assertion, SW14: the only server actions under `/swing` (and
 * `/me/swing`, where the settings form lives) are `watchAdd`, `watchDismiss`, `watchAnnotate`,
 * `watchReconfirm` and `settingsSave`; none imports anything from the execution package; nothing
 * under these trees names the desk console's routes or a verb that places.
 *
 * `watchReconfirm` is STANDING-ANSWERS A14's addition to the four `05` §2 first named — a MANUAL
 * row's "Still watching" control (DECISIONS-SW SW14.1). Every one of the five is a Track A write
 * that "changes no money".
 *
 * Structural, like `services/api/tests/test_swing_readonly.py` on the other side of the wire:
 * it walks the files, so an action added in a new file fails here the moment it exists, not the
 * moment somebody notices. `lib/swing/__tests__/read-only.test.ts` keeps the fetch helper's
 * whitelist and the word bans over `lib/swing`; this file is the census of what can write.
 */

const APP = join(__dirname, "..", "..");
const TREES = [join(APP, "swing"), join(APP, "me", "swing")];

/** The exact set. Adding to it is a deliberate act with a diff on it — and a `05` §2 edit. */
const ALLOWED_ACTIONS = [
  "watchAdd",
  "watchDismiss",
  "watchAnnotate",
  "watchReconfirm",
  "settingsSave",
].sort();

/** The routes and the verbs that reach an order, in either codebase's spelling. */
const FORBIDDEN_WORDS = [
  "/desk/",
  "/swing/execute",
  "/execute",
  "/reconcile",
  "/cutoff",
  "/gtt",
  "place_order",
  "placeorder",
  "place_gtt",
  "delete_gtt",
  "modify_gtt",
  "rearm",
  "re-arm",
  "kiteconnect",
  "kite_client",
  "ordergateway",
  "baskfy_execution",
  "confirm=true",
  "confirm all",
  "confirm-all",
  "confirmall",
];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const SOURCES = TREES.flatMap(filesUnder).filter(
  (path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

const SERVER_FILES = SOURCES.filter((path) =>
  /^\s*["']use server["'];?/m.test(readFileSync(path, "utf8")),
);

function exportsOf(source: string): string[] {
  return [...source.matchAll(/^export\s+(?:async\s+)?function\s+(\w+)/gm)].map(
    (match) => match[1] ?? "",
  );
}

describe("the swing hub's server actions are exactly the five non-money writes", () => {
  it("finds the action files", () => {
    expect(SERVER_FILES.map((path) => path.slice(APP.length)).sort()).toEqual([
      "/me/swing/actions.ts",
      "/swing/actions.ts",
    ]);
  });

  it("exports the five, and nothing else, from files marked use server", () => {
    const found = SERVER_FILES.flatMap((path) => exportsOf(readFileSync(path, "utf8"))).sort();
    expect(found).toEqual(ALLOWED_ACTIONS);
  });

  it("exports nothing from a use-server file that is not a function", () => {
    // A `use server` module may only export async functions; a re-exported constant or type
    // from one would be a bundling error in Next and a hole in the census above.
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      const named = [...source.matchAll(/^export\s+(const|let|var|type|interface|class|\{)/gm)];
      expect(named, `${path} exports something that is not an action`).toHaveLength(0);
      expect(source, `${path} has a default export`).not.toMatch(/^export\s+default/m);
    }
  });

  it("makes every action a server-only, bearer-carrying call through the one write helper", () => {
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      expect(source).toContain('from "@/lib/swing/write"');
      expect(source, `${path} calls fetch itself`).not.toMatch(/\bfetch\s*\(/);
      expect(source, `${path} forgets to revalidate`).toContain("revalidatePath(");
      // The bearer lives in `write.ts` (`auth()`), which is `server-only`; an action that read a
      // token itself would be a second place for it to leak from.
      expect(source, `${path} reads a token`).not.toMatch(/accessToken|Authorization/);
    }
  });

  it("imports nothing from the execution package or a broker, anywhere under the trees", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN_WORDS) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("binds every form to a server action, never to a URL or a method", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} posts a form to a URL`).not.toMatch(/<form[^>]*action="/);
      expect(source, `${path} gives a form a method`).not.toMatch(/<form[^>]*method=/);
    }
  });

  it("names the actions a page uses only from the two action files", () => {
    // A page that imported an action from anywhere else would be importing something this
    // census never saw.
    for (const path of SOURCES.filter((file) => !SERVER_FILES.includes(file))) {
      const source = readFileSync(path, "utf8");
      const imports = [...source.matchAll(/import\s*\{([^}]+)\}\s*from\s*"([^"]+)"/g)];
      for (const [, names, from] of imports) {
        const used = (names ?? "")
          .split(",")
          .map((name) => name.trim().split(" as ")[0]?.trim() ?? "")
          .filter((name) => ALLOWED_ACTIONS.includes(name));
        if (used.length > 0) {
          expect(from, `${path} imports ${used.join(", ")} from ${from}`).toMatch(/\/actions$/);
        }
      }
    }
  });

  it("says on the setups page that nothing here can trade", () => {
    const setups = readFileSync(join(APP, "swing", "page.tsx"), "utf8");
    expect(setups).toContain("can place an order");
    expect(setups).toContain("desk console");
  });
});
