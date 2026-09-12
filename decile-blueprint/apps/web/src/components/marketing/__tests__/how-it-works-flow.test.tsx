/**
 * The landing diagram, asserted against the product it describes.
 *
 * A marketing picture is the one surface with no compiler behind it: nothing breaks when the app
 * grows a tab the diagram never heard of, or retires a rail the diagram still draws. The first
 * draft of this component was accurate on 24 Aug 2026 and stale by 26 Aug, and nothing failed.
 * These tests are the thing that would have failed.
 *
 * Each one asserts a **spec** and not the copy: the tab names come from `lib/nav`, the fee numbers
 * come from the fee FAQ's own arithmetic, and the broker claim is checked against the fact that
 * exactly one broker is wired. Rewording a sentence must not fail any of them; making one of them
 * false must.
 *
 * ## Rewritten 27 Aug 2026, because the spec changed
 *
 * House rule 2 is that tests assert the spec rather than current behaviour, which cuts both ways:
 * when the spec itself changes, the tests are rewritten to the new one rather than trimmed until
 * they pass. Two things changed (`FLOW-REFINE-PROMPT.md`, `gates/marketing-flow-responsive.md`):
 *
 * - **The cost stage is gone**, on Maulik's instruction, so `describe("the cost is on the stage")`
 *   is gone with it and its inverse took its place: no fee copy may appear on the stage at all.
 *   That reverses G1 of `gates/marketing-flow-refresh.md`; it is recorded in `DECISIONS-MERGE.md`.
 * - **The fixed 1360 x 420 canvas is gone**, and with it every geometry test that re-derived wire
 *   lengths against it. Those tests were guarding a class of bug — a connector drifting away from
 *   the card it points at — that the rebuild deletes rather than fixes: connectors are now grid
 *   cells with no coordinates of their own. What replaced those tests is the structural check
 *   that the canvas cannot come back (`responsive flow`) and the check that the animation is one
 *   schedule rather than eight (`choreographed cycle`).
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  CONNECTOR_BOX,
  ENGINE,
  FLOW_CYCLE_SECONDS,
  HowItWorksFlow,
  PULSE_DASH,
  SEGMENT_H,
  SEGMENT_V,
  SEGMENT_WINDOW,
  SWEEP_DASH,
  SWEEP_WINDOW,
  TIMELINE,
} from "@/components/marketing/how-it-works-flow";
import { SECTION_TABS } from "@/lib/nav";

/* ------------------------------------------------------------------ helpers */

const COMPONENT = readFileSync(
  resolve(process.cwd(), "src/components/marketing/how-it-works-flow.tsx"),
  "utf8",
);

const GLOBALS = readFileSync(resolve(process.cwd(), "src/app/globals.css"), "utf8");

const PAGE = readFileSync(resolve(process.cwd(), "src/app/(marketing)/page.tsx"), "utf8");

/**
 * The component with every comment removed.
 *
 * The structural checks below are greps, and this file's comments deliberately quote the very
 * strings those greps forbid — "`absolute`-positioned", "1360 x 420" — because deleting the record
 * of what went wrong is how it gets rebuilt. So the greps run against the code alone.
 */
const CODE = COMPONENT.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^[ \t]*\/\/.*$/gm, " ");

function stage(): HTMLElement {
  render(<HowItWorksFlow />);
  return screen.getByTestId("how-it-works-stage");
}

/** Everything a reader can read on the stage — the picture, not the numbered steps under it. */
function stageText(): string {
  return (stage().textContent ?? "").replace(/\s+/g, " ");
}

/** Every arbitrary Tailwind font size in the component, in px. */
function arbitraryFontSizes(): number[] {
  return [...CODE.matchAll(/text-\[([0-9.]+)px\]/g)].map((match) => Number(match[1]));
}

/* ------------------------------------------------------------- engine stages */

