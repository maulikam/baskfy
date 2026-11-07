import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `docs/vbt/05` §2's census, restated for the desk port (6 Nov 2026, Option A).
 *
 * One server action (`scanNow`). Staff additionally get the desk console's sections; the only
 * order path is `lib/vbt/desk.ts`'s `confirmVbtLine` — the desk's own same-origin
 * `/desk/vbt/execute`, one line, with the desk-issued plan_id and confirm=true, on a second
 * click. The web app gains no order route, and VBT has no auto-confirm.
 */

const APP = join(__dirname, "..", "..");
const TREE = join(APP, "vbt");
const LIB = join(__dirname, "..", "..", "..", "..", "lib", "vbt");
const COMPONENTS = join(__dirname, "..", "..", "..", "..", "components", "vbt");
/** The one file that may name the desk's confirm route (same-origin `/desk`, via `lib/desk-actions/client`). */
const DESK_FILE = join(LIB, "desk.ts");
const PANEL_FILE = join(COMPONENTS, "desk-panel.tsx");

/** The exact set. Adding to it is a deliberate act with a diff on it — and a `05` §2 edit. */
const ALLOWED_ACTIONS = ["scanNow"];

/** Desk routes allowed only in `DESK_FILE`: the confirm stays on the desk, one line per POST. */
const DESK_ROUTE_WORDS = ['"/desk/', "`/desk/", "/vbt/execute", "/execute", "confirm=true"];

