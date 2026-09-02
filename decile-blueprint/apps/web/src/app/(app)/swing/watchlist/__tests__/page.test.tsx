import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { formatTradeDate } from "@/lib/format";
import type { SwingSignal, SwingWatchRow } from "@/lib/swing/fetch";

import { signalSentence, stopShareOfAdr } from "../copy";
import SwingWatchlistPage from "../page";

/**
 * SW11B, the watchlist half — `docs/swing/STANDING-ANSWERS.md` A3 and `05` §2:
 *
 * - the typed (or auto-filled) `catalyst` text and the feed's **link** are two things on the
 *   same cell: the text stays, the headline links to the exchange's copy in a new tab;
 * - the earnings badge follows the row's own flag even when the feed has no announcement;
 * - nothing from a filing is rendered.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchWatchlist: vi.fn(),
  fetchSignals: vi.fn(),
}));

// The actions reach `next-auth` through the write helper; the page only binds them to forms.
vi.mock("../../actions", () => ({
  watchAdd: vi.fn(),
  watchDismiss: vi.fn(),
  watchAnnotate: vi.fn(),
  watchReconfirm: vi.fn(),
}));

// The symbol box's typeahead is a browser call; nothing here types into it.
vi.mock("@/lib/api/search", () => ({
  searchCatalog: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/swing/watchlist",
}));

const { fetchWatchlist, fetchSignals } = await import("@/lib/swing/fetch");

function signal(overrides: Partial<SwingSignal> = {}): SwingSignal {
  return {
    id: 100,
    watch_id: 1,
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    session_date: "2026-09-02",
    // 09:23 IST, carried as the instant.
    raised_at: "2026-09-02T03:53:00+00:00",
    state: "TRIGGERED",
    or_window_minutes: 5,
    range_high: 418.9,
    range_low: 412.3,
    low_of_day: 412.3,
    last_price: 419.35,
    entry: 419.35,
    stop: 412.3,
    plan_line_id: null,
    ...overrides,
  };
}

const FILING =
  "https://nsearchives.nseindia.com/corporate/FLAGCO_02092026084105_PR.pdf";

function row(overrides: Partial<SwingWatchRow> = {}): SwingWatchRow {
  return {
    id: 1,
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    source: "DETECTOR",
    added_on: "2026-09-01",
    expires_on: "2026-09-15",
    trigger: 149.6,
    stop_ref: 141.86,
    distance_to_trigger_pct: 3.01,
    last_close: 145.1,
    note: null,
    catalyst: null,
    state: "WATCHING",
    earnings_date: null,
    catalyst_feed: null,
    ...overrides,
  };
}

async function renderWith(rows: SwingWatchRow[], signals: SwingSignal[] = []) {
  vi.mocked(fetchWatchlist).mockResolvedValue({ data: rows });
  vi.mocked(fetchSignals).mockResolvedValue({
    session_date: signals.length > 0 ? "2026-09-02" : null,
    data: signals,
  });
  return render(await SwingWatchlistPage());
}

function rowOf(symbol: string): HTMLElement {
  const row = screen.getByText(symbol).closest("tr");
  if (!row) throw new Error(`no row for ${symbol}`);
  return row;
}

describe("the watchlist links out to the catalyst beside the typed note", () => {
  it("keeps the typed text and links the feed's headline in a new tab", async () => {
    await renderWith([
      row({
        catalyst: "Q2 result, order book up",
        catalyst_feed: {
          headline: "Press Release - FLAGCO wins a multi-year order",
          published_at: "2026-09-02T08:41:05+05:30",
          url: FILING,
          earnings_date: null,
        },
      }),
    ]);
    expect(screen.getByText("Q2 result, order book up")).toBeInTheDocument();
    const link = screen.getByRole("link", {
      name: "Press Release - FLAGCO wins a multi-year order",
    });
    expect(link).toHaveAttribute("href", FILING);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("shows the earnings badge from the row's own flag when the feed has no announcement", async () => {
    await renderWith([
      row({ earnings_date: "2026-10-15", catalyst_feed: null }),
    ]);
    expect(screen.getByText(/earnings 15 Oct 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /filing/i })).toBeNull();
  });

  it("renders an em dash and nothing else for a name with no catalyst at all", async () => {
    await renderWith([row()]);
    const cell = screen.getByText("FLAGCO").closest("tr");
    expect(cell).not.toBeNull();
    expect(cell?.textContent).toContain("—");
    expect(cell?.textContent).not.toMatch(/earnings/i);
    expect(cell?.querySelector("a")).toBeNull();
  });
});

/**
 * SW14 — `05` §2's watchlist, completed: the add-by-hand form, the note and catalyst edited in
 * place, "Still watching" on a MANUAL row with its dates, Dismiss, the stop as a share of the
 * ADR with the will-skip mark, the focus mark and the 20/5 counts, and yesterday's signals
 * under the row.
 */
