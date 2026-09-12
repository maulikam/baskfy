import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `docs/twt/05` §4's first bullet, asserted structurally — TW8, restated for the one control that
 * now exists.
 *
 * **The web app has no order path under `/twt` and does not gain one.** That is unchanged, and it
 * is the product's first non-negotiable ("never auto-execute"), law 2 ("`packages/execution` is
 * the only path to an order"), and `docs/twt/02` Track C §4, which says this hub "gets no route
 * under `/twt` that can reach the gateway". `05` §2 still puts the click in the desk console and
 * nowhere else.
 *
 * WHAT THIS FILE USED TO SAY, AND WHY IT NO LONGER CAN
 * ----------------------------------------------------
 * Until 12 Sep 2026 this tree had no server action at all. `05` §1 permits exactly two writes
 * from this hub — a note and a dismissal — because they change no money; neither was built, since
 * `03`'s data model has no table to put a note in, so "no server action at all" was the strongest
 * true claim available and this test made it (TW8.7).
 *
 * Maulik asked for **"Scan now"** on this page (`PLAN-SCAN-SYNC.md`, leaf 5), so that claim is no
 * longer available. It is replaced by the swing hub's census, which is a stronger guarantee than
 * a prose promise ever was: the file walk enumerates **every export of every `use server` module
 * under the tree**, and the set must be exactly `["scanNow"]`. An action added in a new file
 * fails here the moment it exists rather than the moment somebody notices.
 *
 * **Why a scan is allowed where an order is not.** A scan queues the detector — prices in,
 * detection rows out — and cannot reach the gateway: the write helper's path type is a closed
 * union of one string, not a pattern, and a pattern that admitted `/twt/scan` would admit the
 * desk's confirm route too. It also cannot set this sleeve's capital or flip its execution
 * switch, which are Maulik's alone (`02` §3) and live nowhere this application can reach.
 * DECISIONS-TW TW11.
 */

const APP_TREE = join(__dirname, "..");
const LIB_TREE = join(__dirname, "..", "..", "..", "..", "lib", "twt");
const COMPONENT_TREE = join(__dirname, "..", "..", "..", "..", "components", "twt");

/**
 * `03` §7's plan-line kinds, which are DATA and not verbs.
 *
 * The forbidden list below is lower-cased before it is searched, and two of `03`'s stored kinds
 * lower-case into words on it. They are the names of rows in a plan this page only ever *reads* —
 * the desk is what acts on one — so they are removed before the search rather than the patterns
 * being widened, which is the convention `tools/check-namespace.sh` sets for exactly this
 * problem: a narrow, named exception with a reason, never a loosened rule.
 */
const DATA_LITERALS = [/\bARM_GTT\b/g, /\bRAISE_GTT_STOP\b/g];

/** The exact set of server actions. Adding to it is a deliberate act with a diff on it. */
const ALLOWED_ACTIONS = ["scanNow"];

