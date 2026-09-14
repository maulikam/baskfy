import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth", () => ({ auth: vi.fn() }));

import { auth } from "@/lib/auth";
import { fetchRegime } from "@/lib/desk/fetch";

/**
 * The API requires a signed-in caller on every `/desk/*` route (`routers/desk.py`,
 * `require_authenticated`). A reader that omits the bearer token gets 401 on every call, which the
 * portfolio page then reports as an unreachable desk.
 */
describe("desk reads carry the signed-in user's token", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ tier: "NORMAL" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.mocked(auth).mockReset();
    fetchMock.mockReset();
  });

  it("sends Authorization: Bearer <token> to /desk/regime", async () => {
    vi.mocked(auth).mockResolvedValue({ accessToken: "tok-123" } as never);
    await fetchRegime();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/desk\/regime$/);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-123");
  });

  it("sends no Authorization header when nobody is signed in", async () => {
    vi.mocked(auth).mockResolvedValue(null as never);
    await fetchRegime();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });
});
