import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GateCard } from "@/components/twt/gate-card";
import { HalfSizeCounter } from "@/components/twt/half-size-counter";
import { OpenPositions } from "@/components/twt/open-positions";
import { TightNames } from "@/components/twt/tight-names";
import {
  emptyToday,
  gate,
  halfSize,
  nakedPosition,
  position,
  tightName,
  today,
} from "@/lib/twt/__tests__/fixtures";
import { todayView } from "@/lib/twt/view";

/**
 * TW8's rendered-DOM acceptance for `docs/twt/05` §1 — the four things a reader must be able to
 * see whatever the data does, and the three states `06` TW8 names by hand.
 *
 * These render the components against fixtures written to `03-data-model.md` rather than against
 * a live payload, because TW4 and TW5 have not landed. That is the point of building the pages
 * first: the three states below — an empty strategy, a shut gate, an unprotected position — are
 * the states this page will actually spend its first weeks in, and they are the ones a page built
 * after its data never gets tested in.
 */

describe("the twt hub shows the gate before it shows a single name", () => {
  it("reads OPEN and says plainly what that allows", () => {
    render(<GateCard gate={gate()} />);

    expect(screen.getByTestId("twt-gate-badge")).toHaveTextContent("OPEN");
    expect(screen.getByTestId("twt-gate-meaning")).toHaveTextContent(
      /new entries may be planned/i,
    );
    expect(screen.getByText(/56\.4% of 2,894 names/)).toBeInTheDocument();
    expect(screen.getByText(/the gate opens above 40%/)).toBeInTheDocument();
  });

  /**
   * `05` §1.1: "A reader must never have to infer that exits keep running."
   *
   * The failure this guards against is a single word doing the wrong work: SHUT read as "get
   * out". The sentence has to contain both halves — no new entries AND everything open is managed
   * as always — and asserting only the first half would let the second be deleted.
   */
  it("reads SHUT and still says the open positions are managed as always", () => {
    render(<GateCard gate={gate({ gate: "SHUT", pct_above_dma: "31.2000" })} />);

    expect(screen.getByTestId("twt-gate-badge")).toHaveTextContent("SHUT");
    const meaning = screen.getByTestId("twt-gate-meaning");
    expect(meaning).toHaveTextContent(/no new entries/i);
    expect(meaning).toHaveTextContent(/managed exactly as always/i);
    expect(meaning).toHaveTextContent(/the stops stay where they are/i);
  });

  it("carries the funnel in a disclosure rather than down the page", () => {
    render(<GateCard gate={gate()} />);

    const funnel = screen.getByTestId("twt-funnel");
    expect(funnel).toHaveTextContent("4,855");
    expect(funnel).toHaveTextContent("2,894");
    expect(funnel).toHaveTextContent("1,632");
    /* Closed by default. An empty account rendered 4,661 pixels on a phone on 11 Sep 2026 because
       every explanation was open; a disclosure that starts open is the same defect in one place. */
    expect(funnel).not.toHaveAttribute("open");
  });

  it("says a thin session is a thin session and publishes no percentage for it", () => {
    render(
      <GateCard
        gate={gate({ gate: "SHUT", thin_session: true, pct_above_dma: null, measured_count: null })}
      />,
    );

    expect(screen.getByTestId("twt-thin-session")).toBeInTheDocument();
    expect(screen.queryByText(/% of .* names are above/)).not.toBeInTheDocument();
  });
});

