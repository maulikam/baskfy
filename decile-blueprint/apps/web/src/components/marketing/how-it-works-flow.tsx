import { SECTION_TABS } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * The animated "how it works" diagram — the shape Maulik pointed at on vaaya.ai/how-it-works
 * (24 Aug 2026), redrawn for what actually happens here.
 *
 * ## What it has to say
 *
 * The whole platform, left to right, and the diagram fails if a reader comes away thinking this is
 * a momentum screener with a checkout button (Maulik, 24 Aug 2026). The right-hand node is
 * *portfolios*, plural, with allocations inside them, not a single account.
 *
 * ## Revised twice on 26 Aug 2026
 *
 * The first revision brought the content up to date with the portfolio redesign and then made the
 * cards paragraphs — five sentences on a diagram nobody reads sentences on. Maulik's note was
 * "more blocks, not this much text", and it is the right note: a flow chart's job is to show the
 * *shape*, and the prose already exists twice over, in the numbered list underneath and in the
 * three-ways section below it. So every node is now a short label plus chips, the engine grew from
 * three boxes to five, and no block carries more than one line of body copy.
 *
 * What was re-measured against the source rather than recalled, with the check in brackets:
 *
 * 1. **The front door moved.** `PORTFOLIO_REDESIGN.md` made the product holdings-first: you
 *    connect a broker, what you already own arrives **Unallocated**, and organising it is the
 *    first thing you do. That is why the engine has an `organize` box and why "Connect" is step 1
 *    of five. [`components/portfolio/unallocated-section.tsx`, `new-portfolio-flow.tsx`]
 * 2. **The landing node names surfaces that exist.** Its chips are {@link SECTION_TABS}
 *    `.portfolio`, imported rather than retyped, so a tab renamed in `lib/nav.ts` cannot leave a
 *    stale word on the landing page. The first draft said "Tradebook", which is not a page.
 * 3. **Only one broker is live, and the diagram says so.** `baskfy_core.broker_connections.BROKERS`
 *    carries ten; exactly one — `zerodha` — is `holdings_sync="ready"` and `trading="ready"`. The
 *    other nine are `planned` or `partner`, so the rails carry the count and the caveat together.
 *    [`packages/core/tests/test_broker_capability_honesty.py` exists because this has drifted]
 * 4. **The cost is a box in the pipeline, not a card off to the side.** The section is headed
 *    "with the cost visible before it runs" and the first draft showed no cost at all. `cost` is
 *    now the last box before the plan reaches a rail, which is the claim drawn as a shape. Its
 *    numbers are the platform fee's real ones — `min(Rs 100, 1.5% x amount) + 18% GST` on a buy,
 *    zero on a rebalance, an exit or a customize [`components/investments/fee-faq.tsx`].
 *
 *    What is *not* shipped is a pre-trade brokerage-and-STT line: `curated_plans.py` and the
 *    investment routers carry no cost field, and `/portfolio/[id]/costs` is accrued platform fees,
 *    not statutory charges. Hence the attribution line under the engine, which is the one sentence
 *    on the stage that cannot be shortened away.
 *
 * The factor count needed no change: `factor_registry.NAMED_FACTOR_COUNT` is 64.
 *
 * ## Why the nodes are HTML and only the wires are SVG
 *
 * Everything a reader reads is real DOM text: selectable, translatable, inherited from the theme
 * tokens, and picked up by a screen reader in reading order without a single `<tspan>` of manual
 * line breaking. The SVG layer draws nothing but connectors and their labels, and is
 * `aria-hidden` — a wire is not information a screen reader can use, and the same journey is
 * spelled out in the numbered list underneath, which is where the sentences live.
 *
 * ## Why the stage is a fixed 1360 x 420, and why the cards have explicit heights
 *
 * Connector coordinates and node positions have to agree, and the only way to make them agree
 * without measuring the DOM at runtime (a client component, a resize observer, a layout thrash on
 * every breakpoint) is to draw both against one fixed canvas. Below that width the canvas scrolls
 * horizontally, which is what the site being copied does too.
 *
 * Every card a wire starts from or ends below carries an explicit `h-[...]`. The first draft let
 * them size to their content and guessed where the bottom would land; adding a line of copy then
 * silently moved a card's edge under a connector, which is exactly what happened. The heights
 * below were measured off the rendered page with slack, and `__tests__/how-it-works-flow.test.tsx`
 * re-derives every straight wire's length from its own path data.
 *
 * ## The animation
 *
 * Each wire is drawn twice: a static hairline, and over it a short dash that travels the path.
 * `pathLength="100"` normalises every path, so the same `100 -> 0` keyframe makes exactly one
 * traversal per cycle whether the wire is 38 units or 1312 — but the dash *width* has to be
 * converted back out of that scale per wire, or the long one draws a bar. See `Wire.length`.
 * `stroke-dashoffset` only, and `prefers-reduced-motion` removes the travelling dashes entirely
 * rather than parking them at the end of their wires (`globals.css`).
 */

