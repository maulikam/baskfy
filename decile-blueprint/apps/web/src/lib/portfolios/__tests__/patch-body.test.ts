import { describe, expect, it } from "vitest";

import { buildPatchBody } from "@/lib/portfolios/queries";

/**
 * `PATCH /portfolios/{id}` tells "absent" and "null" apart with `model_fields_set`, and they are
 * two different requests:
 *
 * - **omit** `parent_id` → leave the parent alone.
 * - send `parent_id: null` → promote this portfolio to a root.
 *
 * A client that cannot express both can only express the destructive one. `JSON.stringify` drops
 * an `undefined` value and keeps a `null`, so the assertion below is on the serialised body — the
 * thing the server actually receives — rather than on the object's shape in memory.
 */

describe("the move request can say both leave-alone and promote-to-root", () => {
  it("omits parent_id entirely when the caller did not mention it", () => {
    const body = buildPatchBody({ name: "Renamed" });
    expect("parent_id" in body).toBe(false);
    expect(JSON.stringify(body)).toBe('{"name":"Renamed"}');
  });

  it("sends an explicit null when the caller promotes to a root", () => {
    const body = buildPatchBody({ parentId: null });
    expect(body.parent_id).toBeNull();
    expect(JSON.stringify(body)).toBe('{"parent_id":null}');
  });

  it("sends the id when the caller files it under a parent", () => {
    expect(JSON.stringify(buildPatchBody({ parentId: 4 }))).toBe('{"parent_id":4}');
  });

  it("keeps the two requests distinguishable on the wire", () => {
    expect(JSON.stringify(buildPatchBody({ name: "X" }))).not.toBe(
      JSON.stringify(buildPatchBody({ name: "X", parentId: null })),
    );
  });

  it("treats broker re-attribution the same way", () => {
    expect("broker_account_id" in buildPatchBody({ name: "X" })).toBe(false);
    expect(JSON.stringify(buildPatchBody({ brokerAccountId: null }))).toBe(
      '{"broker_account_id":null}',
    );
    expect(JSON.stringify(buildPatchBody({ brokerAccountId: 7 }))).toBe(
      '{"broker_account_id":7}',
    );
  });

  it("can rename and move in one request", () => {
    expect(JSON.stringify(buildPatchBody({ name: "Core", parentId: 2 }))).toBe(
      '{"name":"Core","parent_id":2}',
    );
  });

  it("sends an empty body when nothing was asked for, rather than inventing a change", () => {
    expect(JSON.stringify(buildPatchBody({}))).toBe("{}");
  });
});
