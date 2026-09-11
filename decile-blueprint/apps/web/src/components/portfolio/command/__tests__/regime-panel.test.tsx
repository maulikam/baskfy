import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { RegimePanel } from "@/components/portfolio/command/regime-panel";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { RegimeOut } from "@/lib/portfolio/regime";

/**
 * The regime panel as a person meets it.
 *
 * Against the brief's sentences rather than against the markup, because the markup is the part
 * that is allowed to change. The five that would each be a real defect in a product that places
 * live orders:
 *
 *   · a bare "—" reaching the screen with nothing to explain it;
 *   · R2 read as "out of the market", when it is a lower cap with entries usually still open;
 *   · an old tier presented as the one in force;
 *   · a figure appearing for something `RegimeOut` does not carry;
 *   · a sentence a reader could act on as advice, which Baskfy is not registered to give.
 */

/* Schema-exact, no `as`. `RegimeOut` is generated from the API's own OpenAPI document. */
function payload(over: Partial<RegimeOut> = {}): RegimeOut {
  return {
    evaluated_at: "2026-09-04T18:30:00+05:30",
    signal_date: "2026-09-04",
    tier: "R2",
    previous_tier: "R1",
    new_buys: "half",
    mode: "observe",
    breadth_pct: 38.4,
    actual_equity_pct: 78.0,
    target_equity_cap_pct: 70.0,
    reasons: [
      "Breadth below the R3 threshold.",
      "Risk reduction applied directly to the candidate tier.",
    ],
    data_stale: false,
    manual_action_required: false,
    next_evaluation_date: "2026-09-11",
    ...over,
  };
}

function renderPanel(over: Partial<RegimeOut> = {}, today: string | null = "2026-09-07") {
  return render(
    <TooltipProvider>
      <RegimePanel regime={payload(over)} today={today} />
    </TooltipProvider>,
  );
}

function panelText(): string {
  return screen.getByTestId("regime-panel").textContent ?? "";
}

/* ------------------------------------------------------------------------------- the gap */

describe("the exposure gap on screen", () => {
  it("gap: writes the brief's sentence from the two real fields", () => {
    renderPanel({ actual_equity_pct: 78, target_equity_cap_pct: 70 });

    const gap = screen.getByTestId("regime-gap");
    expect(gap).toHaveAttribute("data-direction", "above");
    expect(gap.textContent).toMatch(/8\.0/);
    expect(gap.textContent).toMatch(/Current exposure is 8\.0 percentage points above target\./);
  });

  it("gap: names its direction in words, never by colour alone", () => {
    renderPanel({ actual_equity_pct: 31.8, target_equity_cap_pct: 40 });

    /* A reader who sees no colour — greyscale print, a screenshot, a colour-vision difference —
       still gets the direction, because it is spelled out beside the figure. */
    expect(screen.getByTestId("regime-gap").textContent).toMatch(/below target/);
  });

  it("gap: with a missing cap it renders the reason, never a dash", () => {
    renderPanel({ target_equity_cap_pct: null });

    const gap = screen.getByTestId("regime-gap");
    expect(gap).toHaveAttribute("data-direction", "unavailable");
    expect(gap.textContent).toMatch(/no gap to state/);
    expect(gap.textContent).not.toMatch(/—/);
  });

  it("gap: the meter is drawn only when both of its numbers are real", () => {
    const both = renderPanel({ actual_equity_pct: 78, target_equity_cap_pct: 70 });
    expect(screen.getByTestId("regime-meter")).toBeInTheDocument();
    both.unmount();

    /* A bar with one end invented is exactly the defect the plan's §1 forbids. */
    renderPanel({ actual_equity_pct: null }).unmount();
    expect(screen.queryByTestId("regime-meter")).not.toBeInTheDocument();

    renderPanel({ target_equity_cap_pct: null });
    expect(screen.queryByTestId("regime-meter")).not.toBeInTheDocument();
  });

  it("gap: the meter says in words what the bar says in pixels", () => {
    renderPanel({ actual_equity_pct: 78, target_equity_cap_pct: 70 });

    const meter = within(screen.getByTestId("regime-meter")).getByRole("img");
    expect(meter).toHaveAccessibleName(
      "78.0% of capital in shares against a cap of 70.0%. Current exposure is 8.0 percentage points above target.",
    );
  });
});

/* ---------------------------------------------------------------------------------- R2 */