/** The stage's own coordinate system. The container and the `viewBox` are the same numbers. */
export const STAGE_WIDTH = 1360;
export const STAGE_HEIGHT = 420;

export interface Wire {
  d: string;
  /**
   * The path's own length in stage units.
   *
   * It is here because a travelling dash has to be the same *size* on every wire, and the wires
   * differ by a factor of thirty — the return sweep is 1312 units, the shortest is 38. With
   * `pathLength="100"` normalising each path, a dash written in the normalised scale is
   * `DASH_UNITS * 100 / length`, which is a constant number of real units on every wire. Get this
   * wrong and the long wire draws a 40-unit bar while the short ones draw a speck (it did).
   *
   * For the straight runs this is arithmetic and the test checks it exactly. For the four fan
   * curves it is `getTotalLength()` read off the rendered page — they are 72 and not the 68 their
   * chord suggests — and the test bounds it against the chord rather than pretending jsdom can
   * measure a bezier.
   */
  length: number;
  /** Seconds for one traversal. Authored per wire: the long way back reads better as a sweep. */
  duration: number;
  delay: number;
}

/** How long the travelling dash is, in stage units, on every wire. */
const DASH_UNITS = 5;

/**
 * Geometry, in the stage's own coordinates. The node positions below are the same numbers, and
 * every horizontal wire sits on one spine at y = 230 — which is why each card's `top` is half its
 * height subtracted from that spine rather than a round number.
 *
 *   you       190,230   ->  baskfy    250,230
 *   market    610, 86   ->  baskfy    610,130
 *   baskfy    920,230   ->  rails     970,{176, 230, 284}
 *   rails    1120,*     ->  folios   1170,230
 *   folios   1265,354   ->  you       105,294   (dashed, the way back)
 */
export const WIRES: readonly Wire[] = [
  { d: "M190,230 H242", length: 52, duration: 2.2, delay: 0 },
  { d: "M610,86 V124", length: 38, duration: 1.8, delay: 0.5 },
  { d: "M920,230 C944,230 946,176 962,176", length: 72, duration: 2.2, delay: 1.2 },
  { d: "M920,230 H962", length: 42, duration: 2.2, delay: 1.2 },
  { d: "M920,230 C944,230 946,284 962,284", length: 72, duration: 2.2, delay: 1.2 },
  { d: "M1120,176 C1144,176 1146,230 1162,230", length: 72, duration: 2.2, delay: 2 },
  { d: "M1120,230 H1162", length: 42, duration: 2.2, delay: 2 },
  { d: "M1120,284 C1144,284 1146,230 1162,230", length: 72, duration: 2.2, delay: 2 },
];

/** The way back. Dashed rather than solid: holdings are a fact you read, not a call we make. */
export const RETURN_WIRE: Wire = {
  d: "M1265,354 V400 H105 V294",
  length: 1312,
  duration: 6,
  delay: 2.8,
};

/** The x where a wire leaves the engine for a rail. The cost box has to sit left of it. */
export const ENGINE_RIGHT_EDGE = 920;

/**
 * The engine's five boxes, in order: the whole product as five words.
 *
 * Five, not the first draft's three. `organize` is the redesign's central step and was missing;
 * `cost` is the section heading's promise and was missing. Each body is **one short line** — the
 * sentences belong in the numbered steps, which is what a phone and a screen reader get anyway.
 *
 * The order is load-bearing: `cost` is last, so the picture says the price is settled before the
 * plan reaches a rail. The test asserts that rather than trusting this comment.
 */
export const ENGINE = [
  {
    name: "screen",
    badge: "Rank",
    body: "Sixty-four factors, point-in-time",
    footer: "formulas published",
  },
  {
    name: "basket",
    badge: "Build",
    body: "Weights, caps, a written thesis",
    footer: "versioned",
  },
  {
    name: "organize",
    badge: "File",
    body: "Unallocated until you file it",
    footer: "many portfolios, side by side",
  },
  {
    name: "plan",
    badge: "Review",
    body: "Read-only, line by line",
    footer: "expires in 30 minutes",
  },
  {
    name: "cost",
    badge: "Price",
    body: "1.5% of a buy, capped at ₹100, plus GST",
    footer: "nothing on a rebalance or an exit",
  },
] as const;

