import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TwtScanRun } from "@/lib/twt/fetch";

import { backtest, emptyToday, gate, today } from "@/lib/twt/__tests__/fixtures";

import TwtBacktestPage from "../backtest/page";
import TwtPage from "../page";

/**
 * The two `/twt` routes, rendered end to end over a mocked read — TW8.
 *
 * The components have their own tests; what these add is the wiring: that the page asks for the
 * session it renders, that an absent payload produces the empty state rather than a crash, and
 * that the answer at the top of the page agrees with the card below it about the same gate. A
 * page whose headline and whose card disagree is worse than either being wrong alone.
 */

/* A factory, not `importActual`: the real module imports the session helper, which pulls the
   whole auth stack into a jsdom run for no benefit. The types are all that is shared, and types
   are erased. */
vi.mock("@/lib/twt/fetch", () => ({
  fetchToday: vi.fn(),
  fetchBacktest: vi.fn(),
  fetchLastScan: vi.fn(),
}));

/* The action reaches `next-auth` through the write helper; the page only binds it to the form,
   and "Scan now" has its own suite in `scan-now.test.tsx`. Same reason the swing page test mocks
   its own `../actions`. */
vi.mock("../actions", () => ({ scanNow: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/twt",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const { fetchToday, fetchBacktest, fetchLastScan } = await import("@/lib/twt/fetch");

/* No scan has been run in any of these fixtures — the state the sleeve is genuinely in. */
vi.mocked(fetchLastScan).mockResolvedValue(null);

describe("the twt page answers the gate before it lists anything", () => {
  it("says new entries are allowed, and the card underneath agrees", async () => {
    vi.mocked(fetchToday).mockResolvedValue(today());

    render(await TwtPage());

    expect(screen.getByTestId("twt-answer-gate")).toHaveTextContent("allowed");
    expect(screen.getByTestId("twt-gate-badge")).toHaveTextContent("OPEN");
  });

  it("says new entries are not allowed under a shut gate, without saying sell", async () => {
    vi.mocked(fetchToday).mockResolvedValue(
      today({ gate: gate({ gate: "SHUT", pct_above_dma: "28.1000" }) }),
    );

    render(await TwtPage());

    expect(screen.getByTestId("twt-answer-gate")).toHaveTextContent("not allowed");
    expect(screen.getByTestId("twt-gate-meaning")).toHaveTextContent(/managed exactly as always/i);
    expect(document.body.textContent).not.toMatch(/sell everything/i);
  });

  /* TW4 and TW5 have not landed, so this is the state the page is genuinely in today. It has to
     read as "nothing has run yet", never as "the market is quiet". */
  it("state: nothing computed at all reads as nothing computed, not as a quiet market", async () => {
    vi.mocked(fetchToday).mockResolvedValue(null);

    render(await TwtPage());

    expect(screen.getByText(/Nothing has been read for this strategy yet/i)).toBeInTheDocument();
    expect(screen.getByTestId("twt-gate-badge")).toHaveTextContent("Not read yet");
  });

  it("state: an empty session still carries the gate and the open positions sections", async () => {
    vi.mocked(fetchToday).mockResolvedValue(emptyToday());

    render(await TwtPage());

    expect(screen.getByTestId("twt-open-empty")).toBeInTheDocument();
    expect(screen.getByTestId("twt-half-size")).toBeInTheDocument();
  });
});

describe("the twt backtest page reads its conditions first", () => {
  it("renders the caveats and the two sources", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue(backtest());

    render(await TwtBacktestPage());

    expect(screen.getByTestId("twt-caveats")).toBeInTheDocument();
    expect(screen.getByText("Measured on our own price history")).toBeInTheDocument();
  });

  it("state: no run at all still shows the caveats", async () => {
    vi.mocked(fetchBacktest).mockResolvedValue(null);

    render(await TwtBacktestPage());

    expect(screen.getByTestId("twt-caveats")).toBeInTheDocument();
    expect(screen.getByTestId("twt-backtest-empty")).toBeInTheDocument();
  });
});

/**
 * "Scan now" is on the page — leaf 5 of `PLAN-SCAN-SYNC.md`.
 *
 * The control's own behaviour is `scan-now.test.tsx`; what this adds is the wiring, and the one
 * claim the page must never stop making: the button is here, it is the only one, and nothing on
 * this page can place an order — or set a capital, or flip a switch.
 */
describe("Scan now is on the hub, and it is the only control there", () => {
  async function renderHub(lastScan: TwtScanRun | null = null) {
    vi.mocked(fetchToday).mockResolvedValue(today());
    vi.mocked(fetchLastScan).mockResolvedValue(lastScan);
    render(await TwtPage());
  }

  it("offers the button in the header, enabled, with nothing said about a run yet", async () => {
    await renderHub();

    const control = screen.getByTestId("twt-scan-now");
    expect(within(control).getByRole("button", { name: "Scan now" })).toBeEnabled();
    expect(screen.getByTestId("twt-scan-status")).toHaveTextContent("");
  });

  it("shows the last run's state beside it when the payload names one", async () => {
    await renderHub({
      id: 4,
      status: "RUNNING",
      requested_at: "2026-09-12T08:10:00+00:00",
      finished_at: null,
      error: null,
    });

    expect(screen.getByTestId("twt-scan-status")).toHaveTextContent("Scanning now.");
    expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled();
  });

  /* The state this sleeve is genuinely in: every detection table at zero rows. The button has to
     be pressable there above all, because that is the screen Maulik is looking at. */
  it("offers the button on a hub with nothing computed at all", async () => {
    vi.mocked(fetchToday).mockResolvedValue(emptyToday());
    vi.mocked(fetchLastScan).mockResolvedValue(null);

    render(await TwtPage());

    expect(screen.getByRole("button", { name: "Scan now" })).toBeEnabled();
  });

  it("renders exactly one button on the whole page, and it is Scan now", async () => {
    await renderHub();

    const names = screen.getAllByRole("button").map((node) => node.textContent?.trim());
    expect(names).toEqual(["Scan now"]);
  });

  it("still tells the reader, in the page's own words, that nothing here can place an order", async () => {
    await renderHub();

    expect(document.body.textContent).toContain("nothing on this page can place an order");
  });
});