describe("how R2 reads", () => {
  it("R2 is shown as reduced exposure and never as an exit", () => {
    renderPanel({ tier: "R2", new_buys: "half" });

    const text = panelText();
    expect(text).toMatch(/Cautious/);
    expect(text).toMatch(/Reduced exposure, not an exit/);
    expect(text).not.toMatch(/out of the market|fully in cash|sell everything|liquidated/i);
  });

  it("R2 with entries vetoed is a blocked book, not a sold one", () => {
    renderPanel({
      tier: "R2",
      new_buys: "blocked",
      reasons: ["Momentum sentinel below its 50-DMA — new entries vetoed."],
    });

    expect(screen.getByTestId("regime-new-buys").textContent).toBe("Blocked");
    expect(panelText()).toMatch(/Positions already open are managed under their own stops/);
    expect(panelText()).not.toMatch(/out of the market|fully in cash/i);
    expect(screen.getByTestId("regime-sentinel-veto-50dma")).toHaveAttribute(
      "data-state",
      "in-force",
    );
  });

  it("R2 shows half-sized entries when the evaluation recorded them", () => {
    renderPanel({ tier: "R2", new_buys: "half" });

    expect(screen.getByTestId("regime-new-buys").textContent).toBe("Half size");
    expect(panelText()).toMatch(/smaller entry, not a stop on entries/);
  });

  it("R2 reached from R1 says risk was reduced", () => {
    renderPanel({ tier: "R2", previous_tier: "R1" });

    expect(screen.getByTestId("regime-movement").textContent).toBe("Risk reduced to R2 from R1.");
    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-tier", "R2");
  });
});

/* ------------------------------------------------------- what the response does not carry */

describe("figures declared unavailable", () => {
  it("unavailable: every one the response omits is named with its reason", async () => {
    renderPanel();

    await userEvent.click(screen.getByTestId("regime-disclosure-unavailable"));
    const list = screen.getByTestId("regime-unavailable");

    for (const id of [
      "candidate-tier",
      "index-dma",
      "confirmation-progress",
      "breadth-coverage",
      "pending-execution",
      "algorithm-version",
    ]) {
      expect(within(list).getByTestId(`regime-unavailable-${id}`)).toBeInTheDocument();
    }
    expect(list.textContent).toMatch(/raw_candidate_tier/);
    expect(list.textContent).toMatch(/index_diagnostics/);
    expect(list.textContent).toMatch(/algorithm_version and config_hash/);
  });

  it("unavailable: each one says what would surface it, so it is a gap and not a refusal", async () => {
    renderPanel();

    await userEvent.click(screen.getByTestId("regime-disclosure-unavailable"));
    const items = within(screen.getByTestId("regime-unavailable")).getAllByRole("listitem");

    expect(items).toHaveLength(6);
    for (const item of items) expect(item.textContent).toMatch(/Would need: /);
  });

  it("unavailable: no DMA distance, candidate tier or config hash is ever printed as a figure", async () => {
    renderPanel();
    await userEvent.click(screen.getByTestId("regime-disclosure-unavailable"));

    /* If one of these ever acquired a value, it would be a number a person could size a position
       against and nothing behind it. The panel may name them; it may not quote them. */
    const text = panelText();
    expect(text).not.toMatch(/\bcandidate tier is\b/i);
    expect(text).not.toMatch(/\d+(\.\d+)?% (above|below) (its )?(20|50|200)-DMA/i);
    expect(text).not.toMatch(/config hash [0-9a-f]{6,}/i);
  });

  it("unavailable: a missing breadth reading renders its reason in place of the figure", () => {
    renderPanel({ breadth_pct: null });

    const cell = screen.getByTestId("regime-breadth");
    expect(cell).toHaveAttribute("data-available", "false");
    expect(cell.textContent).toMatch(/No breadth reading was supplied to this evaluation/);
    /* The reason stands WHERE the figure would: no dash, and no number to mistake for one. */
    expect(cell.textContent).not.toMatch(/[—–-]/);
    expect(cell.textContent).not.toMatch(/\d/);
  });

  it("unavailable: breadth carries its missing coverage beside the reading, not only in a drawer", () => {
    renderPanel({ breadth_pct: 38.4 });

    /* Breadth is only usable when it covered enough of the universe. The coverage is not in the
       response, and a reader meets that caveat where they meet the number. */
    const cell = screen.getByTestId("regime-breadth").parentElement;
    expect(cell?.textContent).toMatch(/38\.4%/);
    expect(cell?.textContent).toMatch(/breadth_coverage_pct/);
  });

  it("unavailable: no element anywhere on the panel is a bare dash", () => {
    /* The brief's hard rule, checked over the whole rendered tree rather than one cell — em
       dashes inside a sentence are prose, an element whose entire content is a dash is the
       defect. A missing figure must carry its explanation or not render. */
    renderPanel({
      breadth_pct: null,
      actual_equity_pct: null,
      target_equity_cap_pct: null,
      signal_date: null,
      next_evaluation_date: null,
      new_buys: null,
      mode: null,
    });

    const bare = /^\s*(—|–|-|N\/A|n\/a|null|undefined)\s*$/;
    for (const node of screen.getByTestId("regime-panel").querySelectorAll("*")) {
      expect(node.textContent ?? "").not.toMatch(bare);
    }
  });
});