describe("the twt hub names today's quiet stocks, and what it passed over", () => {
  /**
   * `05` §1.2's sentence, verbatim, and the reason it is verbatim: it is the answer to the single
   * most likely question about this screen — why it lists fewer names than the published version
   * of the same scan on some days. A paraphrase that loses "point-in-time" or loses the
   * explanation of Chartink's week is a paraphrase that stops answering it.
   */
  it("prints the point-in-time sentence under the table, word for word", () => {
    const view = todayView(today());
    render(<TightNames rows={view.tight} session="2026-09-10" />);

    expect(screen.getByTestId("twt-point-in-time")).toHaveTextContent(
      "Computed point-in-time from the close of 10 Sept 2026. Chartink's own backtest export " +
        "uses the week's final close on every day of that week, so it names some stocks this " +
        "screen does not — see the method note.",
    );
  });

  it("prints the point-in-time sentence even when no name held the pattern", () => {
    render(<TightNames rows={[]} session="2026-09-10" />);

    expect(screen.getByTestId("twt-point-in-time")).toBeInTheDocument();
    expect(screen.getByTestId("twt-tight-empty")).toHaveTextContent(
      /No name held the pattern on this session/i,
    );
  });

  it("shows what the liquidity floor rejected, with the reason in words", () => {
    const view = todayView(
      today({
        tight: [
          tightName(),
          tightName({
            instrument_id: 102,
            symbol: "THINCO",
            name: "THINCO LIMITED",
            signal_state: "SCAN_ONLY",
            failed_filters: ["TURNOVER"],
            turnover_avg_20: "4100000",
          }),
        ],
      }),
    );
    render(<TightNames rows={view.tight} session="2026-09-10" />);

    const passed = screen.getByTestId("twt-watch-only");
    expect(passed).toHaveTextContent("THINCO");
    expect(passed).toHaveTextContent(/not enough trades through it on an average day/i);
    /* The tag it is stored under must not be what the reader is shown. */
    expect(passed).not.toHaveTextContent("TURNOVER");
  });

  it("sorts by what can actually be traded, heaviest first", () => {
    const view = todayView(
      today({
        tight: [
          tightName({ instrument_id: 1, symbol: "SMALLCO", turnover_avg_20: "12000000" }),
          tightName({ instrument_id: 2, symbol: "BIGCO", turnover_avg_20: "980000000" }),
        ],
      }),
    );
    render(<TightNames rows={view.tight} session="2026-09-10" />);

    const rows = screen.getAllByTestId("twt-tight-row");
    expect(rows[0]).toHaveTextContent("BIGCO");
    expect(rows[1]).toHaveTextContent("SMALLCO");
  });
});

describe("the twt hub shows every open position and the room under it", () => {
  it("puts the distance to the trigger on the row, because that is the number to agree to", () => {
    const view = todayView(today());
    render(<OpenPositions rows={view.positions} />);

    const row = screen.getByTestId("twt-position-row");
    /* (441.20 − 364.00) / 441.20 × 100 = 17.5%. Computed on decimal strings, not on floats. */
    expect(row).toHaveTextContent("17.5%");
    expect(row).toHaveTextContent("₹364.00");
    expect(row).toHaveTextContent("₹455.00");
  });

  it("converts the gain exactly once: a 42.07% position is not 0.4207%", () => {
    const view = todayView(today());
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-position-row")).toHaveTextContent("+42.1%");
    expect(screen.queryByText(/0\.4%/)).not.toBeInTheDocument();
  });

  it("names tomorrow's stop when the evening has already worked it out", () => {
    const view = todayView(today());
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-ratchet-due")).toHaveTextContent("₹364.00");
    expect(screen.getByTestId("twt-ratchet-due")).toHaveTextContent("₹366.40");
  });

  /** The first of `06` TW8's three states. */
  it("state: an empty strategy explains itself rather than showing an empty table", () => {
    const view = todayView(emptyToday());
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-open-empty")).toHaveTextContent(
      /Nothing is open in this strategy/i,
    );
    expect(screen.queryByTestId("twt-position-row")).not.toBeInTheDocument();
  });

  /**
   * The third of the three states, and the one that matters most.
   *
   * A position with shares open and nothing resting at the exchange is unprotected right now. The
   * row must say so in words — and the two figures that depend on a stop must say the same thing
   * rather than showing a zero, because a zero distance reads as "the stop is right at the price"
   * when the truth is that there is no stop at all.
   */
  it("state: a naked line says there is no stop, and never shows a zero distance", () => {
    const view = todayView(today({ positions: [nakedPosition()] }));
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-naked")).toHaveTextContent("No stop resting");
    expect(screen.getByTestId("twt-naked-note")).toHaveTextContent(/is not protected/i);
    const row = screen.getByTestId("twt-position-row");
    expect(row).toHaveTextContent("No stop resting at the exchange");
    expect(row).not.toHaveTextContent("0.0%");
  });

  it("labels a position taken at half size, and one that was only simulated", () => {
    const view = todayView(today({ positions: [position()] }));
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-half-size-badge")).toHaveTextContent("Half size");
    expect(screen.getByTestId("twt-simulated-badge")).toHaveTextContent("Practice only");
  });
});

describe("the twt hub shows the first-live discipline without a settings page", () => {
  it("says trading is off rather than counting down while nothing can trade", () => {
    render(<HalfSizeCounter halfSize={halfSize()} />);

    expect(screen.getByTestId("twt-half-size")).toHaveTextContent(
      /Trading is switched off for this strategy/i,
    );
  });

  it("counts the entries that remain once it is switched on", () => {
    render(<HalfSizeCounter halfSize={halfSize({ execution_enabled: true, entries_left: 7 })} />);

    expect(screen.getByTestId("twt-half-size")).toHaveTextContent(
      "7 of 10 first live entries remaining at half size.",
    );
  });
});
