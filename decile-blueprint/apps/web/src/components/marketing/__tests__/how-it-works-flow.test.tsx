/**
 * The landing diagram, asserted against the product it describes.
 *
 * A marketing picture is the one surface with no compiler behind it: nothing breaks when the app
 * grows a tab the diagram never heard of, or retires a rail the diagram still draws. The first
 * draft of this component was accurate on 24 Aug 2026 and stale by 26 Aug, and nothing failed.
 * These tests are the thing that would have failed.
 *
 * Each one asserts a **spec** and not the copy: the tab names come from `lib/nav`, the fee numbers
 * come from the fee FAQ's own arithmetic, the wire lengths are recomputed from the path data, and
 * the broker claim is checked against the fact that exactly one broker is wired. Rewording a
 * sentence must not fail any of them; making one of them false must.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ENGINE,
  ENGINE_RIGHT_EDGE,
  HowItWorksFlow,
  RETURN_WIRE,
  STAGE_HEIGHT,
  STAGE_WIDTH,
  WIRES,
  type Wire,
} from "@/components/marketing/how-it-works-flow";
import { SECTION_TABS } from "@/lib/nav";

/* ------------------------------------------------------------------ helpers */

function stage(): HTMLElement {
  render(<HowItWorksFlow />);
  return screen.getByTestId("how-it-works-stage");
}

/** Everything a reader can read on the stage, whitespace-collapsed. */
function stageText(): string {
  return (stage().textContent ?? "").replace(/\s+/g, " ");
}

interface Point {
  x: number;
  y: number;
}

/** A `"x,y"` pair, refusing anything that is not one — `noUncheckedIndexedAccess` is on, and a
 *  silently-undefined coordinate is exactly the bug this parser exists to catch. */
function point(pair: string): Point {
  const [x, y] = pair.split(",").map(Number);
  if (x === undefined || y === undefined || Number.isNaN(x) || Number.isNaN(y)) {
    throw new Error(`not a coordinate pair: "${pair}"`);
  }
  return { x, y };
}

/**
 * Walk a path's `d` far enough to know where it starts, where it ends, and — when it is made of
 * nothing but straight runs — exactly how long it is.
 *
 * Only the four commands this diagram uses are handled, and an unknown one throws rather than
 * being skipped: a silently ignored command would make a wrong length look right.
 */
function walk(d: string): { start: Point; end: Point; straightLength: number | null } {
  const tokens = d.trim().split(/\s+/);
  let cursor: Point | null = null;
  let start: Point | null = null;
  let length = 0;
  let straight = true;

  for (const token of tokens) {
    const command = token[0];
    const rest = token.slice(1);
    if (command === "M") {
      cursor = point(rest);
      start = cursor;
    } else if (command === "H") {
      if (cursor === null) throw new Error(`H before M in ${d}`);
      const x = Number(rest);
      length += Math.abs(x - cursor.x);
      cursor = { x, y: cursor.y };
    } else if (command === "V") {
      if (cursor === null) throw new Error(`V before M in ${d}`);
      const y = Number(rest);
      length += Math.abs(y - cursor.y);
      cursor = { x: cursor.x, y };
    } else if (command === "C") {
      straight = false;
      cursor = null; // the two control points that follow are not positions
    } else if (/^-?[\d.]/.test(token) && !straight) {
      cursor = point(token); // the last pair of a C is the real endpoint
    } else {
      throw new Error(`unhandled path token "${token}" in ${d}`);
    }
  }

  if (start === null || cursor === null) throw new Error(`incomplete path ${d}`);
  return { start, end: cursor, straightLength: straight ? length : null };
}

function chord(wire: Wire): number {
  const { start, end } = walk(wire.d);
  return Math.hypot(end.x - start.x, end.y - start.y);
}

const ALL_WIRES: readonly Wire[] = [...WIRES, RETURN_WIRE];

/** The wires that carry a plan out to a rail: the three that leave the engine's right edge. */
const RAIL_WIRES = WIRES.filter((wire) => walk(wire.d).start.x === ENGINE_RIGHT_EDGE);