/**
 * The one sentence on the stage that cannot be shortened away.
 *
 * No plan surface in this app renders a brokerage or STT line, so the `cost` box states our fee
 * and this states whose the rest are. Dropping it would leave a cost box that reads as the whole
 * bill.
 */
const ATTRIBUTION =
  "That is our fee. Brokerage and statutory charges are your broker's, shown at the broker on the order you confirm there.";

/**
 * The journey in reading order — and the only place the sentences live now.
 *
 * This list, not the stage, is what a screen reader gets and what a phone shows, so it has to
 * carry every beat the picture does. Five, because "Connect" is first: holdings arrive before
 * anything has been built, and "Organize" is the step that files them.
 */
const STEPS = [
  {
    ordinal: "1",
    title: "Connect",
    body: "Link a broker or import a statement. What you already own arrives before you have built anything.",
  },
  {
    ordinal: "2",
    title: "Screen",
    body: "Rank every listed name on the factors you chose, over the index membership the date actually had.",
  },
  {
    ordinal: "3",
    title: "Build",
    body: "Save the rule as a basket, or subscribe to one a registered manager publishes. Either way it is versioned.",
  },
  {
    ordinal: "4",
    title: "Organize",
    body: "File holdings into portfolios. Whatever you have not filed stays visible as Unallocated rather than hidden.",
  },
  {
    ordinal: "5",
    title: "Confirm",
    body: "A read-only order plan goes to your broker with our fee on it. You confirm it there, and the shares stay in your own name.",
  },
] as const;

/** What you bring. Three chips, not a paragraph. */
const YOU = ["Broker", "Holdings", "Your rules"] as const;

/** What feeds the ranking. */
const MARKET = ["NSE closes", "Corporate actions", "Index membership"] as const;

/**
 * The rails a plan can leave by, and the one fact about them that is easy to get wrong.
 *
 * `BROKERS` carries ten entries and one of them works. Naming the other nine here would read as
 * nine connections; naming none of them would hide a roadmap a reader is entitled to. So: the live
 * rail by name, the rest as a count with the caveat attached to it.
 */
const RAILS = ["Zerodha Kite", "Manual", "CSV export"] as const;
const RAILS_CAVEAT = "Nine more brokers planned. None live yet.";

function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "vaaya-pill inline-flex items-center whitespace-nowrap px-2.5 py-[3px] text-[11px]",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** The dash pattern, in the `pathLength="100"` scale, that draws one `DASH_UNITS`-long dot. */
function dashArray(wire: Wire): string {
  const dash = (DASH_UNITS * 100) / wire.length;
  return `${dash.toFixed(3)} ${(100 - dash).toFixed(3)}`;
}

function Wires() {
  return (
    <svg
      aria-hidden
      viewBox={`0 0 ${STAGE_WIDTH} ${STAGE_HEIGHT}`}
      className="pointer-events-none absolute inset-0 h-full w-full text-muted-foreground"
    >
      <defs>
        <marker
          id="how-it-works-arrow"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 Z" fill="currentColor" opacity="0.5" />
        </marker>
      </defs>

      {[...WIRES, RETURN_WIRE].map((wire, index) => (
        <path
          key={`track-${index}`}
          d={wire.d}
          className="flow-track"
          strokeDasharray={wire === RETURN_WIRE ? "3 5" : undefined}
          markerEnd={wire === RETURN_WIRE ? undefined : "url(#how-it-works-arrow)"}
        />
      ))}

      {[...WIRES, RETURN_WIRE].map((wire, index) => (
        <path
          key={`pulse-${index}`}
          d={wire.d}
          pathLength="100"
          className="flow-pulse"
          style={{
            strokeDasharray: dashArray(wire),
            animationDuration: `${wire.duration}s`,
            animationDelay: `${wire.delay}s`,
          }}
        />
      ))}

      <g fill="currentColor" fontSize="11" fontStyle="italic" opacity="0.85">
        <text x="216" y="221" textAnchor="middle">
          holdings
        </text>
        <text x="620" y="108" textAnchor="start">
          feeds the ranking
        </text>
        <text x="1045" y="362" textAnchor="middle">
          you confirm at your broker
        </text>
        <text x="660" y="392" textAnchor="middle">
          holdings sync back
        </text>
      </g>
    </svg>
  );
}