/** The routes and the verbs that reach an order, in either codebase's spelling. */
const FORBIDDEN_WORDS = [
  "/desk/",
  "/twt/execute",
  "/execute",
  "/reconcile",
  "place_order",
  "placeorder",
  "place_gtt",
  "delete_gtt",
  "modify_gtt",
  "arm_gtt",
  "rearm_gtt",
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

/**
 * The two things this hub may never set, in either spelling.
 *
 * `docs/twt/02` §3: the sleeve's capital and its execution switch are Maulik's, and the repo's
 * own plan says no agent sets them "in any circumstance". A web action that could name either
 * would be one refactor away from setting one.
 */
const FORBIDDEN_SETTINGS = ["sleeve_capital", "execution_enabled", "twt_execution"];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const SOURCES = [
  ...filesUnder(APP_TREE),
  ...filesUnder(LIB_TREE),
  ...filesUnder(COMPONENT_TREE),
].filter(
  (path) =>
    (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

const SERVER_FILES = SOURCES.filter((path) =>
  /^\s*["']use server["'];?/m.test(readFileSync(path, "utf8")),
);

function exportsOf(source: string): string[] {
  return [...source.matchAll(/^export\s+(?:async\s+)?function\s+(\w+)/gm)].map(
    (match) => match[1] ?? "",
  );
}

describe("the three-weeks-tight hub can queue a scan and can do nothing else", () => {
  it("read-only: the scan actually finds the pages, the reads and the components", () => {
    // A scan over nothing passes silently and proves nothing.
    expect(SOURCES.length).toBeGreaterThanOrEqual(10);
  });

  it("read-only: exactly one use-server module under the tree, the hub's actions file", () => {
    expect(SERVER_FILES.map((path) => path.slice(APP_TREE.length)).sort()).toEqual([
      "/actions.ts",
    ]);
  });

  it("read-only: it exports scanNow and nothing else", () => {
    const found = SERVER_FILES.flatMap((path) =>
      exportsOf(readFileSync(path, "utf8")),
    ).sort();
    expect(found).toEqual(ALLOWED_ACTIONS);
  });

  it("read-only: a use-server file exports nothing that is not a function", () => {
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      const named = [
        ...source.matchAll(/^export\s+(const|let|var|type|interface|class|\{)/gm),
      ];
      expect(named, `${path} exports something that is not an action`).toHaveLength(0);
      expect(source, `${path} has a default export`).not.toMatch(/^export\s+default/m);
    }
  });

  it("read-only: the action is a server-only, bearer-carrying call through the write helper", () => {
    for (const path of SERVER_FILES) {
      const source = readFileSync(path, "utf8");
      expect(source).toContain('from "@/lib/twt/write"');
      expect(source, `${path} calls fetch itself`).not.toMatch(/\bfetch\s*\(/);
      expect(source, `${path} forgets to revalidate`).toContain("revalidatePath(");
      expect(source, `${path} reads a token`).not.toMatch(/accessToken|Authorization/);
    }
  });

  /**
   * The narrowest guarantee in this file, and the one that does the work.
   *
   * The path is a **closed union of one literal**, not a template pattern. A pattern that admitted
   * `/twt/scan` would admit the desk's confirm route as well, and that route is precisely what
   * Track C §4 keeps out of this application. POST is the only method, because there is nothing
   * here to PATCH and nothing to DELETE.
   */
  it("read-only: the write helper has one path, one method and no way to name another", () => {
    const source = readFileSync(join(LIB_TREE, "write.ts"), "utf8");
    expect(source).toContain('import "server-only"');
    expect(source).toContain('export type TwtWritePath = "/twt/scan";');
    expect(source, "the write path became a pattern").not.toMatch(
      /TwtWritePath\s*=[^;]*\$\{/,
    );
    const methods = [...source.matchAll(/method:\s*"([A-Z]+)"/g)].map((m) => m[1]);
    expect(methods).toEqual(["POST"]);
  });

  it("read-only: nothing names an execution package, a broker or an order-shaped verb", () => {
    for (const path of SOURCES) {
      let source = readFileSync(path, "utf8");
      for (const literal of DATA_LITERALS) source = source.replace(literal, "PLAN_LINE_KIND");
      const lowered = source.toLowerCase();
      for (const word of FORBIDDEN_WORDS) {
        expect(lowered, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  /**
   * The *write* path may not name this sleeve's capital or its execution switch.
   *
   * The scan is checked rather than the whole tree on purpose: `03`'s half-size counter is read
   * and rendered by the hub, so the read helper and the copy module legitimately name the stored
   * switch in order to *say* that trading is off. Reading a setting is not setting one. What must
   * never be nameable is the path that can send something, which is this action and this helper.
   */
  it("read-only: the write path cannot name this sleeve's capital or its switch", () => {
    const writePath = [...SERVER_FILES, join(LIB_TREE, "write.ts")];
    for (const path of writePath) {
      const lowered = readFileSync(path, "utf8").toLowerCase();
      for (const word of FORBIDDEN_SETTINGS) {
        expect(lowered, `${path} names ${word}`).not.toContain(word);
      }
    }
  });

  /**
   * One form on the whole hub, and it is bound to the action rather than to a URL.
   *
   * A form with an `action="…"` string or a `method=` would be posting somewhere this census
   * cannot see. The count is pinned at one because a second form appearing on this tree is
   * exactly the event that should make somebody come and read this file.
   */
  it("read-only: exactly one form, bound to a server action and never to a URL", () => {
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

  it("read-only: the API is reached only through the two server-only helpers", () => {
    for (const path of SOURCES) {
      if (path.startsWith(LIB_TREE)) continue;
      const source = readFileSync(path, "utf8");
      expect(source, `${path} calls fetch itself`).not.toMatch(/\bfetch\s*\(/);
      expect(source, `${path} reads a token`).not.toMatch(/accessToken|Authorization/);
    }
  });

  it("read-only: the read helper is server-only and carries no write method", () => {
    const source = readFileSync(join(LIB_TREE, "fetch.ts"), "utf8");
    expect(source).toContain('import "server-only"');
    expect(source).not.toMatch(/method:\s*["'](POST|PATCH|PUT|DELETE)["']/);
  });

  it("read-only: a page names the action only from the hub's own actions file", () => {
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

  /**
   * Every method other than GET and the hub's own action POST is **405**, and it is so because
   * there is nothing there to answer anything else.
   *
   * A Next route segment with no `route.ts` serves its page on GET and refuses every other method
   * with 405 — the framework's own behaviour, not something this app configures. A server action
   * is not a route: it is a function reference the framework dispatches, not an address anybody
   * can post to by guessing. So the honest assertion is the one that keeps it true: no route
   * handler exists under this tree, and the one action file is the one this census enumerates.
   * The day somebody adds a `route.ts` here, this fails, and they have to come and read why.
   */
  it("405: no route handler exists under the tree, so every method but GET is refused", () => {
    const handlers = filesUnder(APP_TREE).filter(
      (path) => path.endsWith("route.ts") || path.endsWith("route.tsx"),
    );
    expect(handlers, "a route handler appeared under /twt").toEqual([]);

    const actions = filesUnder(APP_TREE)
      .filter((path) => path.endsWith("actions.ts"))
      .map((path) => path.slice(APP_TREE.length));
    expect(actions, "a second server-action module appeared under /twt").toEqual([
      "/actions.ts",
    ]);
  });

  /**
   * The desk view is a *shape*, not a wire. It renders whatever confirm control its host hands
   * it, and the web app hands it none — which is why no page under `(app)` imports it.
   */
  it("read-only: no page renders the desk plan, because no page here may confirm one", () => {
    for (const path of filesUnder(APP_TREE)) {
      if (!path.endsWith("page.tsx")) continue;
      const source = readFileSync(path, "utf8");
      expect(source, `${path} renders the desk plan`).not.toMatch(/DeskPlan/);
    }
  });

  it("read-only: the page still says that nothing on it can place an order", () => {
    const page = readFileSync(join(APP_TREE, "page.tsx"), "utf8");
    expect(page).toContain("can place an order");
    expect(page).toContain("desk console");
  });
});
