import type { ImportReportOut, RebalanceNameOut } from "@baskfy/api-client";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BufferExplainer } from "@/components/portfolios/buffer-explainer";
import { ImportReport } from "@/components/portfolios/import-report";
import { ResultColumn } from "@/components/portfolios/result-column";
import { clipboardText, csvText, filenameFor } from "@/lib/portfolios/export";

/**
 * The rebalance tracker's rendering rules.
 *
 * The *rule* is asserted in `packages/core/tests/test_rebalance.py` and the endpoint in
 * `services/api/tests/test_api_portfolios.py`. What this suite checks is what docs/08
 * §"Rebalance tracker" and PROMPTS.md Prompt 14 §§1 and 3 require of the surface: the three
 * columns with copy and CSV, unmatched symbols shown prominently rather than dropped, and an
 * explanation of the buffer rule with the user's own numbers in it.
 */

function name(symbol: string, rank: number | null, reason?: RebalanceNameOut["reason"]): RebalanceNameOut {
  return {
    instrument_id: symbol.length + (rank ?? 0),
    symbol,
    name: `${symbol}, LIMITED`,
    rank,
    ...(reason ? { reason } : {}),
  };
}

describe("the three result columns", () => {
  it("shows the rank, the symbol and an exit reason", () => {
    render(
      <ResultColumn
        slug="exits"
        title="Exits"
        tone="negative"
        description="Ranked outside the band."
        emptyMessage="Nothing to sell."
        rows={[name("HFCL", 31, "rank_outside_buffer")]}
        asOf="2026-08-18"
      />,
    );
    const column = screen.getByTestId("column-exits");
    expect(within(column).getByText("31")).toBeInTheDocument();
    expect(within(column).getByText("HFCL")).toBeInTheDocument();
    expect(within(column).getByText("Ranked outside the buffer")).toBeInTheDocument();
  });

  it("offers copy-to-clipboard and CSV, disabled when the column is empty", () => {
    render(
      <ResultColumn
        slug="entries"
        title="Entries"
        tone="positive"
        description="In the top N."
        emptyMessage="Nothing to buy — you already hold the whole top N."
        rows={[]}
        asOf="2026-08-18"
      />,
    );
    expect(screen.getByTestId("copy-entries")).toBeDisabled();
    expect(screen.getByTestId("csv-entries")).toBeDisabled();
  });

  it("says what an empty column means rather than showing a zero", () => {
    render(
      <ResultColumn
        slug="exits"
        title="Exits"
        tone="negative"
        description="Ranked outside the band."
        emptyMessage="Nothing to sell. Every holding is still inside the buffer."
        rows={[]}
        asOf="2026-08-18"
      />,
    );
    expect(
      screen.getByText("Nothing to sell. Every holding is still inside the buffer."),
    ).toBeInTheDocument();
  });

  it("shows an em dash where a name has no rank at all", () => {
    render(
      <ResultColumn
        slug="exits"
        title="Exits"
        tone="negative"
        description="Gone from the screen."
        emptyMessage="Nothing to sell."
        rows={[name("ZZZZ", null, "not_in_screen")]}
        asOf="2026-08-18"
      />,
    );
    expect(within(screen.getByTestId("column-exits")).getByText("—")).toBeInTheDocument();
  });
});

describe("the clipboard and CSV payloads", () => {
  const rows = [name("CUPID", 1), name("HFCL", 31, "rank_outside_buffer")];

  it("copies bare symbols, one per line, with no header", () => {
    expect(clipboardText(rows)).toBe("CUPID\nHFCL");
  });

  it("writes a CSV with the rank and the reason in words", () => {
    expect(csvText(rows)).toBe(
      "symbol,name,rank,reason\n" +
        'CUPID,"CUPID, LIMITED",1,\n' +
        'HFCL,"HFCL, LIMITED",31,Ranked outside the buffer\n',
    );
  });

  it("quotes a name containing a comma, like the screener export does", () => {
    expect(csvText([name("ABC", 2)])).toContain('"ABC, LIMITED"');
  });

  it("writes the header alone for an empty list", () => {
    expect(csvText([])).toBe("symbol,name,rank,reason\n");
  });

  it("names the file after the list and the date it was computed for", () => {
    expect(filenameFor("exits", "2026-08-18")).toBe("baskfy-exits-2026-08-18.csv");
  });
});

