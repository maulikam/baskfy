import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * SW14 — the four watchlist actions from a `FormData` to exactly one call each: the symbol from
 * the search box goes as typed, levels go as strings (house rule 8), Dismiss is a DELETE and not a
 * GET, re-confirm is the one-field PATCH, and a refusal comes back to the form as a sentence.
 */

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/swing/write", () => ({ swingWrite: vi.fn() }));

const { swingWrite } = await import("@/lib/swing/write");
const { revalidatePath } = await import("next/cache");
const { watchAdd, watchAnnotate, watchDismiss, watchReconfirm } = await import("../actions");

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

beforeEach(() => {
  vi.mocked(swingWrite).mockReset();
  vi.mocked(swingWrite).mockResolvedValue({ ok: true, body: {}, status: 200 });
  vi.mocked(revalidatePath).mockReset();
});

describe("watchAdd", () => {
  it("posts the symbol as typed, upper-cased, with the levels as strings", async () => {
    const result = await watchAdd(
      null,
      form({ symbol: "flagco", setup: "FLAG", trigger: "149.60", stop_ref: "141.86" }),
    );
    expect(result).toEqual({ ok: true, message: "Watching FLAGCO." });
    expect(swingWrite).toHaveBeenCalledTimes(1);
    expect(swingWrite).toHaveBeenCalledWith("POST", "/swing/watch", {
      symbol: "FLAGCO",
      setup: "FLAG",
      trigger: "149.60",
      stop_ref: "141.86",
    });
    expect(revalidatePath).toHaveBeenCalledWith("/swing", "layout");
  });

  it("prefers the instrument id when a setups row supplies one", async () => {
    await watchAdd(null, form({ instrument_id: "7", symbol: "FLAGCO", setup: "EP" }));
    expect(swingWrite).toHaveBeenCalledWith("POST", "/swing/watch", {
      instrument_id: 7,
      setup: "EP",
      trigger: null,
      stop_ref: null,
    });
  });

  it("refuses a stop above the trigger, a bad setup and an empty symbol before any call", async () => {
    expect((await watchAdd(null, form({ symbol: "X", setup: "FLAG", trigger: "10", stop_ref: "11" }))).ok).toBe(false);
    expect((await watchAdd(null, form({ symbol: "X", setup: "PARABOLIC_SHORT" }))).ok).toBe(false);
    expect((await watchAdd(null, form({ setup: "FLAG" }))).ok).toBe(false);
    expect(swingWrite).not.toHaveBeenCalled();
  });

  it("says a 404 means the symbol is not one NSE lists", async () => {
    vi.mocked(swingWrite).mockResolvedValue({ ok: false, status: 404, error: "No instrument" });
    const result = await watchAdd(null, form({ symbol: "NOSUCH", setup: "FLAG" }));
    expect(result).toEqual({ ok: false, error: "NOSUCH is not a symbol NSE lists." });
  });
});

describe("watchDismiss, watchAnnotate and watchReconfirm", () => {
  it("dismisses with a DELETE on the row", async () => {
    const result = await watchDismiss(null, form({ id: "42" }));
    expect(result).toEqual({ ok: true, message: "Dismissed." });
    expect(swingWrite).toHaveBeenCalledWith("DELETE", "/swing/watch/42");
  });

  it("annotates with the two text fields and nothing else", async () => {
    await watchAnnotate(null, form({ id: "42", note: " tight base ", catalyst: "" }));
    expect(swingWrite).toHaveBeenCalledWith("PATCH", "/swing/watch/42", {
      note: "tight base",
      catalyst: null,
    });
  });

  it("re-confirms with the one-field PATCH", async () => {
    const result = await watchReconfirm(null, form({ id: "42" }));
    expect(result.ok).toBe(true);
    expect(swingWrite).toHaveBeenCalledWith("PATCH", "/swing/watch/42", { reconfirm: true });
  });

  it("passes the server's refusal through to the form", async () => {
    vi.mocked(swingWrite).mockResolvedValue({
      ok: false,
      status: 400,
      error: "Only a WATCHING MANUAL row can be re-confirmed",
    });
    const result = await watchReconfirm(null, form({ id: "42" }));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toContain("MANUAL");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("refuses a missing id without a call", async () => {
    expect((await watchDismiss(null, form({}))).ok).toBe(false);
    expect(swingWrite).not.toHaveBeenCalled();
  });
});