describe("the engine stages are the four that ship, and the fee is not one of them", () => {
  it("runs screen, then basket, then organize, then plan — and stops there", () => {
    // The order is the product: you rank before you build, you file before you review, and the
    // plan is the last thing that happens on our side of the broker.
    expect(ENGINE.map((entry) => entry.name)).toEqual(["screen", "basket", "organize", "plan"]);
  });

  it("has no cost stage, which is the instruction this rebuild carries", () => {
    // Maulik, 27 Aug 2026: the fee does not get a box on the stage. Reverses G1 of
    // `gates/marketing-flow-refresh.md` deliberately — see `docs/DECISIONS-MERGE.md`.
    expect(ENGINE.map((entry) => entry.name)).not.toContain("cost");
    render(<HowItWorksFlow />);
    expect(screen.queryByTestId("engine-cost")).toBeNull();
  });

  it("renders every stage it declares, and nothing it does not", () => {
    render(<HowItWorksFlow />);
    for (const entry of ENGINE) {
      const box = screen.getByTestId(`engine-${entry.name}`);
      expect(box.textContent ?? "").toContain(entry.body);
    }
    const engine = screen.getByTestId("how-it-works-engine");
    expect(engine.querySelectorAll('[data-testid^="engine-"]')).toHaveLength(ENGINE.length);
  });

  it("carries no fee copy anywhere on the stage — not a rate, not a cap, not GST", () => {
    // The fee is real and stays documented; what it may not do is claim a box in the pipeline.
    // A rupee sign, a percentage or the word GST reappearing here is the cost card creeping back.
    const text = stageText();
    expect(text).not.toMatch(/₹/);
    expect(text).not.toMatch(/\d\s?%/);
    expect(text).not.toMatch(/\bGST\b/i);
    expect(text).not.toMatch(/\bfees?\b/i);
    expect(text).not.toMatch(/brokerage|statutory/i);
  });

  it("dropped the attribution line that only existed to caveat the cost box", () => {
    render(<HowItWorksFlow />);
    expect(screen.queryByTestId("how-it-works-attribution")).toBeNull();
  });
});

/* ---------------------------------------------------------- broker honesty */

describe("broker honesty: none is presented as connected except the one that is", () => {
  /*
    `baskfy_core.broker_connections.BROKERS` carries ten entries. Exactly one — `zerodha` — is
    `holdings_sync="ready"` and `trading="ready"`; the other nine are `planned` or `partner`.
    `packages/core/tests/test_broker_capability_honesty.py` exists because a label has run ahead
    of its wiring before, and a marketing page is the easiest place for that to happen again.
  */
  const NOT_LIVE = [
    "HDFC",
    "Kotak",
    "ICICI",
    "Upstox",
    "Angel One",
    "Groww",
    "Fyers",
    "5paisa",
    "Dhan",
  ];

  it("names the live broker", () => {
    expect(stageText()).toContain("Zerodha");
  });

  it("names none of the nine that are not", () => {
    const text = stageText();
    for (const broker of NOT_LIVE) {
      expect(text).not.toContain(broker);
    }
  });

  it("counts the rest, and says in the same breath that they are not live", () => {
    const text = stageText();
    // The count is a roadmap a reader is entitled to; unqualified it reads as nine connections.
    expect(text).toMatch(/nine more brokers/i);
    expect(text).toMatch(/none live yet/i);
  });
});

/* --------------------------------------------------------- journey shipped */

