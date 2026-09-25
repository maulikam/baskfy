import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CandidatesTable } from "@/components/overlap/candidates-table";
import {
  parseCandidates,
  type CorrectTagAction,
  type LabelRowAction,
  type OverlapCandidates,
} from "@/lib/overlap/candidates";
import type * as LiveMarksModule from "@/lib/screens/live-marks";
import { EMPTY_LIVE_MARKS } from "@/lib/screens/live-marks";

/* No live session in these tests: the price cell is the close, as the contract says. */
vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarksModule>();
  return { ...actual, useLiveMarks: () => EMPTY_LIVE_MARKS };
});

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("screen=exmpl0000001"),
}));

afterEach(cleanup);

/**
 * Two names on the wire, shaped as `GET /overlap` serves them — the canonical encoder emits a
 * Decimal as a JSON number, which is why `close` arrives as a number the schema types as a
 * string. `parseCandidates` is the one place that reconciles the two.
 */
const payload = parseCandidates({
  sessions: { swing: "2026-09-11", volume_breakout: "2026-09-10", three_weeks_tight: "2026-09-10" },
  scope: "actionable",
  strategies_read: true,
  screens_checked: 6,
  laya: { last_pass_at: "2026-09-25T06:12:31+05:30", answered: 1, shown: 1, of: 1 },
  data: [
    {
      instrument_id: 1,
      symbol: "BOTH",
      name: "BOTH LIMITED",
      close: 144.75 as unknown as string,
      last_price: null,
      strategy_count: 2,
      actionable: true,
      strategies: [
        {
          strategy: "swing",
          name: "Swing",
          ref: "/swing",
          as_of: "2026-09-11",
          detail: "EP · GAP_DAY",
          actionable: true,
        },
        {
          strategy: "three_weeks_tight",
          name: "Three weeks tight",
          ref: "/twt",
          as_of: "2026-09-10",
          detail: "tight 4 sessions · signal",
          actionable: true,
        },
      ],
      opinion: {
        label: "look_first",
        confidence: 0.82,
        source: "laya",
        shown: true,
        floor: 0.6,
        labelled: false,
        reason: null,
        laya: { label: "look_first", confidence: 0.82 },
      },
      screens: [
        {
          name: "Investing 001",
          public_id: "exmpl0000001",
          is_template: true,
          as_of: "2026-09-11",
          rank: 3,
          of: 40,
          definition_changed: false,
        },
      ],
      catalyst: {
        headline: "Press Release - BOTH wins a multi-year order",
        published_at: "2026-09-11T13:02:00+00:00",
        url: "https://nsearchives.nseindia.com/corporate/BOTH.pdf",
        earnings_date: "2026-09-15",
        tag: {
          event_type: "order",
          review_priority: "high",
          matched: [],
          source: "laya",
          confidence: 0.9834,
          disagrees_with: null,
          corrected: false,
        },
      },
    },
    {
      instrument_id: 2,
      symbol: "QUIET",
      name: "QUIET LIMITED",
      close: "149.60",
      last_price: null,
      strategy_count: 1,
      actionable: false,
      strategies: [
        {
          strategy: "three_weeks_tight",
          name: "Three weeks tight",
          ref: "/twt",
          as_of: "2026-09-10",
          detail: "tight 4 sessions",
          actionable: false,
        },
      ],
      screens: [],
      catalyst: null,
      /* Laya was unsure here, so the server served the rules baseline with its reason. */
      opinion: {
        label: "worth_a_look",
        confidence: 1,
        source: "rules",
        shown: true,
        floor: 0.6,
        labelled: false,
        reason: "a strategy could act; no filing on record",
        laya: { label: "skip", confidence: 0.34 },
      },
    },
  ],
});

