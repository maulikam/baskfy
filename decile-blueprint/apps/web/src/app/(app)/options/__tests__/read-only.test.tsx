import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `06` OP5: "`test_options_readonly.py` on both sides: exactly two mutations (event day,
 * settings), everything else 405; no import of `packages/execution` in `apps/web` or the router."
 * This is the web side, in the census shape the swing and TWT hubs set.
 *
 * * Every export of every `use server` module under the options tree and `/me/options` is one of
 *   exactly three actions — the event day's add and remove, and the settings save.
 * * The one write helper's path type is a closed union of exactly the two paths.
 * * No route handler exists under the tree (no `route.ts`), and no source names the execution
 *   package, a broker call, a confirm or an execute.
 *
 * The desk console's `/nifty-options` is the only surface with a Confirm (`docs/options/05` §3);
 * the web app never gains an order route (`02` Track C §4, root `CLAUDE.md` non-negotiable 1).
 */

const WEB_SRC = join(__dirname, "..", "..", "..", "..");
const TREES = [
  join(WEB_SRC, "app", "(app)", "options"),
  join(WEB_SRC, "app", "(app)", "me", "options"),
  join(WEB_SRC, "lib", "options"),
  join(WEB_SRC, "components", "options"),
];

const ALLOWED_ACTIONS = ["addEventDay", "removeEventDay", "saveOptionsConfig"];

const FORBIDDEN_WORDS = [
  "/desk/",
  "/execute",
  "nifty-options",
  "place_order",
  "placeorder",
  "place_gtt",
  "kiteconnect",
  "kite_client",
  "ordergateway",
  "baskfy_execution",
  "@baskfy/execution",
  "confirm=true",
  "auto_execute",
  "auto-execute",
  "execution_enabled=",
];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const ALL = TREES.flatMap(filesUnder);
const SOURCES = ALL.filter(
  (path) =>
    (path.endsWith(".ts") || path.endsWith(".tsx")) &&
    !path.includes("__tests__"),
);

function exportsOf(source: string): string[] {
  return [...source.matchAll(/^export\s+(?:async\s+)?function\s+(\w+)/gm)].map(
    (match) => match[1] ?? "",
  );
}

describe("the options tab can write an event day and the settings, and nothing else", () => {
  it("scans something: the pages, the reads and the components", () => {
    expect(SOURCES.length).toBeGreaterThanOrEqual(15);
  });

  it("has exactly three server actions across the tree", () => {
    const actions = SOURCES.filter((path) =>
      /^\s*["']use server["'];?/m.test(readFileSync(path, "utf8")),
    ).flatMap((path) => exportsOf(readFileSync(path, "utf8")));
    expect(actions.sort()).toEqual([...ALLOWED_ACTIONS].sort());
  });

  it("has no route handler anywhere under the tree", () => {
    expect(ALL.filter((path) => /[/\\]route\.(ts|tsx|js)$/.test(path))).toEqual(
      [],
    );
  });

  it("writes through a closed union of exactly two paths", () => {
    const write = readFileSync(
      join(WEB_SRC, "lib", "options", "write.ts"),
      "utf8",
    );
    const union =
      /export type OptionsWritePath\s*=\s*([^;]+);/.exec(write)?.[1] ?? "";
    const paths = [...union.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(paths.sort()).toEqual(["/options/config", "/options/event-day"]);
    expect(union).not.toMatch(/`/);
  });

  it("names no execution path, broker call, confirm or execute", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN_WORDS) {
        expect(source.includes(word), `${path} names ${word}`).toBe(false);
      }
    }
  });

  it("app-wide: no page or route handler under the web app is an execute or confirm path", () => {
    const app = filesUnder(join(WEB_SRC, "app")).map((path) => path.toLowerCase());
    expect(app.filter((path) => /[/\\](execute|confirm)[/\\]/.test(path))).toEqual([]);
  });

  it("the tab's own page binds no action at all", () => {
    const page = readFileSync(
      join(WEB_SRC, "app", "(app)", "options", "page.tsx"),
      "utf8",
    );
    expect(page).not.toMatch(/from "\.\/actions"/);
    expect(page).not.toMatch(/action=\{/);
  });
});