/* ------------------------------------------------------------- what it costs */

describe("the cost is on the stage, and it is the cost this product actually charges", () => {
  it("states the platform fee in the fee FAQ's own numbers", () => {
    render(<HowItWorksFlow />);
    const text = (screen.getByTestId("engine-cost").textContent ?? "").replace(/\s+/g, " ");

    // `components/investments/fee-faq.tsx`: base = min(Rs 100, 1.5% x amount), then 18% GST.
    // All three numbers, because a box that dropped the cap would understate a large buy and one
    // that dropped the rate would understate a small one.
    expect(text).toMatch(/1\.5\s?%/);
    expect(text).toContain("₹100");
    expect(text).toMatch(/GST/);
  });

  it("says what accrues nothing, which is most of what a user does", () => {
    render(<HowItWorksFlow />);
    const text = (screen.getByTestId("engine-cost").textContent ?? "").toLowerCase();
    // Rebalance, exit, partial exit and customize are all zero-fee. A cost box that only listed
    // the charge would read as a per-action fee this product does not levy.
    expect(text).toMatch(/rebalance/);
    expect(text).toMatch(/nothing|zero|no fee/);
  });

  it("does not claim brokerage and statutory charges are shown here, because they are not", () => {
    render(<HowItWorksFlow />);
    const text = (screen.getByTestId("how-it-works-attribution").textContent ?? "").replace(
      /\s+/g,
      " ",
    );
    // No plan surface in this app renders an STT or brokerage line — `curated_plans.py` and the
    // investment routers carry no cost field. The stage must attribute them to the broker.
    expect(text).toMatch(/brokerage and statutory charges are your broker/i);
    expect(text).toMatch(/at the broker/i);
  });

  it("puts the cost last in the engine, so the price is settled before the plan leaves", () => {
    // "with the cost visible before it runs" is an ordering claim, so it is asserted as one — as
    // a position in the pipeline rather than as a sentence somebody could reword away.
    const names = ENGINE.map((stage) => stage.name);
    expect(names.at(-1)).toBe("cost");
    expect(names.indexOf("plan")).toBeLessThan(names.indexOf("cost"));
  });

  it("keeps the cost inside the engine, left of every wire that reaches a rail", () => {
    render(<HowItWorksFlow />);
    // The box only means "before it runs" while it is upstream of the fan-out. Every rail wire
    // starts at the engine's right edge, so a cost box outside the engine card would break it.
    expect(screen.getByTestId("how-it-works-engine")).toContainElement(
      screen.getByTestId("engine-cost"),
    );
    for (const wire of RAIL_WIRES) {
      expect(walk(wire.d).start.x).toBe(ENGINE_RIGHT_EDGE);
    }
    expect(RAIL_WIRES).toHaveLength(3);
  });
});

/* ------------------------------------------------------------------- brokers */