describe("the journey shipped is the journey drawn", () => {
  it("names the Portfolio hub's real tabs, from lib/nav rather than from memory", () => {
    const text = stageText();
    for (const tab of SECTION_TABS.portfolio) {
      expect(text).toContain(tab.label);
    }
  });

  it("still counts the factors the registry actually names", () => {
    // `factor_registry.NAMED_FACTOR_COUNT == 64`. Spelled out, so a digit edit cannot slip past.
    expect(stageText()).toContain("Sixty-six factors");
  });

  it("starts from holdings, which is where the redesign put the front door", () => {
    const text = stageText();
    expect(text).toContain("Holdings");
    // The redesign's own step, missing from the first draft entirely.
    expect(ENGINE.map((entry) => entry.name)).toContain("organize");
    // §6.7's default state has a name, and hiding it here would misdescribe the first screen a
    // new user sees after connecting a broker.
    expect(text).toContain("Unallocated");
  });

  it("lands in portfolios, plural, and says whose demat they are", () => {
    const text = stageText();
    expect(text).toContain("Your portfolios");
    expect(text).toMatch(/your own demat/i);
    expect(text).toMatch(/nothing pooled/i);
  });

  it("still promises a plan that expires rather than one that fires", () => {
    // Non-negotiable 1: plans expire in 30 minutes and nothing auto-executes.
    expect(stageText()).toMatch(/expires in 30 minutes/i);
  });

  it("keeps the way back dashed — holdings are a fact you read, not a call we make", () => {
    render(<HowItWorksFlow />);
    const strip = screen.getByTestId("how-it-works-return");
    const track = strip.querySelector("path.flow-track[stroke-dasharray]");
    expect(track).not.toBeNull();
    expect(strip.textContent ?? "").toContain("holdings sync back");
  });
});

/* ---------------------------------------------------------- responsive flow */

