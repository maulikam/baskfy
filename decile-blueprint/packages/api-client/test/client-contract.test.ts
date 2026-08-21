/**
 * The generated client against a hand-written fixture — Prompt 7's fourth acceptance criterion.
 *
 *     "Generated TS client compiles and its types match a hand-written assertion fixture."
 *
 * "Compiles" is `pnpm run lint` (`tsc --noEmit`), which type-checks this file too. What this file
 * adds is the *fixture*: an independently written statement of what the API returns, asserted
 * against the generated types at compile time and against the generated JSON at run time.
 *
 * The point is to fail when the API changes shape without anyone noticing. `Expect<Equal<A, B>>`
 * is a compile-time assertion — if `ScreenRunResponse` ever stops having exactly these members,
 * `tsc` reports it here rather than in a component three months later.
 *
 * These are deliberately hand-written. Deriving them from the same `openapi.json` the client is
 * generated from would assert that a file equals itself.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type {
  ColumnOut,
  FactorOut,
  ProblemOut,
  ScreenOut,
  ScreenRunResponse,
  ScreenRunRowOut,
  StatusOut,
  UniverseOut,
} from "../src/client.js";
import { PROBLEM_TYPES, createDecileClient, isProblem } from "../src/client.js";
import type { paths } from "../src/generated/schema.js";

// --- compile-time assertion helpers -----------------------------------------

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;
type KeysOf<T> = keyof T;

// --- the hand-written fixture ------------------------------------------------

/** docs/07 §Metadata: "factor registry (key, label, family, unit, higher_is_better)". */
interface ExpectedFactor {
  key: string;
  label: string;
  family: string;
  unit: string;
  higher_is_better: boolean;
}

/** docs/07 §"Running a screen", field for field. */
interface ExpectedRunResponse {
  as_of: string;
  data_version: number;
  result_count: number;
  sorting_factor: { key: string; label: string };
  columns: string[];
  rows: ScreenRunRowOut[];
}

/**
 * docs/07 §"Error catalogue" — RFC 9457 members, plus the extensions each type carries.
 *
 * `instance` is non-optional because the server sets it on every problem it emits (the request
 * path), and `openapi-typescript` types a response property with a default as always-present.
 */
interface ExpectedProblem {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string | null;
  [extension: string]: unknown;
}

type _FactorMatches = Expect<Equal<KeysOf<FactorOut>, KeysOf<ExpectedFactor>>>;
type _FactorTypesMatch = Expect<Equal<FactorOut, ExpectedFactor>>;
type _RunMatches = Expect<Equal<KeysOf<ScreenRunResponse>, KeysOf<ExpectedRunResponse>>>;
type _RunTypesMatch = Expect<Equal<ScreenRunResponse, ExpectedRunResponse>>;
// An index signature swallows a `keyof` comparison, so the RFC members are asserted one by one.
type _ProblemType = Expect<Equal<ProblemOut["type"], ExpectedProblem["type"]>>;
type _ProblemTitle = Expect<Equal<ProblemOut["title"], ExpectedProblem["title"]>>;
type _ProblemStatus = Expect<Equal<ProblemOut["status"], ExpectedProblem["status"]>>;
type _ProblemDetail = Expect<Equal<ProblemOut["detail"], ExpectedProblem["detail"]>>;
type _ProblemInstance = Expect<Equal<ProblemOut["instance"], ExpectedProblem["instance"]>>;
/** docs/07 attaches `upgrade_url`, `errors[]`, `retry_after` per type — RFC 9457 extensions. */
type _ProblemAcceptsExtensions = Expect<Equal<ProblemOut["upgrade_url"], unknown>>;

/** The run endpoint takes the body docs/07 documents, and it is optional. */
type RunBody =
  paths["/api/v1/screens/{public_id}/run"]["post"]["requestBody"];
type _RunBodyIsOptional = Expect<Equal<undefined extends RunBody ? true : false, true>>;

/** A result row is `rank` plus the identity columns plus whatever the user picked. */
type _RowHasRank = Expect<Equal<ScreenRunRowOut["rank"], number>>;
type _RowHasSymbol = Expect<Equal<ScreenRunRowOut["symbol"], string>>;

/** The status payload the freshness banner reads. */
type _StatusShape = Expect<
  Equal<
    KeysOf<StatusOut>,
    "as_of" | "data_version" | "last_pipeline_run" | "degraded" | "data_start_date"
  >
>;

/** A screen, as the list and the CRUD endpoints return it. */
type _ScreenShape = Expect<
  Equal<
    KeysOf<ScreenOut>,
    "public_id" | "name" | "definition" | "columns" | "is_example" | "editable" | "created_at" | "updated_at"
  >
>;

