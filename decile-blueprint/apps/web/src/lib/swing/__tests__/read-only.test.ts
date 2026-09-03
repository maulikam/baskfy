import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * SW4's acceptance from the web app's side: **the swing hub can never place an order.**
 *
 * `docs/swing/02-scope-and-gating.md` Track C §4: "`apps/web` gets no route under `/swing` that
 * can reach the gateway. The existing `test_baskets_readonly.py` / `test_desk_readonly.py`
 * pattern is extended to the new routers." This is that extension on this side of the wire; the
 * API asserts the same thing about its own surface in `services/api/tests/test_swing_readonly.py`.
 *
 * A separate file rather than a widened one, for the reason `lib/desk/__tests__` is separate:
 * the two directories can be deleted independently, and a guarantee that quietly stops covering
 * a directory is worse than one that never covered it.
 *
 * The rule is narrower than the desk's, and deliberately: Track A allows this surface writes that
 * "change no money" — the watchlist, a note, the catalyst field (SW5) and the settings form. So
 * the assertion is not "no mutation exists" but **"no mutation reaches an order path"**, plus a
 * whitelist of the endpoints the fetch helper is allowed to name.
 *
 * SW14 gave the hub its forms. The writes live in `lib/swing/write.ts` and in the `actions.ts`
 * files under the pages — never in `fetch.ts`, which stays GET-only, and never inline in a
 * `page.tsx`. `app/(app)/swing/__tests__/read-only.test.tsx` enumerates the actions themselves.
 */

const APP = join(__dirname, "..", "..", "..", "app", "(app)");
const SWING_PAGES = join(APP, "swing");

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const PAGES = filesUnder(SWING_PAGES).filter(
  (path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

const SOURCES = [
  ...filesUnder(join(__dirname, "..")).filter((path) => !path.includes("__tests__")),
  ...PAGES,
].filter((path) => path.endsWith(".ts") || path.endsWith(".tsx"));

/**
 * Every API path the swing hub is allowed to name.
 *
 * A whitelist rather than a pattern, because `/swing/execute` would match any pattern that
 * allowed `/swing/setups`, and `/swing/execute` is exactly the route Track C §4 exists to keep
 * out of this application. Adding an entry here is a deliberate act with a diff on it.
 */
const ALLOWED_PATHS = [
  "/swing/setups",
  "/swing/setups/{id}/bars",
  "/swing/sectors",
  "/swing/market",
  "/swing/config",
  "/swing/watch",
  "/swing/positions",
  "/swing/journal",
  "/swing/signals",
];

/** The paths the write helper may be asked for — `02` Track A's non-money writes, by name. */
const ALLOWED_WRITE_PATHS = [
  "/swing/watch",
  "/swing/watch/${number}",
  "/swing/config",
  // SW15: "Scan now" queues a detection run — quotes in, detection rows out, never an order.
  "/swing/scan",
];

describe("the swing hub is read-only and cannot reach an order", () => {
  it("covers the pages that exist", () => {
    const pages = PAGES.filter((path) => path.endsWith("page.tsx"));
    expect(pages.length).toBeGreaterThanOrEqual(2);
  });

  it("names no order, execution or broker endpoint", () => {
    /*
      The banned words are the ones that *do* something. `gtt` on its own is deliberately not
      among them: a position carries a `gtt_id`, and whether one exists is the single most
      important safety fact this hub shows — the unprotected-position warning is written from it.
      Reading that field is the opposite of arming a trigger, so the list names the verbs
      (`place_gtt`, `delete_gtt`, `/gtt`) rather than the noun.
    */
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of [
        "/swing/execute",
        "/execute",
        "/gtt",
        "place_order",
        "placeorder",
        "place_gtt",
        "delete_gtt",
        "kiteconnect",
        "ordergateway",
        "confirm=true",
      ]) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("only ever reads a gtt id, never writes one", () => {
    // The narrowing above is only safe while `gtt_id` is read. An assignment to it here would
    // mean the web app had started managing triggers, which is Track C §4's whole subject.
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} assigns a gtt id`).not.toMatch(/gtt_id\s*[:=]\s*[^;\n]*\(/);
      expect(source, `${path} builds a gtt request`).not.toMatch(/body:.*gtt/i);
    }
  });

  it("issues no non-GET request from the fetch helper", () => {
    const fetcher = readFileSync(join(__dirname, "..", "fetch.ts"), "utf8");
    for (const verb of [
      'method: "POST"',
      'method: "PUT"',
      'method: "PATCH"',
      'method: "DELETE"',
    ]) {
      expect(fetcher, `lib/swing/fetch.ts uses ${verb}`).not.toContain(verb);
    }
  });

  it("reads only from the endpoints on the whitelist", () => {
    const fetcher = readFileSync(join(__dirname, "..", "fetch.ts"), "utf8");
    const paths = [...fetcher.matchAll(/readOrNull<[^>]*>\(\s*["`]([^"`]+)["`]/g)].map(
      (match) => (match[1] ?? "").replace(/\$\{[^}]*\}/g, "{id}"),
    );
    expect(paths.length).toBeGreaterThanOrEqual(ALLOWED_PATHS.length);
    for (const path of paths) {
      expect(ALLOWED_PATHS, `${path} is not on the swing whitelist`).toContain(path);
    }
  });

  it("writes only to the paths Track A permits, and the helper's type says so", () => {
    /*
      `write.ts` is the only file under `lib/swing` allowed a non-GET verb. Its path type is a
      closed union — a caller asking for `/swing/execute` fails to compile — and this reads the
      union back out of the source so the whitelist has a diff on it.
    */
    const writer = readFileSync(join(__dirname, "..", "write.ts"), "utf8");
    const union = writer.match(/export type SwingWritePath =([^;]+);/)?.[1] ?? "";
    const paths = [...union.matchAll(/["`]([^"`]+)["`]/g)].map((match) => match[1]);
    expect(paths.sort()).toEqual([...ALLOWED_WRITE_PATHS].sort());
    expect(writer).not.toMatch(/method:\s*"PUT"/);
  });

  it("declares no server action inline in a page", () => {
    // The actions live in `actions.ts` files, where the read-only assertion over `(app)/swing`
    // enumerates them by name. A page that declared its own would sit outside that census.
    for (const path of PAGES.filter((file) => file.endsWith("page.tsx"))) {
      expect(readFileSync(path, "utf8"), `${path} declares a server action`).not.toContain(
        '"use server"',
      );
    }
  });

  it("binds every form to a server action and never to a URL", () => {
    // A `<form action="/...">` would post straight to a route; the hub's forms post to a
    // server action, which is the only way the bearer stays on the server.
    for (const path of PAGES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} posts a form to a URL`).not.toMatch(/<form[^>]*action="/);
      expect(source, `${path} posts a form with a method`).not.toMatch(/<form[^>]*method=/);
    }
  });

  it("says on the page that nothing here can trade", () => {
    // Not decoration. A person looking at a table of triggers and stops needs to know it is a
    // list and not a control, and the sentence is worth as much to them as the test is to us.
    const setups = readFileSync(join(SWING_PAGES, "page.tsx"), "utf8");
    expect(setups).toContain("can place an order");
    expect(setups).toContain("desk console");
  });
});
