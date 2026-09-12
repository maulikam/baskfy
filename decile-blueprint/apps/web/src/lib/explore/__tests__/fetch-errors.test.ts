import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth", () => ({
  auth: vi.fn(async () => ({ accessToken: "tok" })),
}));
vi.mock("@/lib/api/config", () => ({
  serverApiOrigin: () => "http://api.test",
}));

const serverFetchJson = vi.fn();
vi.mock("@/lib/api/server-fetch", async () => {
  class ServerFetchStatusError extends Error {
    readonly status: number;
    constructor(url: string, status: number) {
      super(`${url} responded ${status}`);
      this.status = status;
    }
  }
  class ServerFetchTimeoutError extends Error {
    readonly timeoutMs = 2500;
    constructor(path: string) {
      super(`Timed out fetching ${path}`);
    }
  }
  return {
    ServerFetchStatusError,
    ServerFetchTimeoutError,
    serverFetchJson: (...args: unknown[]) => serverFetchJson(...args),
  };
});

import {
  ExploreNotFound,
  ExploreUnavailable,
  readExploreJson,
} from "@/lib/explore/fetch";
import { ServerFetchStatusError, ServerFetchTimeoutError } from "@/lib/api/server-fetch";

/**
 * AUDIT 4.2 — a 404 must not become ExploreUnavailable (which pages used to map to notFound()).
 */
describe("readExploreJson error split", () => {
  beforeEach(() => {
    serverFetchJson.mockReset();
  });

  it("throws ExploreNotFound on HTTP 404", async () => {
    serverFetchJson.mockRejectedValue(
      new ServerFetchStatusError("http://api.test/api/v1/explore/x", 404),
    );
    await expect(readExploreJson("/explore/x")).rejects.toBeInstanceOf(ExploreNotFound);
  });

  it("throws ExploreUnavailable on timeout", async () => {
    serverFetchJson.mockRejectedValue(new ServerFetchTimeoutError("/explore/x"));
    await expect(readExploreJson("/explore/x")).rejects.toBeInstanceOf(ExploreUnavailable);
  });

  it("throws ExploreUnavailable on HTTP 503", async () => {
    serverFetchJson.mockRejectedValue(
      new ServerFetchStatusError("http://api.test/api/v1/explore/x", 503),
    );
    await expect(readExploreJson("/explore/x")).rejects.toBeInstanceOf(ExploreUnavailable);
  });
});