describe("CandidatesTable", () => {
  it("restates each strategy's own word, the count, the close, the filing and the result date", () => {
    render(<CandidatesTable candidates={payload} scope="all" />);

    const rows = screen.getAllByTestId("overlap-candidate-row");
    expect(rows.map((row) => row.getAttribute("data-symbol"))).toEqual(["BOTH", "QUIET"]);
    expect(rows.map((row) => row.getAttribute("data-actionable"))).toEqual(["true", "false"]);

    const both = rows[0]!;
    expect(within(both).getByTestId("overlap-candidate-count")).toHaveTextContent("2");
    const chips = within(both).getAllByTestId("overlap-candidate-strategy");
    expect(chips.map((chip) => chip.textContent)).toEqual([
      "Swing· EP · GAP_DAY",
      "Three weeks tight· tight 4 sessions · signal",
    ]);
    expect(chips[0]).toHaveAttribute("href", "/swing");
    /* The close, at its stored precision — no live session was mocked, so no live cell. */
    expect(within(both).getByTestId("close-price")).toHaveTextContent("144.75");
    expect(within(both).queryByTestId("live-price")).toBeNull();
    expect(within(both).getByTestId("overlap-candidate-earnings")).toHaveTextContent("15 Sept 2026");

    /* The filing is a link out to the exchange's own copy, in a new tab — never the text. */
    const filing = within(both).getByTestId("overlap-candidate-filing");
    expect(filing).toHaveAttribute("href", "https://nsearchives.nseindia.com/corporate/BOTH.pdf");
    expect(filing).toHaveAttribute("target", "_blank");
    expect(filing).toHaveAttribute("rel", "noopener noreferrer");
    expect(filing).toHaveTextContent("BOTH wins a multi-year order");
    expect(within(both).getByTestId("overlap-candidate-screen")).toHaveTextContent(
      "Investing 001 #3 of 40",
    );
    /* The baseline's tag: the reader's word, the priority, the source and the why — as context. */
    const tag = within(both).getByTestId("overlap-candidate-tag");
    expect(tag).toHaveTextContent("Order win");
    expect(tag).toHaveAttribute("data-priority", "high");
    expect(tag).toHaveAttribute("data-source", "laya");
    expect(within(tag).getByTestId("overlap-candidate-tag-confidence")).toHaveTextContent("98%");
    expect(tag).toHaveAttribute("title", expect.stringContaining("Laya read the headline at 98%"));
    expect(tag).toHaveAttribute("title", expect.stringContaining("not used in any rank"));
    expect(screen.getByTestId("overlap-candidates-tag-note")).toHaveTextContent("by Laya");

    /* A name the feed does not cover has no result date and no filing — dashes, not blanks. */
    const quiet = rows[1]!;
    expect(within(quiet).queryByTestId("overlap-candidate-earnings")).toBeNull();
    expect(within(quiet).queryByTestId("overlap-candidate-filing")).toBeNull();
    expect(within(quiet).queryByTestId("overlap-candidate-tag")).toBeNull();
  });

  it("shows the rules tag without a percentage and marks a disagreement", () => {
    const disagreeing: OverlapCandidates = {
      ...payload,
      data: [
        {
          ...payload.data[0]!,
          catalyst: {
            ...payload.data[0]!.catalyst!,
            tag: {
              event_type: "governance",
              review_priority: "medium",
              matched: ["sebi order"],
              source: "rules",
              confidence: null,
              disagrees_with: "laya:corporate_action",
              corrected: false,
            },
          },
        },
      ],
    };
    render(<CandidatesTable candidates={disagreeing} scope="actionable" />);
    const tag = screen.getByTestId("overlap-candidate-tag");
    expect(tag).toHaveTextContent("Governance");
    expect(tag).toHaveAttribute("data-source", "rules");
    expect(screen.queryByTestId("overlap-candidate-tag-confidence")).toBeNull();
    expect(tag).toHaveAttribute("data-disagrees-with", "laya:corporate_action");
    expect(tag).toHaveAttribute("title", expect.stringContaining("matched: sebi order"));
    expect(tag).toHaveAttribute("title", expect.stringContaining("(laya) said corporate_action"));
    /* The rules stood because Laya was unsure: the guess is on hover, not a mark on the row. */
    expect(within(tag).queryByLabelText("the two readers disagree")).toBeNull();
  });

  it("offers the eight words and a clear option when it can write, and nothing when it cannot", () => {
    render(<CandidatesTable candidates={payload} scope="actionable" />);
    expect(screen.queryByTestId("overlap-candidate-tag-correct")).toBeNull();
    cleanup();

    const correctTag: CorrectTagAction = vi.fn(() => Promise.resolve({ ok: true as const }));
    render(<CandidatesTable candidates={payload} scope="actionable" correctTag={correctTag} />);
    const select = screen.getByTestId("overlap-candidate-tag-correct");
    expect(select).toHaveAttribute(
      "aria-label",
      "Correct the tag on “Press Release - BOTH wins a multi-year order”",
    );
    const options = within(select).getAllByRole("option");
    /* A placeholder, the eight words in the wire's order, and the clear option. */
    expect(options.map((option) => option.getAttribute("value"))).toEqual([
      "",
      "earnings",
      "order",
      "approval",
      "fundraising",
      "governance",
      "corporate_action",
      "routine",
      "other",
      "__clear",
    ]);
    expect(options.slice(1, 9).map((option) => option.textContent)).toEqual([
      "Results",
      "Order win",
      "Approval",
      "Fund raise",
      "Governance",
      "Corporate action",
      "Routine notice",
      "Unclear",
    ]);
    expect(within(select).getByRole("option", { name: "Clear correction" })).toBeDisabled();
    /* Laya's word stands: nothing is selected until a person chooses. */
    expect(select).toHaveValue("");
  });

  it("calls the action with the headline and the chosen word", async () => {
    const correctTag: CorrectTagAction = vi.fn(() => Promise.resolve({ ok: true as const }));
    render(<CandidatesTable candidates={payload} scope="actionable" correctTag={correctTag} />);
    fireEvent.change(screen.getByTestId("overlap-candidate-tag-correct"), {
      target: { value: "order" },
    });
    await waitFor(() =>
      expect(correctTag).toHaveBeenCalledWith("Press Release - BOTH wins a multi-year order", "order"),
    );
    expect(correctTag).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("overlap-candidate-tag-error")).toBeNull();
  });

  it("renders a corrected tag as the person's word, without a percentage, and can clear it", async () => {
    const corrected: OverlapCandidates = {
      ...payload,
      data: [
        {
          ...payload.data[0]!,
          catalyst: {
            ...payload.data[0]!.catalyst!,
            tag: {
              event_type: "governance",
              review_priority: "medium",
              matched: [],
              source: "corrected",
              confidence: null,
              disagrees_with: "laya:order",
              corrected: true,
            },
          },
        },
      ],
    };
    const correctTag: CorrectTagAction = vi.fn(() =>
      Promise.resolve({ ok: false as const, error: "The correction was not saved." }),
    );
    render(<CandidatesTable candidates={corrected} scope="actionable" correctTag={correctTag} />);
    const tag = screen.getByTestId("overlap-candidate-tag");
    expect(tag).toHaveAttribute("data-source", "corrected");
    expect(tag).toHaveAttribute("data-corrected", "true");
    expect(tag).toHaveTextContent("Governance");
    expect(within(tag).getByTestId("overlap-candidate-tag-source")).toHaveTextContent("corrected");
    expect(within(tag).queryByTestId("overlap-candidate-tag-confidence")).toBeNull();
    expect(tag).toHaveAttribute("title", expect.stringContaining("Corrected by you"));
    expect(tag).toHaveAttribute("title", expect.stringContaining("overrules laya, which said order"));

    const select = screen.getByTestId("overlap-candidate-tag-correct");
    expect(select).toHaveValue("governance");
    expect(within(select).getByRole("option", { name: "Clear correction" })).toBeEnabled();
    fireEvent.change(select, { target: { value: "__clear" } });
    await waitFor(() =>
      expect(correctTag).toHaveBeenCalledWith("Press Release - BOTH wins a multi-year order", null),
    );
    /* A refusal is said on the chip, in this app's words. */
    expect(await screen.findByTestId("overlap-candidate-tag-error")).toHaveTextContent(
      "The correction was not saved.",
    );
  });

  it("marks a disagreement only when a confident Laya overruled the rules", () => {
    const overruled: OverlapCandidates = {
      ...payload,
      data: [
        {
          ...payload.data[0]!,
          catalyst: {
            ...payload.data[0]!.catalyst!,
            tag: {
              event_type: "corporate_action",
              review_priority: "medium",
              matched: [],
              source: "laya",
              confidence: 0.91,
              disagrees_with: "rules:order",
              corrected: false,
            },
          },
        },
      ],
    };
    render(<CandidatesTable candidates={overruled} scope="actionable" />);
    const tag = screen.getByTestId("overlap-candidate-tag");
    expect(tag).toHaveAttribute("data-source", "laya");
    expect(within(tag).getByLabelText("the two readers disagree")).toHaveAttribute(
      "title",
      "The other reader (rules) said order.",
    );
  });

  it("shows Laya's word with its percentage when it was sure, the rules' word with its reason otherwise, and never a trade", () => {
    render(<CandidatesTable candidates={payload} scope="all" />);
    const [sure, rules] = screen.getAllByTestId("overlap-candidate-opinion");
    expect(sure).toHaveTextContent("Look first82%");
    expect(sure).toHaveAttribute("data-state", "shown");
    expect(sure).toHaveAttribute("data-source", "laya");
    expect(sure).toHaveAttribute("title", expect.stringContaining("never a trade"));
    /* The column always has a word: no "not sure", no "—", no percentage on the rules. */
    expect(rules).toHaveTextContent("Worth a lookrules");
    expect(rules).not.toHaveTextContent("%");
    expect(rules).toHaveAttribute("data-state", "shown");
    expect(rules).toHaveAttribute("data-source", "rules");
    expect(rules).toHaveAttribute(
      "title",
      expect.stringContaining("by the rules: a strategy could act; no filing on record"),
    );
    expect(rules).toHaveAttribute("title", expect.stringContaining("never a trade"));
    expect(screen.queryByText("not sure")).toBeNull();
    expect(screen.getByRole("columnheader", { name: "Laya" })).toBeInTheDocument();
    /* The model's own percentage is visible on the rules row, and not repeated on Laya's own. */
    const [asides] = screen.getAllByTestId("overlap-candidate-opinion-laya");
    expect(screen.getAllByTestId("overlap-candidate-opinion-laya")).toHaveLength(1);
    expect(asides).toHaveTextContent("Laya: Skip 34%");
    expect(asides).toHaveAttribute("title", expect.stringContaining("under its 60% floor"));
  });

  it("says why the rules said skip, and keeps the row labellable", () => {
    const skipped: OverlapCandidates = {
      ...payload,
      data: [
        {
          ...payload.data[1]!,
          opinion: {
            label: "skip",
            confidence: 1,
            source: "rules",
            shown: true,
            floor: 0.6,
            labelled: false,
            reason: "the filing is adverse; Swing gate shut",
            laya: null,
          },
        },
      ],
    };
    const labelRow: LabelRowAction = vi.fn(() => Promise.resolve({ ok: true as const }));
    render(<CandidatesTable candidates={skipped} scope="all" labelRow={labelRow} />);
    const opinion = screen.getByTestId("overlap-candidate-opinion");
    expect(opinion).toHaveTextContent("Skiprules");
    expect(opinion).toHaveAttribute("data-label", "skip");
    expect(opinion).toHaveAttribute(
      "title",
      expect.stringContaining("Skip by the rules: the filing is adverse; Swing gate shut"),
    );
    /* A person can still overrule the baseline — the select is empty, not the rules' word. */
    expect(screen.getByTestId("overlap-candidate-opinion-label")).toHaveValue("");
    /* No answer from the sidecar yet: said, not hidden. */
    expect(screen.getByTestId("overlap-candidate-opinion-laya")).toHaveTextContent("Laya: not read yet");
  });

  it("offers the three words and a clear option on the Laya cell when it can write, and nothing when it cannot", () => {
    render(<CandidatesTable candidates={payload} scope="all" />);
    expect(screen.queryByTestId("overlap-candidate-opinion-label")).toBeNull();
    cleanup();

    const labelRow: LabelRowAction = vi.fn(() => Promise.resolve({ ok: true as const }));
    render(<CandidatesTable candidates={payload} scope="all" labelRow={labelRow} />);
    /* Every row gets a select — including one the model has not answered or was unsure on. */
    const selects = screen.getAllByTestId("overlap-candidate-opinion-label");
    expect(selects).toHaveLength(2);
    const select = selects[0]!;
    expect(select).toHaveAttribute("aria-label", "Label BOTH: how much does this row deserve a look");
    const options = within(select).getAllByRole("option");
    /* A placeholder, the three words in the wire's order, and the clear option. */
    expect(options.map((option) => option.getAttribute("value"))).toEqual([
      "",
      "look_first",
      "worth_a_look",
      "skip",
      "__clear",
    ]);
    expect(options.slice(1, 4).map((option) => option.textContent)).toEqual([
      "Look first",
      "Worth a look",
      "Skip",
    ]);
    expect(within(select).getByRole("option", { name: "Clear label" })).toBeDisabled();
    /* Laya's word stands: nothing is selected until a person chooses. */
    expect(select).toHaveValue("");
    expect(within(select).getByRole("option", { name: "Label…" })).toBeDisabled();
  });

  it("calls the label action with the instrument and the chosen word", async () => {
    const labelRow: LabelRowAction = vi.fn(() => Promise.resolve({ ok: true as const }));
    render(<CandidatesTable candidates={payload} scope="all" labelRow={labelRow} />);
    const [, quiet] = screen.getAllByTestId("overlap-candidate-opinion-label");
    fireEvent.change(quiet!, { target: { value: "skip" } });
    await waitFor(() => expect(labelRow).toHaveBeenCalledWith(2, "skip"));
    expect(labelRow).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("overlap-candidate-opinion-error")).toBeNull();
  });

  it("renders a labelled opinion as the person's word, without a percentage, and can clear it", async () => {
    const labelled: OverlapCandidates = {
      ...payload,
      data: [
        {
          ...payload.data[0]!,
          opinion: {
            label: "skip",
            confidence: 0,
            source: "labelled",
            shown: true,
            floor: 0.6,
            labelled: true,
            reason: null,
            laya: { label: "look_first", confidence: 0.41 },
          },
        },
      ],
    };
    const labelRow: LabelRowAction = vi.fn(() =>
      Promise.resolve({ ok: false as const, error: "The label was not saved." }),
    );
    render(<CandidatesTable candidates={labelled} scope="actionable" labelRow={labelRow} />);
    const opinion = screen.getByTestId("overlap-candidate-opinion");
    expect(opinion).toHaveAttribute("data-source", "labelled");
    expect(opinion).toHaveAttribute("data-state", "shown");
    expect(opinion).toHaveAttribute("data-label", "skip");
    expect(opinion).toHaveTextContent("Skiplabelled");
    expect(opinion).not.toHaveTextContent("%");
    expect(within(opinion).getByTestId("overlap-candidate-opinion-source")).toHaveTextContent("labelled");
    expect(opinion).toHaveAttribute("title", expect.stringContaining("Labelled by you"));
    expect(opinion).toHaveAttribute("title", expect.stringContaining("never a trade"));
    /* What the model said stays visible beside the person's word — the disagreement is the point. */
    expect(screen.getByTestId("overlap-candidate-opinion-laya")).toHaveTextContent("Laya: Look first 41%");

    const select = screen.getByTestId("overlap-candidate-opinion-label");
    expect(select).toHaveValue("skip");
    expect(within(select).getByRole("option", { name: "Clear label" })).toBeEnabled();
    fireEvent.change(select, { target: { value: "__clear" } });
    await waitFor(() => expect(labelRow).toHaveBeenCalledWith(1, null));
    /* A refusal is said in the cell, in this app's words. */
    expect(await screen.findByTestId("overlap-candidate-opinion-error")).toHaveTextContent(
      "The label was not saved.",
    );
  });

  it("says whether Laya is working, in one line", () => {
    render(<CandidatesTable candidates={payload} scope="all" />);
    expect(screen.getByTestId("overlap-candidates-laya")).toHaveTextContent(
      /Laya read 1 of 1 filings \(last pass .*IST\) and is shown on every one\./,
    );
    cleanup();
    const never: OverlapCandidates = {
      ...payload,
      laya: { last_pass_at: null, answered: 0, shown: 0, of: 1 },
    };
    render(<CandidatesTable candidates={never} scope="all" />);
    expect(screen.getByTestId("overlap-candidates-laya")).toHaveTextContent(
      "Laya has not read these filings yet",
    );
  });

  it("names each sleeve's session and says when they differ", () => {
    render(<CandidatesTable candidates={payload} scope="actionable" />);
    const sessions = screen.getByTestId("overlap-candidates-sessions");
    expect(sessions).toHaveTextContent("Swing 11 Sept 2026");
    expect(sessions).toHaveTextContent("Three weeks tight 10 Sept 2026");
    expect(sessions).toHaveTextContent("not on the same session");
  });

  it("keeps the picked screens while switching scope", () => {
    render(<CandidatesTable candidates={payload} scope="actionable" />);
    expect(screen.getByTestId("overlap-candidates-scope-actionable")).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByTestId("overlap-candidates-scope-all")).toHaveAttribute(
      "href",
      "/build/overlap?screen=exmpl0000001&scope=all",
    );
    expect(screen.getByTestId("overlap-candidates-scope-actionable")).toHaveAttribute(
      "href",
      "/build/overlap?screen=exmpl0000001",
    );
  });

  it("distinguishes an unread API, another account, and an empty morning", () => {
    render(<CandidatesTable candidates={null} scope="actionable" />);
    expect(screen.getByTestId("overlap-candidates-unread")).toBeInTheDocument();
    cleanup();

    const notMine: OverlapCandidates = { ...payload, strategies_read: false, data: [] };
    render(<CandidatesTable candidates={notMine} scope="actionable" />);
    expect(screen.getByTestId("overlap-candidates-not-yours")).toBeInTheDocument();
    cleanup();

    const empty: OverlapCandidates = { ...payload, data: [] };
    render(<CandidatesTable candidates={empty} scope="actionable" />);
    expect(screen.getByTestId("overlap-candidates-empty")).toHaveTextContent(
      "Switch to every row",
    );
    expect(screen.queryByTestId("overlap-candidates-table")).toBeNull();
  });
});
