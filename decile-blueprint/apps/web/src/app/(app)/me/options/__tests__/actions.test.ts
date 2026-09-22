import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The options tab's two writes, at the action boundary: what each can send, and what a refusal
 * reads as. The settings patch can name no pause, no execution switch and no ceiling — there is
 * no form field for any of them — and a 422 comes back as the sentence beside the form, naming
 * the limit, never the environment variable that sets it.
 */

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/options/write", () => ({ optionsWrite: vi.fn() }));

const { optionsWrite } = await import("@/lib/options/write");
const { saveOptionsConfig } = await import("../actions");
const { addEventDay, removeEventDay } = await import("../../../options/calendar/actions");

function form(entries: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(entries)) data.set(key, value);
  return data;
}

beforeEach(() => vi.mocked(optionsWrite).mockReset());

describe("the settings save", () => {
  it("sends the book and each group's fields, typed, as one patch", async () => {
    vi.mocked(optionsWrite).mockResolvedValue({ ok: true, status: 200, body: {} });
    const result = await saveOptionsConfig(
      null,
      form({
        "book.account_inr": "500000",
        "O2.max_lots": "3",
        "O2.hard_exit_time": "15:00",
        "O2.paper_enabled": "yes",
        "O3.risk_per_trade_pct": "0.50",
        // A field the form does not own is never forwarded.
        "O2.paused_until": "2026-12-31",
      }),
    );
    expect(result.ok).toBe(true);
    expect(optionsWrite).toHaveBeenCalledWith("PATCH", "/options/config", {
      body: {
        book: { account_inr: "500000" },
        sleeves: {
          O2: { max_lots: 3, hard_exit_time: "15:00", paper_enabled: true },
          O3: { risk_per_trade_pct: "0.50" },
        },
      },
    });
  });

  it("renders a ceiling refusal as the limit, without the variable's name", async () => {
    vi.mocked(optionsWrite).mockResolvedValue({
      ok: false,
      status: 422,
      error: "max_lots may not exceed 10; 99 was requested. (BASKFY_OPTIONS_MAX_LOTS_MAX)",
      field: "max_lots",
      ceiling: "10",
    });
    const result = await saveOptionsConfig(null, form({ "O2.max_lots": "99" }));
    expect(result).toMatchObject({ ok: false });
    if (!result.ok) {
      expect(result.error).toBe("Most lots: max 10 — set by the server. Nothing was saved.");
      expect(result.error).not.toMatch(/BASKFY_/);
    }
  });

  it("refuses a malformed number before any request", async () => {
    const result = await saveOptionsConfig(null, form({ "O1M.max_lots": "two" }));
    expect(result.ok).toBe(false);
    expect(optionsWrite).not.toHaveBeenCalled();
  });
});

describe("the event day", () => {
  it("adds a day with its note, and removes one by date", async () => {
    vi.mocked(optionsWrite).mockResolvedValue({ ok: true, status: 201, body: {} });
    const added = await addEventDay(null, form({ date: "2026-10-20", note: "state results" }));
    expect(added.ok).toBe(true);
    expect(optionsWrite).toHaveBeenCalledWith("POST", "/options/event-day", {
      body: { date: "2026-10-20", note: "state results" },
    });
    await removeEventDay(form({ date: "2026-10-20" }));
    expect(optionsWrite).toHaveBeenLastCalledWith("DELETE", "/options/event-day", {
      search: { date: "2026-10-20" },
    });
  });

  it("asks for a date rather than sending an empty one", async () => {
    const result = await addEventDay(null, form({ date: "" }));
    expect(result.ok).toBe(false);
    expect(optionsWrite).not.toHaveBeenCalled();
  });
});