type _ColumnShape = Expect<Equal<KeysOf<ColumnOut>, "key" | "label" | "unit" | "is_factor">>;
type _UniverseShape = Expect<
  Equal<KeysOf<UniverseOut>, "index_id" | "slug" | "name" | "sort_order" | "market_health">
>;

// Reference every alias so `noUnusedLocals` keeps them checked rather than eliding them.
type _AllAssertions = [
  _FactorMatches,
  _FactorTypesMatch,
  _RunMatches,
  _RunTypesMatch,
  _ProblemType,
  _ProblemTitle,
  _ProblemStatus,
  _ProblemDetail,
  _ProblemInstance,
  _ProblemAcceptsExtensions,
  _RunBodyIsOptional,
  _RowHasRank,
  _RowHasSymbol,
  _StatusShape,
  _ScreenShape,
  _ColumnShape,
  _UniverseShape,
];

// --- run-time assertions against the emitted document ------------------------

const SPEC_PATH = fileURLToPath(new URL("../openapi.json", import.meta.url));
const spec = JSON.parse(readFileSync(SPEC_PATH, "utf-8")) as {
  paths: Record<string, Record<string, { operationId?: string }>>;
  components: { schemas: Record<string, unknown> };
};

describe("the emitted OpenAPI document", () => {
  it("documents every path the client types", () => {
    const typed: Array<keyof paths> = [
      "/api/v1/meta/factors",
      "/api/v1/meta/columns",
      "/api/v1/meta/universes",
      "/api/v1/meta/trading-days",
      "/api/v1/meta/status",
      "/api/v1/screens",
      "/api/v1/screens/{public_id}",
      "/api/v1/screens/{public_id}/run",
      "/api/v1/screens/{public_id}/csv",
      "/api/v1/screens/{public_id}/runs",
      "/api/v1/screens/{public_id}/duplicate",
      "/api/v1/screens/preview",
    ];
    for (const path of typed) {
      expect(Object.keys(spec.paths)).toContain(path);
    }
  });

  it("names every operation in camelCase", () => {
    const ids = Object.values(spec.paths).flatMap((methods) =>
      Object.values(methods)
        .map((operation) => operation.operationId)
        .filter((id): id is string => typeof id === "string"),
    );
    expect(ids.length).toBeGreaterThan(0);
    for (const id of ids) {
      expect(id).toMatch(/^[a-z][A-Za-z0-9]*$/);
    }
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("carries the ProblemOut schema the error responses reference", () => {
    expect(spec.components.schemas).toHaveProperty("ProblemOut");
  });
});

describe("createDecileClient", () => {
  it("attaches the bearer token to every request", async () => {
    const seen: Headers[] = [];
    const client = createDecileClient({
      baseUrl: "http://api.test",
      getAccessToken: () => "token-abc",
      fetch: async (request: Request) => {
        seen.push(request.headers);
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });

    await client.GET("/api/v1/meta/factors");
    expect(seen).toHaveLength(1);
    expect(seen[0]?.get("authorization")).toBe("Bearer token-abc");
  });

  it("omits the header when there is no token", async () => {
    const seen: Headers[] = [];
    const client = createDecileClient({
      baseUrl: "http://api.test",
      getAccessToken: () => undefined,
      fetch: async (request: Request) => {
        seen.push(request.headers);
        return new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });

    await client.GET("/api/v1/meta/universes");
    expect(seen[0]?.has("authorization")).toBe(false);
  });

  it("surfaces a problem document as the error branch", async () => {
    const problem: ProblemOut = {
      type: "payment-required",
      title: "Your plan does not include this feature",
      status: 402,
      detail: "'export_csv' is not included in your plan.",
      instance: "/api/v1/screens/exmpl0000001/csv",
      upgrade_url: "/pricing",
    };
    const client = createDecileClient({
      baseUrl: "http://api.test",
      fetch: async () =>
        new Response(JSON.stringify(problem), {
          status: 402,
          headers: { "content-type": "application/problem+json" },
        }),
    });

    const { data, error } = await client.GET("/api/v1/screens/{public_id}/csv", {
      params: { path: { public_id: "exmpl0000001" } },
    });
    expect(data).toBeUndefined();
    expect(isProblem(error)).toBe(true);
    expect((error as ProblemOut).type).toBe("payment-required");
  });
});

describe("PROBLEM_TYPES", () => {
  it("matches docs/07's error catalogue", () => {
    expect([...PROBLEM_TYPES]).toEqual([
      "invalid-screen-definition",
      "unauthenticated",
      "payment-required",
      "not-found",
      "stale-data-version",
      "no-trading-day",
      "rate-limited",
      "pipeline-degraded",
      "internal-error",
    ]);
  });

  it("rejects a non-problem body", () => {
    expect(isProblem(undefined)).toBe(false);
    expect(isProblem({ message: "oops" })).toBe(false);
  });
});