describe("the add-by-hand form has the three fields and a symbol box", () => {
  it("renders symbol, setup, trigger and stop reference, posting to the add action", async () => {
    await renderWith([]);
    const form = screen.getByRole("button", { name: "Watch" }).closest("form");
    expect(form).not.toBeNull();
    expect(within(form as HTMLElement).getByLabelText("Symbol")).toHaveAttribute("name", "symbol");
    expect(within(form as HTMLElement).getByLabelText("Setup")).toHaveAttribute("name", "setup");
    expect(within(form as HTMLElement).getByLabelText("Trigger")).toHaveAttribute("name", "trigger");
    expect(within(form as HTMLElement).getByLabelText("Stop reference")).toHaveAttribute(
      "name",
      "stop_ref",
    );
    expect(form).not.toHaveAttribute("method");
    expect(form?.getAttribute("action") ?? "").not.toMatch(/^\//);
  });
});

describe("the row controls", () => {
  it("gives a MANUAL row Still watching and shows when it was re-confirmed and when it expires", async () => {
    await renderWith([
      row({
        id: 9,
        source: "MANUAL",
        added_on: "2026-08-20",
        reconfirmed_on: "2026-09-01",
        expires_on: "2026-09-15",
      }),
    ]);
    const tr = rowOf("FLAGCO");
    const still = within(tr).getByRole("button", { name: "Still watching" });
    expect(still.closest("form")?.querySelector('input[name="id"]')).toHaveValue("9");
    expect(tr).toHaveTextContent(`re-confirmed ${formatTradeDate("2026-09-01")}`);
    expect(tr).toHaveTextContent(`expires ${formatTradeDate("2026-09-15")}`);
    expect(tr).toHaveTextContent("by hand");
  });

  it("gives a DETECTOR row Dismiss but not Still watching", async () => {
    await renderWith([row({ id: 3 })]);
    const tr = rowOf("FLAGCO");
    expect(within(tr).queryByRole("button", { name: "Still watching" })).toBeNull();
    const dismiss = within(tr).getByRole("button", { name: "Dismiss" });
    expect(dismiss.closest("form")?.querySelector('input[name="id"]')).toHaveValue("3");
    expect(dismiss.closest("form")).not.toHaveAttribute("method");
  });

  it("edits the note and the catalyst in place, and no level", async () => {
    await renderWith([row({ id: 3, note: "tight", catalyst: "order win" })]);
    const tr = rowOf("FLAGCO");
    const save = within(tr).getByRole("button", { name: "Save note" });
    const form = save.closest("form") as HTMLFormElement;
    expect(form.querySelector('input[name="note"]')).toHaveValue("tight");
    expect(form.querySelector('input[name="catalyst"]')).toHaveValue("order win");
    expect(form.querySelector('input[name="trigger"]')).toBeNull();
    expect(form.querySelector('input[name="stop_ref"]')).toBeNull();
  });
});

describe("the stop as a share of the ADR", () => {
  it("shows the share and marks a stop wider than one ADR as one the plan will skip", async () => {
    // (149.60 - 141.86) / 149.60 = 5.17%; against a 4.00% ADR that is 1.29 ADR.
    await renderWith([row({ adr_pct: 4 })]);
    const tr = rowOf("FLAGCO");
    expect(tr).toHaveTextContent("1.29 ADR");
    expect(within(tr).getByText("will skip")).toBeInTheDocument();
  });

  it("does not mark a stop inside one ADR", async () => {
    await renderWith([row({ adr_pct: 6 })]);
    const tr = rowOf("FLAGCO");
    expect(tr).toHaveTextContent("0.86 ADR");
    expect(within(tr).queryByText("will skip")).toBeNull();
  });

  it("is an em dash when the row has no ADR", () => {
    expect(stopShareOfAdr(row({ adr_pct: null }))).toBeNull();
    const { adr_pct: _omitted, ...withoutAdr } = row();
    expect(stopShareOfAdr(withoutAdr as SwingWatchRow)).toBeNull();
  });
});

describe("focus and the funnel counts", () => {
  it("marks focus rows, puts them first and counts them against 5 and 20", async () => {
    await renderWith([
      row({ id: 1, symbol: "FARCO", distance_to_trigger_pct: 1.0, focus: false }),
      row({ id: 2, symbol: "NEARCO", instrument_id: 2, distance_to_trigger_pct: 8.0, focus: true }),
      row({
        id: 3,
        symbol: "EPCO",
        instrument_id: 3,
        setup: "EP",
        focus: true,
        distance_to_trigger_pct: null,
      }),
    ]);
    const symbols = screen
      .getAllByRole("row")
      .map((tr) => tr.textContent ?? "")
      .filter((text) => /FARCO|NEARCO|EPCO/.test(text))
      .map((text) => text.match(/FARCO|NEARCO|EPCO/)?.[0]);
    expect(symbols).toEqual(["NEARCO", "EPCO", "FARCO"]);
    expect(rowOf("NEARCO")).toHaveAttribute("data-focus", "true");
    expect(rowOf("FARCO")).not.toHaveAttribute("data-focus");
    const funnel = screen.getByTestId("funnel");
    expect(funnel).toHaveTextContent("3 names");
    expect(funnel).toHaveTextContent("2 in focus of 5 + every pivot");
    expect(funnel).toHaveTextContent("2 of the 20 flags the evening watches");
    expect(funnel).toHaveTextContent("1 pivot");
  });
});

describe("yesterday's signals under the row", () => {
  it("says fired 09:23, 5-min range 412.30–418.90 under the name that fired", async () => {
    await renderWith([row()], [signal()]);
    const line = screen.getByTestId("signal");
    expect(line).toHaveTextContent("fired 09:23, 5-min range 412.30–418.90");
    expect(line).toHaveTextContent("entry 419.35, stop 412.30");
    expect(screen.getByText(formatTradeDate("2026-09-02"))).toBeInTheDocument();
    expect(fetchSignals).toHaveBeenCalledWith();
  });

  it("keeps the other verdicts as what they were, and shows nothing under a quiet name", async () => {
    await renderWith(
      [row(), row({ id: 2, instrument_id: 2, symbol: "QUIETCO" })],
      [
        signal({ id: 1, state: "BELOW_PIVOT", raised_at: "2026-09-02T03:51:00+00:00", entry: null, stop: null }),
        signal({ id: 2, state: "LOCKED_UPPER_CIRCUIT", raised_at: "2026-09-02T03:50:00+00:00" }),
      ],
    );
    const lines = screen.getAllByTestId("signal").map((node) => node.textContent);
    expect(lines).toEqual([
      "09:21: broke the range but not the pivot, 5-min range 412.30–418.90",
      "09:20: locked at its upper circuit — no seller, no fill",
    ]);
    const quiet = rowOf("QUIETCO").nextElementSibling;
    expect(quiet?.textContent).toBe("");
  });

  it("names a plan line when the trigger became one", () => {
    expect(signalSentence(signal({ plan_line_id: 5 }))).toContain("a plan line was made");
    expect(signalSentence(signal({ state: "SESSION_OVER" }))).toContain("closed without a trigger");
  });
});
