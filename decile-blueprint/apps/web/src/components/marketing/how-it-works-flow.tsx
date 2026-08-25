import { cn } from "@/lib/utils";

/**
 * The animated "how it works" diagram — the shape Maulik pointed at on vaaya.ai/how-it-works
 * (24 Aug 2026), redrawn for what actually happens here.
 *
 * ## What it has to say
 *
 * Three products, one journey, and the diagram fails if a reader comes away thinking this is a
 * momentum screener with a checkout button (Maulik, 24 Aug 2026). The engine card therefore has
 * three boxes and not two — **screen -> basket -> plan** — because that is the whole platform in
 * three words: the screener, the basket a rule becomes, and the order plan a portfolio produces.
 * The right-hand node is *portfolios*, plural, with sleeves inside them, not a single account.
 *
 * ## Why the nodes are HTML and only the wires are SVG
 *
 * Everything a reader reads is real DOM text: selectable, translatable, inherited from the theme
 * tokens, and picked up by a screen reader in reading order without a single `<tspan>` of manual
 * line breaking. The SVG layer draws nothing but connectors and their four labels, and is
 * `aria-hidden` — a wire is not information a screen reader can use, and the same journey is
 * spelled out in the numbered list underneath.
 *
 * ## Why the stage is a fixed 1360 x 500
 *
 * Connector coordinates and node positions have to agree, and the only way to make them agree
 * without measuring the DOM at runtime (a client component, a resize observer, a layout thrash on
 * every breakpoint) is to draw both against one fixed canvas. Below that width the canvas scrolls
 * horizontally, which is what the site being copied does too. The numbered steps below are the
 * whole content in linear form, so a phone reader loses nothing by not scrolling it.
 *
 * ## The animation
 *
 * Each wire is drawn twice: a static hairline, and over it a short dash that travels the path.
 * `pathLength="100"` normalises every path, so the same `100 -> 0` keyframe makes exactly one
 * traversal per cycle whether the wire is 42 units or 1361 — but the dash *width* has to be
 * converted back out of that scale per wire, or the long one draws a bar. See `Wire.length`.
 * `stroke-dashoffset` only, and `prefers-reduced-motion` removes the travelling dashes entirely
 * rather than parking them at the end of their wires (`globals.css`).
 */