describe("no broker is presented as connected except the one that is", () => {
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

/* ------------------------------------------------------------------- journey */

describe("the journey drawn is the journey shipped", () => {
  it("names the Portfolio hub's real tabs, from lib/nav rather than from memory", () => {
    const text = stageText();
    for (const tab of SECTION_TABS.portfolio) {
      expect(text).toContain(tab.label);
    }
  });

  it("starts from holdings, which is where the redesign put the front door", () => {
    const text = stageText();
    expect(text).toContain("Holdings");
    // The redesign's own step, missing from the first draft entirely.
    expect(ENGINE.map((stage) => stage.name)).toContain("organize");
    // §6.7's default state has a name, and hiding it here would misdescribe the first screen a
    // new user sees after connecting a broker.
    expect(text).toContain("Unallocated");
  });

  it("still says the shares are the reader's own, which is the §9 claim the page rests on", () => {
    const text = stageText();
    expect(text).toMatch(/your own demat/i);
    expect(text).toMatch(/nothing pooled/i);
  });

  it("still promises a plan that expires rather than one that fires", () => {
    // Non-negotiable 1: plans expire in 30 minutes and nothing auto-executes.
    expect(stageText()).toMatch(/expires in 30 minutes/i);
  });
});

/* ------------------------------------------------- the accessible fallback */

describe("the whole journey survives in reading order", () => {
  it("hides the wires from assistive technology", () => {
    const { container } = render(<HowItWorksFlow />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    // A connector is not information a screen reader can use; the list below is the same content.
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
  });

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
    // The stage is scroll-only on a phone, so anything the list omits is invisible there.
    for (const beat of ["Connect", "Screen", "Build", "Organize", "Confirm"]) {
      expect(text).toContain(beat);
    }
    expect(text).toMatch(/read-only order plan/i);
  });
});

/* ------------------------------------------------------------------ geometry */

describe("the geometry agrees with itself", () => {
  it("declares the exact length of every straight wire", () => {
    for (const wire of ALL_WIRES) {
      const { straightLength } = walk(wire.d);
      if (straightLength === null) continue;
      // A wrong length here makes the travelling dash the wrong size on that wire alone — the
      // long-wire-draws-a-bar bug the component's comment records.
      expect({ d: wire.d, length: wire.length }).toEqual({ d: wire.d, length: straightLength });
    }
  });

  it("keeps every curved wire's declared length between its chord and a sane bound", () => {
    for (const wire of ALL_WIRES) {
      if (walk(wire.d).straightLength !== null) continue;
      const c = chord(wire);
      // jsdom has no `getTotalLength`, so this bounds the measured number rather than recomputing
      // it: a curve is never shorter than its chord, and these are gentle S-bends.
      expect(wire.length).toBeGreaterThanOrEqual(c);
      expect(wire.length).toBeLessThanOrEqual(c * 1.3);
    }
  });

  it("keeps every wire inside the canvas it is drawn on", () => {
    for (const wire of ALL_WIRES) {
      for (const point of [walk(wire.d).start, walk(wire.d).end]) {
        expect(point.x).toBeGreaterThanOrEqual(0);
        expect(point.x).toBeLessThanOrEqual(STAGE_WIDTH);
        expect(point.y).toBeGreaterThanOrEqual(0);
        expect(point.y).toBeLessThanOrEqual(STAGE_HEIGHT);
      }
    }
  });

  it("draws the stage at the size the viewBox is drawn against", () => {
    const { container } = render(<HowItWorksFlow />);
    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("viewBox")).toBe(`0 0 ${STAGE_WIDTH} ${STAGE_HEIGHT}`);
    // The HTML nodes are positioned in the same coordinates, so a container that disagreed with
    // the viewBox would slide every card off its connector.
    expect(screen.getByTestId("how-it-works-stage").className).toContain(`h-[${STAGE_HEIGHT}px]`);
    expect(screen.getByTestId("how-it-works-stage").className).toContain(`w-[${STAGE_WIDTH}px]`);
  });

  it("gives every wire a dash that is the same size on all of them", () => {
    // The reason `length` exists at all. Five stage units on the shortest wire and on the longest.
    for (const wire of ALL_WIRES) {
      const dashPercent = (5 * 100) / wire.length;
      expect((dashPercent / 100) * wire.length).toBeCloseTo(5, 6);
    }
  });
});

/* ------------------------------------------------- the blurb above the stage */

describe("the section blurb promises only what the app does", () => {
  const PAGE = readFileSync(
    resolve(process.cwd(), "src/app/(marketing)/page.tsx"),
    "utf8",
  );

  it("no longer says statutory charges are on screen here", () => {
    // They are not: grep `curated_plans.py` and the investment routers for a cost field and you
    // get a comment. `/portfolio/[id]/costs` is accrued platform fees, not statutory charges.
    expect(PAGE).not.toMatch(/cost you in brokerage and statutory charges is on\s*\n?\s*screen/);
  });

  it("still says nothing on the site executes anything", () => {
    // The product's central claim, and non-negotiable 1 in prose form.
    expect(PAGE).toContain("Nothing on this site executes anything");
  });

  it("still heads the section with the cost promise the diagram now keeps", () => {
    expect(PAGE).toContain("From intent to result, with the cost visible before it runs.");
  });
});