/** The routes and the verbs that reach an order, in either codebase's spelling. */
const FORBIDDEN_WORDS = [
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

const SOURCES = [...filesUnder(TREE), ...filesUnder(LIB), ...filesUnder(COMPONENTS)].filter(
  (path) =>
    (path.endsWith(".ts") || path.endsWith(".tsx")) &&
    !path.includes("__tests__"),
);

const SERVER_FILES = SOURCES.filter((path) =>
  /^\s*["']use server["'];?/m.test(readFileSync(path, "utf8")),
);

function exportsOf(source: string): string[] {
  return [...source.matchAll(/^export\s+(?:async\s+)?function\s+(\w+)/gm)].map(
    (match) => match[1] ?? "",
  );
}

describe("the volume-breakout hub: one scan action, and staff confirm desk lines on the desk", () => {
  it("finds the pages", () => {
    expect(SOURCES.length).toBeGreaterThanOrEqual(6);
  });

  it("has exactly one use-server module under the tree, and it is the hub's actions file", () => {
    expect(SERVER_FILES.map((path) => path.slice(APP.length)).sort()).toEqual([
      "/vbt/actions.ts",
    ]);
  });

  it("exports scanNow, and nothing else, from files marked use server", () => {
    const found = SERVER_FILES.flatMap((path) =>
      exportsOf(readFileSync(path, "utf8")),
    ).sort();
    expect(found).toEqual(ALLOWED_ACTIONS);
  });

  it("exports nothing from a use-server file that is not a function", () => {
    // A `use server` module may only export async functions; a re-exported constant or type from
    // one would be a bundling error in Next and a hole in the census above.
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      const named = [
        ...source.matchAll(/^export\s+(const|let|var|type|interface|class|\{)/gm),
      ];
      expect(named, `${path} exports something that is not an action`).toHaveLength(0);
      expect(source, `${path} has a default export`).not.toMatch(/^export\s+default/m);
    }
  });

  it("makes the action a server-only, bearer-carrying call through the one write helper", () => {
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      expect(source).toContain('from "@/lib/vbt/write"');
      expect(source, `${path} calls fetch itself`).not.toMatch(/\bfetch\s*\(/);
      expect(source, `${path} forgets to revalidate`).toContain("revalidatePath(");
      // The bearer lives in `write.ts` (`auth()`), which is `server-only`; an action that read a
      // token itself would be a second place for it to leak from.
      expect(source, `${path} reads a token`).not.toMatch(/accessToken|Authorization/);
    }
  });

  /**
   * The narrowest guarantee in this file, and the one that does the work.
   *
   * The path is a **closed union of one literal**, not a template pattern. A pattern that admitted
   * `/vbt/scan` would admit the desk's confirm route as well, and that route is precisely what
   * Track C §4 keeps out of this application. POST is the only method, because there is nothing
   * here to PATCH and nothing to DELETE.
   */
  it("gives the write helper one path, one method and no way to name another", () => {
    const source = readFileSync(join(LIB, "write.ts"), "utf8");
    expect(source).toContain('import "server-only"');
    expect(source).toContain('export type VbtWritePath = "/vbt/scan";');
    expect(source, "the write path became a pattern").not.toMatch(
      /VbtWritePath\s*=[^;]*\$\{/,
    );
    const methods = [...source.matchAll(/method:\s*"([A-Z]+)"/g)].map((m) => m[1]);
    expect(methods).toEqual(["POST"]);
  });

  it("names no execution package, broker or order-shaped verb", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN_WORDS) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
      if (path === DESK_FILE) continue;
      for (const word of DESK_ROUTE_WORDS) {
        expect(source, `${path} names a desk route outside lib/vbt/desk.ts`).not.toContain(word);
      }
    }
  });

  it("sends a confirm only to the desk, same-origin, one line with its plan_id and confirm=true", () => {
    const source = readFileSync(DESK_FILE, "utf8");
    expect(source).not.toContain('"use server"');
    expect(source).toContain('from "@/lib/desk-actions/client"');
    expect(source, "desk.ts calls fetch itself").not.toMatch(/\bfetch\s*\(/);
    expect(source).toMatch(
      /confirmVbtLine\(planId: string, lineId: number\)[\s\S]*?"\/vbt\/execute"[\s\S]*?plan_id: planId[\s\S]*?confirm: "true"/,
    );
    // Exactly one confirm route, and no batch form of it.
    expect([...source.matchAll(/"\/vbt\/execute"/g)]).toHaveLength(1);
    expect(source).not.toMatch(/lineIds|line_ids/);
  });

  it("gates the desk console to staff on both sides, and confirms only on a second click", () => {
    const page = readFileSync(join(TREE, "page.tsx"), "utf8");
    expect(page).toContain("fetchMe()");
    expect(page).toMatch(/me\?\.is_staff \? <VbtDeskPanel \/> : null/);
    const panel = readFileSync(PANEL_FILE, "utf8");
    expect(panel).toContain("useIsStaff()");
    expect(panel).toContain("if (!isStaff) return null;");
    expect(panel).toContain("onClick={() => setArmed(true)}");
    // The confirm call is reached only from the armed button's `send`, never from an effect.
    expect([...panel.matchAll(/confirmVbtLine\(/g)]).toHaveLength(1);
    expect(panel).toMatch(/async function send\(\)[\s\S]*?confirmVbtLine\(line\.plan_id, line\.id\)/);
    expect(panel).toContain("if (sent) return;");
  });

  /**
   * One form on the whole hub, and it is bound to the action rather than to a URL.
   *
   * A form with an `action="…"` string or a `method=` would be posting somewhere this census
   * cannot see. The count is pinned at one because a second form appearing on this tree is
   * exactly the event that should make somebody come and read this file.
   */
  it("declares exactly one form, bound to a server action and never to a URL", () => {
    let forms = 0;
    let submits = 0;
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      /* Comments are stripped first: this file's own header explains the control by quoting
         `<form>` and `type="submit"`, and a census that counted prose would be a census of how
         carefully the code was documented. */
      const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
      forms += [...code.matchAll(/<form[\s>]/g)].length;
      submits += [...code.matchAll(/type="submit"/g)].length;
      expect(code, `${path} posts a form to a URL`).not.toMatch(/<form[^>]*action="/);
      expect(code, `${path} gives a form a method`).not.toMatch(/<form[^>]*method=/);
    }
    expect(forms, "the hub grew a second form").toBe(1);
    expect(submits, "the hub grew a second submit button").toBe(1);
  });

  it("reaches the API only through the lib helpers", () => {
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

  it("names the action a page uses only from the hub's own actions file", () => {
    for (const path of SOURCES.filter((file) => !SERVER_FILES.includes(file))) {
      const source = readFileSync(path, "utf8");
      const imports = [...source.matchAll(/import\s*\{([^}]+)\}\s*from\s*"([^"]+)"/g)];
      for (const [, names, from] of imports) {
        const used = (names ?? "")
          .split(",")
          .map((name) => name.trim().split(" as ")[0]?.trim() ?? "")
          .filter((name) => ALLOWED_ACTIONS.includes(name));
        if (used.length > 0) {
          expect(from, `${path} imports ${used.join(", ")} from ${from}`).toMatch(
            /\/actions$/,
          );
        }
      }
    }
  });

  it("says on the page that an order is sent only on Place Buy Order", () => {
    const page = readFileSync(join(TREE, "page.tsx"), "utf8");
    expect(page).toContain("Place Buy Order");
    expect(page).toContain("desk console");
  });
});
