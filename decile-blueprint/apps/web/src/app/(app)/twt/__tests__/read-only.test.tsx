import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * `docs/twt/05` §4's first bullet, asserted structurally — TW8.
 *
 * **The web app has no order path under `/twt` and does not gain one.** That is the product's
 * first non-negotiable ("never auto-execute"), law 2 ("`packages/execution` is the only path to
 * an order"), and `docs/twt/02` Track C §4, which says this hub "gets no route under `/twt` that
 * can reach the gateway". `05` §2 puts the click in the desk console and nowhere else.
 *
 * Structural rather than behavioural, like the sibling sleeve's: it walks the files, so an action
 * added in a new file fails the moment it exists rather than the moment somebody notices. A test
 * that posted to a route and asserted a 405 would only ever cover the routes it knew about.
 *
 * WHAT THE TWO ALLOWED MUTATIONS WOULD BE, AND WHY THERE ARE NONE
 * --------------------------------------------------------------
 * `05` §1 permits exactly two writes from this hub — a note and a dismissal — because they change
 * no money. Neither is built: `03`'s data model has no table to put a note in, and inventing one
 * to hold an affordance nobody has asked for would be a migration in service of a mock-up. The
 * sibling sleeve reached the same conclusion for the same reason (DECISIONS-VB VB8.4), so the
 * assertion here is the stronger one that is available: this tree has no server action at all.
 * DECISIONS-TW TW8.7.
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

describe("the three-weeks-tight hub is read-only, and cannot become otherwise", () => {
  it("read-only: the scan actually finds the pages, the reads and the components", () => {
    // A scan over nothing passes silently and proves nothing.
    expect(SOURCES.length).toBeGreaterThanOrEqual(10);
  });

  it("read-only: no module under the tree is a server action", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} is a server-action module`).not.toMatch(
        /^\s*["']use server["'];?/m,
      );
    }
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

  it("read-only: nothing declares a form — there is nothing here to submit", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} renders a form`).not.toMatch(/<form[\s>]/);
      expect(source, `${path} renders a submit button`).not.toMatch(/type="submit"/);
    }
  });

  it("read-only: the API is reached only through the one server-only read helper", () => {
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

  /**
   * Every method other than GET under `/twt` answers **405**, and it does so because there is
   * nothing there to answer anything else.
   *
   * A Next route segment with no `route.ts` and no server action serves its page on GET and
   * refuses every other method with 405 — that is the framework's own behaviour, not something
   * this app configures. So the honest assertion is the one that keeps it true: no route handler
   * exists under this tree, and none of the two permitted money-free mutations has been built.
   * The day somebody adds a `route.ts` here, this fails, and they have to come and read why.
   */
  it("405: no route handler exists under the tree, so every method but GET is refused", () => {
    const handlers = filesUnder(APP_TREE).filter(
      (path) => path.endsWith("route.ts") || path.endsWith("route.tsx"),
    );
    expect(handlers, "a route handler appeared under /twt").toEqual([]);

    const actions = filesUnder(APP_TREE).filter((path) => path.endsWith("actions.ts"));
    expect(actions, "a server-action module appeared under /twt").toEqual([]);
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
});