describe("the import report", () => {
  function report(overrides: Partial<ImportReportOut> = {}): ImportReportOut {
    return {
      total_lines: 4,
      matched: 2,
      ambiguous: 0,
      unmatched: 1,
      skipped: 1,
      imported: 2,
      rows: [],
      skipped_rows: [],
      ignored_columns: [],
      ...overrides,
    };
  }

  it("shows an unmatched symbol with its line number and the reason in words", () => {
    render(
      <ImportReport
        report={report({
          rows: [
            {
              line: 5,
              raw_symbol: "532540",
              symbol: "532540",
              status: "unmatched",
              reason: "bse_code",
              candidates: [],
              issues: [],
              matched_via_alias: false,
            },
          ],
        })}
      />,
    );
    const problems = screen.getByTestId("import-problems");
    expect(within(problems).getByText(/532540/)).toBeInTheDocument();
    expect(within(problems).getByText(/BSE scrip code/)).toBeInTheDocument();
    expect(within(problems).getByText(/\(line 5\)/)).toBeInTheDocument();
  });

  it("lists the candidates for an ambiguous symbol instead of picking one", () => {
    render(
      <ImportReport
        report={report({
          ambiguous: 1,
          rows: [
            {
              line: 2,
              raw_symbol: "twinco",
              symbol: "TWINCO",
              status: "ambiguous",
              candidates: [
                { instrument_id: 1, symbol: "TWINCO", name: "Twin EQ", series: "EQ" },
                { instrument_id: 2, symbol: "TWINCO", name: "Twin BE", series: "BE" },
              ],
              issues: [],
              matched_via_alias: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/TWINCO·EQ or TWINCO·BE/)).toBeInTheDocument();
  });

  it("reports skipped rows rather than dropping them silently", () => {
    render(
      <ImportReport
        report={report({ skipped_rows: [{ line: 3, reason: "blank", raw: "" }] })}
      />,
    );
    expect(screen.getByText(/1 row produced no holding/)).toBeInTheDocument();
    expect(screen.getByText(/Line 3: Blank line/)).toBeInTheDocument();
  });

  it("names the columns it ignored", () => {
    render(<ImportReport report={report({ ignored_columns: ["Broker Note", "ISIN"] })} />);
    expect(screen.getByText("Columns ignored: Broker Note, ISIN.")).toBeInTheDocument();
  });

  it("does not list matched rows one by one", () => {
    render(
      <ImportReport
        report={report({
          unmatched: 0,
          rows: [
            {
              line: 2,
              raw_symbol: "cupid",
              symbol: "CUPID",
              status: "matched",
              instrument_id: 7,
              name: "CUPID LIMITED",
              candidates: [],
              issues: [],
              matched_via_alias: false,
            },
          ],
        })}
      />,
    );
    expect(screen.queryByTestId("import-problems")).toBeNull();
    expect(screen.getByText(/2 imported/)).toBeInTheDocument();
  });
});

describe("the buffer explanation", () => {
  it("states the bands with the user's own numbers", () => {
    render(<BufferExplainer topN={20} holdBuffer={10} />);
    const panel = screen.getByTestId("buffer-explainer");
    expect(within(panel).getByText(/ranked 1–20 in the screen/)).toBeInTheDocument();
    expect(within(panel).getByText(/21–30/)).toBeInTheDocument();
    expect(within(panel).getByText(/ranked 31 or worse/)).toBeInTheDocument();
  });

  it("repeats docs/01 §8's claim about turnover", () => {
    render(<BufferExplainer topN={20} holdBuffer={10} />);
    expect(screen.getByText(/materially reduces turnover/)).toBeInTheDocument();
  });
});