describe("responsive flow: one layout that reflows, in normal document flow", () => {
  it("positions nothing absolutely, anywhere in the component", () => {
    // The root cause of every fault this rebuild answers. A card laid over a wire layer has
    // coordinates that must agree with copy, and copy always wins in the end.
    expect(CODE).not.toMatch(/\babsolute\b/);
    expect(CODE).not.toMatch(/\binset-0\b/);
    // And not through the back door either: a `style` prop can position just as well as a class.
    expect(CODE).not.toMatch(/position:\s*["']?(absolute|fixed)/);
  });

  it("has no fixed canvas and no horizontal scroll wrapper", () => {
    expect(CODE).not.toMatch(/w-\[\d{3,}px\]/);
    expect(CODE).not.toMatch(/h-\[\d{3,}px\]/);
    expect(CODE).not.toMatch(/overflow-x-auto/);
  });

  it("measures nothing at runtime — no observer, no client component, no layout read", () => {
    // The alternative to a fixed canvas is not a resize observer; it is not needing the numbers.
    expect(CODE).not.toMatch(/use client/);
    expect(CODE).not.toMatch(/ResizeObserver|getBoundingClientRect|useEffect|useRef/);
  });

  it("owns its type sizes rather than a token it could not override if it tried", () => {
    /*
      `.vaaya-eyebrow` hard-codes `--v-text-11` and sits in a `@layer utilities` block *after*
      `@import "tailwindcss"` in `globals.css` — same layer, same specificity, later in source, so
      it beats a `text-[12px]` written beside it on the element. The old stage's
      `vaaya-eyebrow text-[8px]` therefore never rendered at 8px, and reading the JSX could not
      tell you that. This stage's floor is 12px, and a floor that depends on cascade order is not
      a floor.
    */
    expect(CODE).not.toMatch(/vaaya-eyebrow/);
  });

  it("renders nothing below 12px at any breakpoint", () => {
    // 11px bodies and 8px eyebrows are what made the old stage unreadable on the phone this
    // rebuild exists for.
    const sizes = arbitraryFontSizes();
    expect(sizes.length).toBeGreaterThan(0);
    expect(sizes.filter((size) => size < 12)).toEqual([]);
  });

  it("steps its type with the breakpoint instead of freezing one size", () => {
    // "Responsive" is not only the layout: a 12px floor that never grows wastes a desktop.
    const stepped = [...CODE.matchAll(/sm:text-\[([0-9.]+)px\]/g)].map((m) => Number(m[1]));
    expect(stepped.length).toBeGreaterThanOrEqual(5);
    expect(stepped.filter((size) => size < 12)).toEqual([]);
  });

  it("puts the nodes in the DOM in the order the story is told", () => {
    // Which is also the order a screen reader and a phone get them in, because below `lg` the
    // grid is one column and DOM order *is* visual order.
    const nodes = Array.from(stage().querySelectorAll("[data-flow-node]")).map((node) =>
      node.getAttribute("data-flow-node"),
    );
    expect(nodes).toEqual(["you", "market", "engine", "rails", "portfolios"]);
  });

  it("draws one self-contained connector per gap, not one full-bleed overlay", () => {
    render(<HowItWorksFlow />);
    const connectors = screen.getAllByTestId("flow-connector");
    // You -> engine, market -> engine, engine -> rails, rails -> portfolios.
    expect(connectors).toHaveLength(4);
    for (const cell of connectors) {
      const svg = cell.querySelector("svg");
      // Square, so it scales uniformly and the arrowhead never skews.
      expect(svg?.getAttribute("viewBox")).toBe(`0 0 ${CONNECTOR_BOX} ${CONNECTOR_BOX}`);
    }
  });

  it("turns every connector vertical below lg, and gates the horizontal one behind it", () => {
    render(<HowItWorksFlow />);
    for (const cell of screen.getAllByTestId("flow-connector")) {
      const groups = Array.from(cell.querySelectorAll("g"));
      const vertical = groups.filter((group) =>
        group.querySelector(`path[d="${SEGMENT_V}"]`),
      );
      expect(vertical).toHaveLength(1);
      for (const group of groups) {
        if (group.querySelector(`path[d="${SEGMENT_H}"]`)) {
          // A horizontal arrow may only render from the breakpoint that gives it room.
          expect(group.getAttribute("class") ?? "").toContain("lg:");
        }
      }
    }
  });

  it("wraps the engine's stages rather than squeezing four across a narrow column", () => {
    // Two across until `xl`: at `lg` the middle column is ~390px, and four stages there would be
    // ~84px each — six characters to a line, which is a diagram that cannot be read.
    expect(CODE).toMatch(/grid-cols-2[^"]*xl:grid-cols-4/);
  });

  it("keeps every connector out of the accessibility tree", () => {
    const { container } = render(<HowItWorksFlow />);
    const svgs = Array.from(container.querySelectorAll("svg"));
    expect(svgs.length).toBeGreaterThan(0);
    for (const svg of svgs) {
      expect(svg.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("leaves every reader-facing word as real DOM text", () => {
    // The captions that used to be SVG `<text>` are HTML now; nothing a reader reads is drawn.
    const { container } = render(<HowItWorksFlow />);
    expect(container.querySelector("text")).toBeNull();
    expect(container.querySelector("tspan")).toBeNull();
    const text = (screen.getByTestId("how-it-works-stage").textContent ?? "").replace(/\s+/g, " ");
    for (const caption of ["holdings", "feeds the ranking", "you confirm at your broker"]) {
      expect(text).toContain(caption);
    }
  });
});

/* -------------------------------------------------------- choreographed cycle */

describe("choreographed cycle: one story, not eight specks", () => {
  /** The schedule, flattened into the order the eye is meant to follow. */
  const BEATS: readonly (readonly [string, number])[] = [
    ["you", TIMELINE.you],
    ["you -> engine", TIMELINE.youToEngine],
    ["market", TIMELINE.market],
    ["market -> engine", TIMELINE.marketToEngine],
    ...TIMELINE.stages.map((delay, index) => [`stage ${index}`, delay] as const),
    ["engine -> rails", TIMELINE.engineToRails],
    ...TIMELINE.rails.map((delay, index) => [`rail ${index}`, delay] as const),
    ["rails -> portfolios", TIMELINE.railsToPortfolios],
    ["portfolios", TIMELINE.portfolios],
    ["return sweep", TIMELINE.returnSweep],
  ];

  it("fires its beats in narrative order, each strictly after the last", () => {
    // This is the whole difference between a story and eight dashes on arbitrary offsets.
    for (let index = 1; index < BEATS.length; index += 1) {
      const previous = BEATS[index - 1];
      const current = BEATS[index];
      if (previous === undefined || current === undefined) throw new Error("beat missing");
      expect({ beat: current[0], after: previous[0], ok: current[1] > previous[1] }).toEqual({
        beat: current[0],
        after: previous[0],
        ok: true,
      });
    }
  });

  it("acknowledges one stage per engine stage and one rail per rail", () => {
    expect(TIMELINE.stages).toHaveLength(ENGINE.length);
    expect(TIMELINE.rails).toHaveLength(3);
  });

  it("finishes inside its own cycle, with a beat of rest before it repeats", () => {
    // A beat that ran past the loop point would collide with the next cycle's opening move, which
    // is how a schedule stops reading as a sequence.
    const lastSegment = Math.max(
      TIMELINE.youToEngine,
      TIMELINE.marketToEngine,
      TIMELINE.engineToRails,
      TIMELINE.railsToPortfolios,
    );
    expect(lastSegment + SEGMENT_WINDOW * FLOW_CYCLE_SECONDS).toBeLessThan(FLOW_CYCLE_SECONDS);
    const sweepEnds = TIMELINE.returnSweep + SWEEP_WINDOW * FLOW_CYCLE_SECONDS;
    expect(sweepEnds).toBeLessThan(FLOW_CYCLE_SECONDS);
    // The rest is deliberate: a loop with no pause reads as a machine, not as a story.
    expect(FLOW_CYCLE_SECONDS - sweepEnds).toBeGreaterThanOrEqual(0.3);
  });

  it("gives every animated element the same period, so the phases cannot drift", () => {
    /*
      The old version's central bug. `animation-delay` offsets only the *first* iteration, so
      wires with different durations looped forever end up in permanently arbitrary relative
      phase. Equal durations turn the delays into a fixed relationship instead — which means no
      element may carry its own inline duration; the one in `globals.css` has to be the only one.
    */
    const { container } = render(<HowItWorksFlow />);
    const animated = Array.from(container.querySelectorAll(".flow-pulse, .flow-sweep, .flow-ack"));
    expect(animated.length).toBeGreaterThanOrEqual(12);
    for (const element of animated) {
      const style = element.getAttribute("style") ?? "";
      expect(style).toMatch(/animation-delay/);
      expect(style).not.toMatch(/animation-duration/);
    }
  });

  it("declares that one period in globals.css, at the number the component believes", () => {
    expect(GLOBALS).toContain(`--flow-cycle: ${FLOW_CYCLE_SECONDS}s`);
    // And the windows the schedule is proved against are the keyframes' own percentages.
    expect(GLOBALS).toMatch(
      new RegExp(`@keyframes flow-segment[\\s\\S]*?${SEGMENT_WINDOW * 100}% \\{`),
    );
    expect(GLOBALS).toMatch(new RegExp(`@keyframes flow-sweep[\\s\\S]*?${SWEEP_WINDOW * 100}% \\{`));
  });

  it("draws one dot per pulse, in the normalised scale every pulse path shares", () => {
    // `pathLength="100"` means a dash is a percentage of its own path. Per-gap connectors are all
    // the same length, so one constant is right on all of them — the reason the old file's
    // per-wire dash arithmetic is gone rather than merely moved.
    render(<HowItWorksFlow />);
    for (const cell of screen.getAllByTestId("flow-connector")) {
      for (const pulse of Array.from(cell.querySelectorAll("path.flow-pulse"))) {
        expect(pulse.getAttribute("pathLength")).toBe("100");
        expect(pulse.getAttribute("stroke-dasharray")).toBe(`${PULSE_DASH} ${100 - PULSE_DASH}`);
      }
    }
    const sweep = screen
      .getByTestId("how-it-works-return")
      .querySelector("path.flow-sweep");
    expect(sweep?.getAttribute("stroke-dasharray")).toBe(`${SWEEP_DASH} ${100 - SWEEP_DASH}`);
    // A sweep is meant to read as longer than a hop; if it stopped being so, it is a hop.
    expect(SWEEP_WINDOW).toBeGreaterThan(SEGMENT_WINDOW);
  });

  it("removes every pulse and every acknowledgment under prefers-reduced-motion", () => {
    // Not shortened to 0.01 ms by `@layer base`'s blanket rule, which would *finish* them and
    // park a dot at the end of each wire. `globals.css` says so unlayered, on purpose.
    const reduced = GLOBALS.slice(GLOBALS.lastIndexOf("@media (prefers-reduced-motion: reduce)"));
    const block = GLOBALS.slice(GLOBALS.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(block).toMatch(/\.flow-pulse,\s*\n?\s*\.flow-sweep \{\s*\n?\s*display: none/);
    expect(block).toMatch(/\.flow-ack \{[\s\S]*?animation: none/);
    expect(reduced.length).toBeGreaterThan(0);
  });
});

/* ---------------------------------------------------------- the reading order */

describe("the whole journey survives in reading order", () => {
  it("numbers its steps contiguously from one", () => {
    render(<HowItWorksFlow />);
    const items = within(screen.getByTestId("how-it-works-steps")).getAllByRole("listitem");
    expect(items.length).toBeGreaterThanOrEqual(5);
    items.forEach((item, index) => {
      expect(item.textContent ?? "").toContain(`${index + 1} · `);
    });
  });

  it("carries the steps the stage carries, connect and confirm included", () => {
    render(<HowItWorksFlow />);
    const text = (screen.getByTestId("how-it-works-steps").textContent ?? "").replace(/\s+/g, " ");
    for (const beat of ["Connect", "Screen", "Build", "Organize", "Confirm"]) {
      expect(text).toContain(beat);
    }
    expect(text).toMatch(/read-only order plan/i);
  });

  it("is where the fee lives now that it is off the stage", () => {
    // `components/investments/fee-faq.tsx`: base = min(Rs 100, 1.5% x amount), then 18% GST, and
    // rebalance, exit, partial exit and customize accrue nothing. All three numbers, because a
    // sentence that dropped the cap would overstate a large buy and one that dropped the rate
    // would understate a small one. Removing the cost *box* must not remove the cost.
    render(<HowItWorksFlow />);
    const text = (screen.getByTestId("how-it-works-steps").textContent ?? "").replace(/\s+/g, " ");
    expect(text).toMatch(/1\.5\s?%/);
    expect(text).toContain("₹100");
    expect(text).toMatch(/GST/);
    expect(text).toMatch(/rebalance/i);
    expect(text).toMatch(/nothing|zero|no fee/i);
  });
});

/* ------------------------------------------------------------ section blurb */

describe("the section blurb promises only what the stage draws", () => {
  it("no longer heads the section with a cost the picture does not show", () => {
    // The cost card left on Maulik's instruction; a heading that outlived it would be the page
    // making a claim its own illustration refutes.
    expect(PAGE).not.toContain("From intent to result, with the cost visible before it runs.");
    expect(PAGE).toContain("From intent to result — nothing runs until you confirm.");
  });

  it("no longer says statutory charges are on screen here", () => {
    // They are not: grep `curated_plans.py` and the investment routers for a cost field and you
    // get a comment. `/portfolio/[id]/costs` is accrued platform fees, not statutory charges.
    expect(PAGE).not.toMatch(/cost you in brokerage and statutory charges is on\s*\n?\s*screen/);
    expect(PAGE).toMatch(/statutory charges are your broker/);
  });

  it("still says nothing on the site executes anything", () => {
    // The product's central claim, and non-negotiable 1 in prose form.
    expect(PAGE).toContain("Nothing on this site executes anything");
  });

  it("keeps the fee stated where the stage no longer states it", () => {
    expect(PAGE).toMatch(/1\.5% of a buy/);
    expect(PAGE).toMatch(/GST/);
  });
});