/* --------------------------------------------------------------------- the ladder in context */

describe("the configured ladder", () => {
  it("is labelled as the desk's defaults and cites REGIME_R4_EQUITY_PCT", async () => {
    renderPanel({ target_equity_cap_pct: 55 });

    await userEvent.click(screen.getByTestId("regime-disclosure-ladder"));
    const table = screen.getByTestId("regime-ladder");

    expect(table.textContent).toMatch(/REGIME_R4_EQUITY_PCT/);
    expect(panelText()).toMatch(/configured defaults, not this evaluation's figures/);
    /* The cap in force is the evaluation's 55, not the ladder's 70. */
    expect(panelText()).toMatch(/55\.0%/);
  });
});

/* ------------------------------------------------------------------- staleness and failure */

describe("when the stance may not be the one in force", () => {
  it("stale: a stale evaluation changes the heading, not just adds a chip", () => {
    renderPanel({ data_stale: true });

    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-current", "false");
    expect(panelText()).toMatch(/Last recorded stance — not confirmed current/);
    expect(panelText()).not.toMatch(/Stance in force/);
  });

  it("stale: the stale notice explains itself and gives exactly one next step", () => {
    renderPanel({ data_stale: true });

    const notice = screen.getByTestId("regime-notice-data-stale");
    expect(notice.textContent).toMatch(/Critical/);
    expect(notice.textContent).toMatch(/holds the previous tier and blocks new buys/);
    expect(within(notice).getAllByText(/Next step:/)).toHaveLength(1);
  });

  it("stale: a manual-action flag gets its own explanation and next step", () => {
    renderPanel({ manual_action_required: true });

    const notice = screen.getByTestId("regime-notice-manual-action");
    expect(notice.textContent).toMatch(/flagged for a person to look at/);
    expect(notice.textContent).toMatch(/Next step: Open the desk console/);
  });

  it("stale: an overdue weekly evaluation is named with the date it was due", () => {
    renderPanel({ next_evaluation_date: "2026-09-11" }, "2026-09-14");

    expect(screen.getByTestId("regime-notice-overdue").textContent).toMatch(/due on 2026-09-11/);
    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-current", "false");
  });

  it("stale: a current evaluation says so and raises no notice", () => {
    renderPanel({ next_evaluation_date: "2026-09-11" }, "2026-09-07");

    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-current", "true");
    expect(panelText()).toMatch(/Stance in force/);
    expect(screen.queryByTestId("regime-notices")).not.toBeInTheDocument();
  });

  it("stale: an unreachable desk shows no tier at all, only what happened", () => {
    render(
      <TooltipProvider>
        <RegimePanel
          regime={null}
          today="2026-09-07"
          unavailableReason="/desk/regime timed out after 4000ms."
        />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-state", "unavailable");
    expect(panelText()).toMatch(/timed out after 4000ms/);
    expect(panelText()).toMatch(/Next step:/);
    /* A tier from a market the screen cannot currently see is worse than no tier. */
    expect(panelText()).not.toMatch(/\bR[1-4]\b/);
  });
});

/* ----------------------------------------------------------- the desk's words, and only those */

describe("the reasons", () => {
  it("prints the desk's sentences unchanged, under a heading that says whose they are", () => {
    const reasons = [
      "Breadth below the R3 threshold.",
      "Momentum sentinel confirmed below its 200-DMA.",
    ];
    renderPanel({ reasons });

    const list = screen.getByTestId("regime-reasons");
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent?.trim())).toEqual([
      "·Breadth below the R3 threshold.",
      "·Momentum sentinel confirmed below its 200-DMA.",
    ]);
    expect(panelText()).toMatch(/The sentences below are the desk’s own/);
  });

  it("says so when the evaluation recorded none, rather than showing an empty list", () => {
    renderPanel({ reasons: [] });

    expect(screen.getByTestId("regime-reasons").textContent).toMatch(/recorded no reasons/);
  });

  it("shows the sentinel as not stated when the desk said nothing about it", () => {
    renderPanel({ reasons: ["No risk-off rule fired and R1 conditions were not all met."] });

    const veto = screen.getByTestId("regime-sentinel-veto-50dma");
    expect(veto).toHaveAttribute("data-state", "not-stated");
    expect(veto.textContent).toMatch(/Not stated/);
    expect(veto.textContent).not.toMatch(/Reported clear/);
  });
});

