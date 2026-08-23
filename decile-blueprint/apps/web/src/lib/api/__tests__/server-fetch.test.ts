/**
 * Tree-5 RSC perf — timed server fetch contracts.
 */
import { describe, expect, it, vi, afterEach } from "vitest";

import {
  SERVER_FETCH_TIMEOUT_MS,
  ServerFetchTimeoutError,
  serverFetchJson,
  serverFetchJsonOrNull,
} from "@/lib/api/server-fetch";

describe("server-fetch", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("exports a default timeout at or under 3000ms", () => {
    expect(SERVER_FETCH_TIMEOUT_MS).toBeLessThanOrEqual(3000);
    expect(SERVER_FETCH_TIMEOUT_MS).toBeGreaterThan(0);
  });

  it("serverFetchJson returns JSON on ok", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ ok: true }, { status: 200 }),
      ) as unknown as typeof fetch,
    );
    await expect(serverFetchJson({ url: "http://example.test/x" })).resolves.toEqual({
      ok: true,
    });
  });

  it("serverFetchJsonOrNull returns null on timeout", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
        const signal = init?.signal;
        return await new Promise<Response>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "TimeoutError";
            reject(err);
          });
        });
      }) as unknown as typeof fetch,
    );
    await expect(
      serverFetchJsonOrNull({ url: "http://example.test/slow", timeoutMs: 20 }),
    ).resolves.toBeNull();
  });

  it("serverFetchJson throws ServerFetchTimeoutError on abort", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
        const signal = init?.signal;
        return await new Promise<Response>((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        });
      }) as unknown as typeof fetch,
    );
    await expect(serverFetchJson({ url: "http://example.test/slow", timeoutMs: 20 })).rejects.toBeInstanceOf(
      ServerFetchTimeoutError,
    );
  });
});