interface Wire {
  d: string;
  /**
   * The path's own length in stage units, from `getTotalLength()`.
   *
   * It is here because a travelling dash has to be the same *size* on every wire, and the wires
   * differ by a factor of thirty — the return sweep is 1361 units, the shortest fan is 42. With
   * `pathLength="100"` normalising each path, a dash written in the normalised scale is
   * `DASH_UNITS * 100 / length`, which is a constant number of real units on every wire. Get this
   * wrong and the long wire draws a 40-unit bar while the short ones draw a speck (it did).
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
 * every horizontal wire sits on one spine at y = 250 — which is why each card's `top` is its
 * measured height subtracted from that spine rather than a round number.
 *
 *   you       230,250   ->  baskfy    300,250
 *   market    590, 93   ->  baskfy    590,143
 *   baskfy    880,250   ->  rails     930,{196, 250, 304}
 *   rails    1090,*     ->  folios   1140,250
 *   folios   1250,375   ->  you       105,333   (dashed, the way back)
 *
 * `length` is `getTotalLength()` read off the rendered page, not an estimate — the fan curves are
 * 72 and not the 63 the chord suggests, and the dash width depends on getting that right.
 */
const WIRES: readonly Wire[] = [
  { d: "M230,250 H292", length: 62, duration: 2.2, delay: 0 },
  { d: "M590,93 V135", length: 42, duration: 1.8, delay: 0.5 },
  { d: "M880,250 C904,250 906,196 922,196", length: 72, duration: 2.2, delay: 1 },
  { d: "M880,250 H922", length: 42, duration: 2.2, delay: 1 },
  { d: "M880,250 C904,250 906,304 922,304", length: 72, duration: 2.2, delay: 1 },
  { d: "M1090,196 C1114,196 1116,250 1132,250", length: 72, duration: 2.2, delay: 1.8 },
  { d: "M1090,250 H1132", length: 42, duration: 2.2, delay: 1.8 },
  { d: "M1090,304 C1114,304 1116,250 1132,250", length: 72, duration: 2.2, delay: 1.8 },
];

/** The way back. Dashed rather than solid: holdings are a fact you read, not a call we make. */
const RETURN_WIRE: Wire = { d: "M1250,375 V462 H105 V333", length: 1361, duration: 6, delay: 2.6 };

/**
 * The engine's three boxes, which are the three products in order. Kept as data so the card is one
 * loop rather than three near-identical blocks that can drift apart.
 */
const ENGINE = [
  {
    name: "screen",
    badge: "Rank",
    body: "Sixty-four factors on adjusted closes, over the index membership that date actually had.",
    footer: "formulas published",
  },
  {
    name: "basket",
    badge: "Build",
    body: "The rule saved with weights, caps and a written thesis. Versioned every time it changes.",
    footer: "held in your name",
  },
  {
    name: "plan",
    badge: "Review",
    body: "A portfolio, or one sleeve of it, turned into a read-only order plan you read line by line.",
    footer: "expires in 30 minutes",
  },
] as const;

const STEPS = [
  {
    ordinal: "1",
    title: "Screen",
    body: "Rank every listed name on the factors you chose, over the index membership the date actually had.",
  },
  {
    ordinal: "2",
    title: "Build",
    body: "Save the rule as a basket — weights, caps and a written thesis, versioned every time it changes.",
  },
  {
    ordinal: "3",
    title: "Allocate",
    body: "Put the basket in a portfolio, or in one sleeve of it, beside whatever else that account holds.",
  },
  {
    ordinal: "4",
    title: "Confirm",
    body: "A read-only order plan goes to your broker. You confirm it there, and the shares stay in your own name.",
  },
] as const;

function Pill({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "vaaya-pill inline-flex items-center whitespace-nowrap px-3 py-1 text-xs",
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
      viewBox="0 0 1360 500"
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

      <g fill="currentColor" fontSize="11.5" fontStyle="italic" opacity="0.85">
        <text x="261" y="240" textAnchor="middle">
          rules
        </text>
        <text x="600" y="121" textAnchor="start">
          feeds the ranking
        </text>
        <text x="1010" y="352" textAnchor="middle">
          you confirm at your broker
        </text>
        <text x="680" y="454" textAnchor="middle">
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
        <div className="relative mx-auto h-[500px] w-[1360px]">
          <Wires />

          {/* You — left. Three pills, because there are three things you can start from. */}
          <div className="vaaya-card absolute left-0 top-[168px] w-[230px] p-5">
            <p className="text-[15px] font-medium">You</p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              <Pill>Screen</Pill>
              <Pill>Basket</Pill>
              <Pill>Portfolio</Pill>
            </div>
            <p className="mt-3.5 text-xs text-muted-foreground">Set the rules once.</p>
          </div>

          {/* Market data — above the engine, because it is an input and not a step. */}
          <div className="vaaya-card absolute left-[440px] top-0 w-[300px] px-5 py-4">
            <p className="text-[15px] font-medium">Market data</p>
            <p className="mt-1 text-xs text-muted-foreground">
              NSE closes · corporate actions · index membership
            </p>
          </div>

          {/* The engine: the three products, in order. */}
          <div className="vaaya-card absolute left-[300px] top-[143px] w-[580px] p-4">
            <div className="flex items-baseline justify-between px-1">
              <p className="text-[15px] font-medium">Baskfy</p>
              <p className="text-[11px] italic text-muted-foreground">
                one rule in — one order plan out
              </p>
            </div>

            <div className="mt-3 flex items-stretch gap-2.5">
              {ENGINE.map((stage) => (
                <div
                  key={stage.name}
                  className="flex-1 rounded-[18px] border border-border bg-muted/50 p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-sm font-medium">{stage.name}</p>
                    <span className="vaaya-eyebrow text-[9px]">{stage.badge}</span>
                  </div>
                  <p className="mt-2 text-[11.5px] leading-snug text-muted-foreground">
                    {stage.body}
                  </p>
                  <p className="mt-3 text-[11px] font-medium">{stage.footer}</p>
                </div>
              ))}
            </div>
          </div>

          {/* The rails a plan can leave by. */}
          <div className="absolute left-[930px] top-[178px] flex w-[160px] flex-col gap-[18px]">
            {["Zerodha Kite", "Manual", "CSV export"].map((rail) => (
              <p
                key={rail}
                className="vaaya-pill flex h-9 items-center justify-center text-[13px] font-normal"
              >
                {rail}
              </p>
            ))}
          </div>

          {/* Where it all lands. Plural, and with sleeves inside — this is the part that is not a
              screener. */}
          <div className="vaaya-card absolute left-[1140px] top-[125px] w-[220px] p-5">
            <p className="text-[15px] font-medium">Your portfolios</p>
            <p className="mt-1 text-xs text-muted-foreground">One account, many — and sleeves</p>
            <div className="mt-3.5 grid grid-cols-2 gap-1.5">
              {["Sleeves", "Weights", "Rebalances", "Tradebook"].map((item) => (
                <Pill key={item} className="justify-center px-2 text-[11px]">
                  {item}
                </Pill>
              ))}
            </div>
            <p className="mt-4 border-t border-border pt-3 text-[11.5px] leading-snug text-muted-foreground">
              Shares sit in your own demat. Nothing pooled, nothing held on your behalf.
            </p>
          </div>
        </div>
      </div>

      {/* The same journey in reading order — and the whole of it on a phone. */}
      <ol className="mx-auto mt-4 grid max-w-6xl gap-6 px-6 sm:grid-cols-2 lg:grid-cols-4">
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