/* ------------------------------------------------------------- when it was worked out */

describe("the evaluation dates", () => {
  it("shows when it was evaluated, from which session, and when the next is due", () => {
    renderPanel({ evaluated_at: "2026-09-04T18:30:00+05:30", signal_date: "2026-09-04" });

    expect(screen.getByTestId("regime-evaluated").textContent).toMatch(/2026-09-04/);
    expect(screen.getByTestId("regime-signal-date").textContent).toMatch(/2026-09-04/);
    expect(screen.getByTestId("regime-next-evaluation").textContent).toMatch(/2026-09-11/);
  });

  it("stale: an undated next evaluation says so rather than disappearing", () => {
    renderPanel({ next_evaluation_date: null });

    const cell = screen.getByTestId("regime-next-evaluation");
    expect(cell).toHaveAttribute("data-available", "false");
    expect(cell.textContent).toMatch(/did not record when the next one is due/);
  });

  it("stale: a tier outside R1–R4 is printed as written and explains the changed heading", () => {
    renderPanel({ tier: "R5" });

    /* The heading drops to "not confirmed current" for an unrecognised tier. A heading that
       changes with nothing to explain it is its own defect. */
    expect(screen.getByTestId("regime-panel")).toHaveAttribute("data-current", "false");
    expect(screen.getByTestId("regime-notice-tier-unrecognised").textContent).toMatch(
      /which this screen does not know/,
    );
    expect(panelText()).toMatch(/R5/);
  });
});

/* ------------------------------------------------------------------------------- G7, advice */

describe("what the panel may not say", () => {
  it("advice: nothing on the panel is phrased as investment advice", () => {
    renderPanel({
      tier: "R3",
      previous_tier: "R2",
      new_buys: "blocked",
      actual_equity_pct: 62,
      target_equity_cap_pct: 40,
      reasons: [
        "Momentum sentinel confirmed below its 200-DMA.",
        "New entries blocked by the momentum sentinel veto.",
      ],
    });

    /* A blunt /\bbuy\b/ scan would be the wrong guard here and would fail on the desk's own
       recorded sentence ("new buys", "new entries"). The rule is not that the words are banned —
       it is that the panel must never ADDRESS THE READER about their own money. So the scan is
       for second-person direction and for recommendation vocabulary. D3; §6.2 rule 4. */
    const text = panelText();
    expect(text).not.toMatch(/\byou (should|must|can|may|need to|ought)\b/i);
    expect(text).not.toMatch(/\b(we|baskfy) (recommend|advise|suggest)\b/i);
    expect(text).not.toMatch(/\bconsider (buying|selling|reducing|trimming|adding)\b/i);
    expect(text).not.toMatch(/\byour (position|holding|portfolio|exposure|book)\b/i);
    expect(text).not.toMatch(/\b(buy|sell|exit|trim) (now|today|immediately)\b/i);
    expect(text).not.toMatch(/\b(guaranteed|sure[- ]shot|multibagger|hot stock|target price)\b/i);
  });

  it("advice: says out loud that it is a record of someone else's decision", () => {
    renderPanel();

    expect(panelText()).toMatch(
      /A record of what the desk decided for its own portfolio, not a recommendation about yours\./,
    );
  });

  it("advice: the panel offers no action that could reach an order", () => {
    renderPanel();

    /* Non-negotiable #1. The only link is to the desk's own read-only stance page. */
    const links = within(screen.getByTestId("regime-panel")).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/regime"]);
    expect(within(screen.getByTestId("regime-panel")).queryAllByRole("button")).toHaveLength(2);
  });

  it("advice: says whether the stance is being acted on at all", () => {
    renderPanel({ mode: "observe" });

    /* In `observe` the desk forces no selling, and a tier shown without that reads as though the
       book had already been moved to the cap. */
    expect(screen.getByTestId("regime-mode").textContent).toBe("Observing");
    expect(panelText()).toMatch(/acts on none of it/);
  });
});