export function HowItWorksFlow() {
  return (
    <>
      {/* The scroll container holds the stage and *only* the stage: with the step list inside it,
          dragging the diagram sideways on a phone would drag the prose with it. */}
      <div className="overflow-x-auto pb-2">
        <div data-testid="how-it-works-stage" className="relative mx-auto h-[420px] w-[1360px]">
          <Wires />

          {/* You — the redesign's front door, in the order you arrive: an account, the shares
              already in it, then a rule. */}
          <div className="vaaya-card absolute left-0 top-[172px] h-[116px] w-[190px] p-4">
            <p className="text-[15px] font-medium">You</p>
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {YOU.map((item) => (
                <Chip key={item}>{item}</Chip>
              ))}
            </div>
          </div>

          {/* Market data — above the engine, because it is an input and not a step. */}
          <div className="vaaya-card absolute left-[300px] top-0 h-[86px] w-[620px] px-5 py-4">
            <p className="text-[15px] font-medium">Market data</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {MARKET.map((item) => (
                <Chip key={item}>{item}</Chip>
              ))}
            </div>
          </div>

          {/* The engine: the product as five words, cost last. */}
          <div
            data-testid="how-it-works-engine"
            className="vaaya-card absolute left-[250px] top-[130px] h-[184px] w-[670px] p-4"
          >
            <div className="flex items-baseline justify-between px-0.5">
              <p className="text-[15px] font-medium">Baskfy</p>
              <p className="text-[11px] italic text-muted-foreground">
                one rule in — one order plan out
              </p>
            </div>

            <div className="mt-2.5 flex items-stretch gap-2">
              {ENGINE.map((stage) => (
                <div
                  key={stage.name}
                  data-testid={`engine-${stage.name}`}
                  className="flex-1 rounded-2xl border border-border bg-muted/50 p-2.5"
                >
                  <div className="flex items-center justify-between gap-1">
                    <p className="text-[13px] font-medium">{stage.name}</p>
                    <span className="vaaya-eyebrow text-[8px]">{stage.badge}</span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
                    {stage.body}
                  </p>
                  <p className="mt-2 text-[10.5px] font-medium leading-snug">{stage.footer}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Whose the rest of the bill is. Directly under the cost box, deliberately. */}
          <p
            data-testid="how-it-works-attribution"
            className="absolute left-[250px] top-[322px] w-[670px] text-center text-[11px] leading-snug text-muted-foreground"
          >
            {ATTRIBUTION}
          </p>

          {/* The rails a plan can leave by, with the count of the ones that cannot yet. */}
          <div className="absolute left-[970px] top-[158px] flex w-[150px] flex-col gap-[18px]">
            {RAILS.map((rail) => (
              <p
                key={rail}
                className="vaaya-pill flex h-9 items-center justify-center text-[12.5px] font-normal"
              >
                {rail}
              </p>
            ))}
          </div>
          <p className="absolute left-[970px] top-[312px] w-[150px] text-center text-[11px] leading-snug text-muted-foreground">
            {RAILS_CAVEAT}
          </p>

          {/* Where it all lands. Plural, and named after the tabs a reader will actually find. */}
          <div className="vaaya-card absolute left-[1170px] top-[114px] h-[232px] w-[190px] p-4">
            <p className="text-[15px] font-medium">Your portfolios</p>
            <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
              Unallocated until you file it.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {SECTION_TABS.portfolio.map((tab) => (
                <Chip key={tab.href} className="px-2 py-[2px] text-[10px]">
                  {tab.label}
                </Chip>
              ))}
            </div>
            <p className="mt-3 border-t border-border pt-2.5 text-[11px] leading-snug text-muted-foreground">
              Daily value and returns. Your own demat — nothing pooled.
            </p>
          </div>
        </div>
      </div>

      {/* The same journey in reading order — and the whole of it on a phone. */}
      <ol
        data-testid="how-it-works-steps"
        className="mx-auto mt-4 grid max-w-6xl gap-6 px-6 sm:grid-cols-2 lg:grid-cols-5"
      >
        {STEPS.map((step) => (
          <li key={step.ordinal} className="space-y-1.5 border-t border-border pt-4">
            <p className="text-sm font-medium">
              <span className="text-muted-foreground">{step.ordinal} · </span>
              {step.title}
            </p>
            <p className="text-sm leading-relaxed text-muted-foreground">{step.body}</p>
          </li>
        ))}
      </ol>
    </>
  );
}
