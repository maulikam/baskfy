import { SECTION_TABS } from "@/lib/nav";
import { cn } from "@/lib/utils";

/**
 * The animated "how it works" diagram: the whole platform, left to right, in one picture.
 *
 * ## What it has to say
 *
 * The diagram fails if a reader comes away thinking this is a momentum screener with a checkout
 * button (Maulik, 24 Aug 2026). The right-hand node is *portfolios*, plural, holding allocations,
 * not a single account, and nothing on the way there fires an order.
 *
 * ## Rebuilt 27 Aug 2026, and why the whole geometry went with it
 *
 * The version this replaces was a hard-coded `1360 x 420` stage inside an `overflow-x-auto`
 * wrapper, with every card `absolute`-positioned over one full-bleed SVG wire layer at
 * hand-measured `top` offsets. Four things followed from that single decision, and Maulik
 * rejected all four (`FLOW-REFINE-PROMPT.md`):
 *
 * 1. **It side-scrolled on a phone**, at 11px bodies and 8px eyebrows, because a fixed canvas has
 *    no other way to behave. Nothing on this stage is now below 12px and nothing is fixed-width.
 * 2. **The cards piled onto each other**, and the file's own comments recorded the failure mode:
 *    add a line of copy, a card's edge moves, a connector now points at nothing. Coordinates that
 *    have to agree with copy are coordinates that will stop agreeing with copy.
 * 3. **The cost box had to go** -- the fee does not get a box on the stage. That reverses G1 of
 *    `gates/marketing-flow-refresh.md` on Maulik's instruction, not on an agent's judgement, and
 *    the section heading changed in the same breath so the page stops promising a picture it no
 *    longer draws. The fee itself stays exactly where it was already documented: `fee-faq.tsx`,
 *    the numbered "Confirm" step, and the blurb above this stage.
 * 4. **The animation had no story.** Eight dashes on unrelated durations, looping forever, drift
 *    permanently out of phase after the first pass. See `FLOW_CYCLE_SECONDS`.
 *
 * ## The layout rule that makes (2) impossible rather than unlikely
 *
 * Everything here is in **normal document flow**. There is no absolute positioning, no overlay,
 * no fixed stage size, and no runtime measurement -- no resize observer, no `getBoundingClientRect`,
 * no client component. The stage is one CSS grid; the connectors are *cells of that grid*, each
 * drawing its own self-contained 40x40 SVG in the gap between two nodes. A connector therefore
 * cannot drift away from what it connects, because it has no coordinates of its own to drift in:
 * grid put it there. Copy can grow by a paragraph and nothing moves out from under anything.
 *
 * It reflows in two steps, and both are the width deciding, not a guess:
 *
 * - **Below `lg`** the grid collapses to one column, each connector's group swaps its horizontal
 *   path for its vertical one, and the diagram reads top to bottom in the same narrative order.
 * - **At `lg`** the seven columns arrive, but the engine keeps its stages two across: the middle
 *   column is about 390px there, and four stages in it would be ~84px each.
 * - **At `xl`** the stage runs wider than the prose beneath it and the engine finally packs four
 *   across with about 120px apiece.
 *
 * That is the entire responsive story -- no second markup tree, no hidden-on-mobile diagram, no
 * horizontal scroll at any width.
 *
 * ## Why the nodes are HTML and only the connectors are SVG
 *
 * Everything a reader reads is real DOM text: selectable, translatable, inherited from the theme
 * tokens, picked up by a screen reader in reading order without a single `<tspan>` of manual line
 * breaking. Each SVG draws a hairline and an arrowhead and nothing else, and is `aria-hidden` --
 * a connector is not information a screen reader can use, and the same journey is spelled out in
 * the numbered {@link STEPS} underneath, which is where the sentences live. The wire captions
 * that used to be SVG `<text>` are now HTML captions belonging to their connector cell.
 *
 * ## What was measured rather than recalled
 *
 * 1. **Sixty-six factors.** `factor_registry.NAMED_FACTOR_COUNT` (64 named + M56 short-horizon blends).
 * 2. **The front door is holdings.** `PORTFOLIO_REDESIGN.md` made the product holdings-first: you
 *    connect a broker, what you already own arrives **Unallocated**, and organising it is the
 *    first thing you do. Hence the engine's `organize` stage and "Connect" as step 1 of five.
 *    [`components/portfolio/unallocated-section.tsx`, `new-portfolio-flow.tsx`]
 * 3. **The landing node names surfaces that exist.** Its chips are {@link SECTION_TABS}
 *    `.portfolio`, imported rather than retyped, so a tab renamed in `lib/nav.ts` cannot leave a
 *    stale word on the landing page.
 * 4. **Only one broker is live, and the diagram says so.** `baskfy_core.broker_connections.BROKERS`
 *    carries ten; exactly one -- `zerodha` -- is `holdings_sync="ready"` and `trading="ready"`. The
 *    other nine are `planned` or `partner`, so the rails carry the count and the caveat together.
 *    [`packages/core/tests/test_broker_capability_honesty.py` exists because this has drifted]
 */

/* ------------------------------------------------------------------ timeline */

/**
 * One period for the whole diagram, in seconds. **Must equal `--flow-cycle` in `globals.css`**,
 * and `__tests__/how-it-works-flow.test.tsx` reads that file to prove it does.
 *
 * Every animated element on this stage -- every connector pulse, the return sweep, every stage
 * acknowledgment -- runs at exactly this duration and differs only by `animation-delay`. That is
 * the whole trick, and it is the fix for the old version's central bug: `animation-delay` offsets
 * only the *first* iteration, so wires given different durations and looped forever drift apart
 * permanently and the diagram degenerates into unrelated specks. Equal durations make the delays
 * a fixed phase relationship instead, which is what lets the beats below stay a sequence.
 *
 * The window a beat occupies lives in the keyframes (13% of the cycle for a segment, 30% for the
 * sweep), so the numbers here are read simply: the second, within an 11-second loop, at which
 * that beat begins.
 */
export const FLOW_CYCLE_SECONDS = 11;

/**
 * The fraction of the cycle a beat occupies. **Must match the keyframe percentages in
 * `globals.css`** (`flow-segment` finishes at 13%, `flow-sweep` at 30%); the test reads that file
 * and checks both, because these are what make the schedule below provably non-overlapping.
 */
export const SEGMENT_WINDOW = 0.13;
export const SWEEP_WINDOW = 0.3;

/**
 * The story, in the order the eye is meant to follow it, with a beat of rest before it repeats.
 *
 * You -> the engine, stage by stage -> out to the rails -> into your portfolios -> and the dashed
 * way back. At any instant there is one thing moving, which is the point: the old version fired
 * eight dashes at arbitrary offsets and the eye had nowhere to go.
 *
 *   0.2 -  1.6   you -> engine          (a segment travels for 13% of 11 s = 1.43 s)
 *   0.8 -  2.2   market data -> engine
 *   1.7 -  2.9   the four engine stages acknowledge in turn
 *   3.4 -  4.8   engine -> rails
 *   4.8 -  5.0   the three rails acknowledge, which is the fan-out
 *   5.3 -  6.7   rails -> portfolios
 *   7.2 - 10.5   the dashed return sweep
 *  10.5 - 11.0   rest
 *
 * The test asserts the *order* of these rather than the numbers: a choreography is a sequence, and
 * a sequence is the thing that can regress.
 */
export const TIMELINE = {
  you: 0,
  youToEngine: 0.2,
  market: 0.6,
  marketToEngine: 0.8,
  stages: [1.7, 2.1, 2.5, 2.9],
  engineToRails: 3.4,
  rails: [4.8, 4.9, 5.0],
  railsToPortfolios: 5.3,
  portfolios: 6.7,
  returnSweep: 7.2,
} as const;

/* ---------------------------------------------------------------- connectors */

/**
 * A connector's own coordinate system.
 *
 * Square, so the SVG scales uniformly at every size and the arrowhead never skews -- the reason
 * this is not one stretched full-width path. And 40, because the element is rendered at exactly
 * `h-10 w-10` (40px): the viewBox and the box agree, the scale factor is 1, and a 1.25-unit
 * hairline is a 1.25px hairline instead of whatever a fractional scale rounds it to. The old
 * stage got this for free by being 1360 units wide in a 1360px box; per-gap connectors have to be
 * given it deliberately.
 */
export const CONNECTOR_BOX = 40;

/** Horizontal run and its arrowhead, used from `lg` up. */
export const SEGMENT_H = "M3,20 H37";
const ARROW_H = "M31,14 L37,20 L31,26";

/** The same connector below `lg`, and the only shape the market-data feed ever takes. */
export const SEGMENT_V = "M20,3 V37";
const ARROW_V = "M14,31 L20,37 L26,31";

/**
 * The travelling dot, written in the `pathLength="100"` scale every pulse path is normalised to.
 *
 * The old file needed per-wire arithmetic here because its wires differed by a factor of thirty --
 * 38 units against 1312 -- so one dash width drew a speck on one and a bar on another. Per-gap
 * connectors are all the same 34-unit path, so one constant is correct on all of them, and the
 * only path that still needs its own number is the return sweep, which is deliberately longer
 * because a sweep is what it is meant to read as.
 */
export const PULSE_DASH = 14;
export const SWEEP_DASH = 5;

function dashArray(dash: number): string {
  return `${dash} ${100 - dash}`;
}

/**
 * One gap between two nodes: a hairline, an arrowhead, a pulse, and the caption that used to
 * float in the wire layer.
 *
 * Both orientations live in one SVG as two `<g>`s toggled by a breakpoint utility. That is two
 * short paths, not a second copy of the diagram: an arrow that points right and an arrow that
 * points down are genuinely different drawings, and drawing one and rotating it would either
 * distort the stroke or need a transform that lies about the element's box in flow.
 */
function Connector({
  delay,
  caption,
  vertical = false,
  className,
}: {
  delay: number;
  caption?: string;
  /** `true` for the market-data feed, which points down at every width. */
  vertical?: boolean;
  className?: string;
}) {
  function group(path: string, arrow: string, groupClassName?: string) {
    return (
      <g className={groupClassName}>
        <path className="flow-track" d={path} />
        <path className="flow-track" d={arrow} />
        <path
          className="flow-pulse"
          d={path}
          pathLength="100"
          strokeDasharray={dashArray(PULSE_DASH)}
          style={{ animationDelay: `${delay}s` }}
        />
      </g>
    );
  }

  return (
    <div
      data-testid="flow-connector"
      className={cn(
        "flex flex-col items-center justify-center gap-1 py-2 text-muted-foreground lg:py-0",
        className,
      )}
    >
      <svg
        aria-hidden
        viewBox={`0 0 ${CONNECTOR_BOX} ${CONNECTOR_BOX}`}
        className="h-10 w-10 shrink-0"
      >
        {vertical ? (
          group(SEGMENT_V, ARROW_V)
        ) : (
          <>
            {group(SEGMENT_V, ARROW_V, "lg:hidden")}
            {group(SEGMENT_H, ARROW_H, "hidden lg:inline")}
          </>
        )}
      </svg>
      {caption ? (
        <p className="max-w-[22ch] text-center text-[12px] italic leading-tight sm:text-[13px]">
          {caption}
        </p>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------- the content */

/**
 * The engine's four stages, in order: the whole product as four words.
 *
 * `cost` was a fifth and is gone (Maulik, 27 Aug 2026). The fee is real and is stated -- in
 * `fee-faq.tsx`, in the numbered "Confirm" step, and in the blurb above this stage -- but a fee is
 * not a step in the pipeline, and drawing it as one made the picture claim a pre-trade charges
 * screen this product does not have.
 */
export const ENGINE = [
  {
    name: "screen",
    badge: "Rank",
    body: "Sixty-six factors, point-in-time",
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
] as const;

/**
 * The journey in reading order -- and the only place the sentences live.
 *
 * This list, not the stage, is what a screen reader gets, so it has to carry every beat the
 * picture does. Five, because "Connect" is first: holdings arrive before anything has been built,
 * and "Organize" is the step that files them. "Confirm" keeps the fee sentence, which is where
 * the fee belongs now that it is off the stage.
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
    body: "A read-only order plan goes to your broker with our fee on it — 1.5% of a buy, capped at ₹100, plus GST, and nothing at all on a rebalance or an exit. You confirm it there, and the shares stay in your own name.",
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

/* ------------------------------------------------------------------- pieces */

/**
 * Type on this stage steps with the breakpoint rather than being frozen at one size, and its floor
 * is 12px — the phone the rebuild exists for. The one place that does *not* step up is the engine's
 * four stage boxes at `xl`, where the columns are at their narrowest: `sm:text-[14px]
 * xl:text-[13px]` on the stage name is the step back down, and their body copy stays at the floor
 * because a wider word in a narrower box is not a better read.
 */
function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "vaaya-pill inline-flex items-center whitespace-nowrap px-2.5 py-[3px] text-[12px] sm:text-[13px]",
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * A node's role label, and the thing that acknowledges the pulse as it arrives.
 *
 * Spelled out from the same tokens as `.vaaya-eyebrow` rather than composed with it, for two
 * reasons that both bite silently:
 *
 * 1. **The size has to be this file's decision.** `.vaaya-eyebrow` hard-codes `--v-text-11`, and
 *    it lives in a `@layer utilities` block *after* `@import "tailwindcss"` in `globals.css` --
 *    same layer, same specificity, later in source, so it wins over a `text-[12px]` sitting
 *    beside it on the element. The old stage's `vaaya-eyebrow text-[8px]` therefore never
 *    rendered at 8px and nobody could tell by reading it. This stage's rule is that nothing on it
 *    renders below 12px, and a rule that depends on cascade order is not a rule.
 * 2. **`transform` needs a box.** `flow-ack` lifts the label a pixel; a bare inline `<span>`
 *    ignores transforms entirely, so the acknowledgment would be an opacity flicker and the
 *    `translateY` would be dead code.
 * 3. **The colour is `--foreground`, not `--muted-foreground`, and that is deliberate.** The mute
 *    is `.flow-ack`'s rest opacity — one mechanism, not two. This read `text-muted-foreground`
 *    until 12 Sep 2026, and muted-at-70%-opacity composited to 2.91:1 in light and 4.42:1 in
 *    dark: seven of axe's twenty colour-contrast failures on the landing page. `globals.css`
 *    carries the arithmetic. Do not put a muted token back here without removing the opacity.
 */
function Eyebrow({ children, delay }: { children: React.ReactNode; delay: number }) {
  return (
    <span
      className="flow-ack inline-block font-mono text-[12px] font-medium uppercase tracking-[var(--v-tracking-caps)] text-foreground"
      style={{ animationDelay: `${delay}s` }}
    >
      {children}
    </span>
  );
}

/**
 * The rails' acknowledgment. A pill's label cannot carry {@link Eyebrow} without sitting at its
 * dimmed rest opacity permanently and reading as disabled, so the fan-out lights a dot instead --
 * three of them in quick succession, which is the split drawn in time rather than in geometry.
 */
function AckDot({ delay }: { delay: number }) {
  return (
    <span
      aria-hidden
      className="flow-ack mr-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-accent align-middle"
      style={{ animationDelay: `${delay}s` }}
    />
  );
}

function NodeTitle({ children }: { children: React.ReactNode }) {
  return <p className="text-[15px] font-medium sm:text-base">{children}</p>;
}

/* -------------------------------------------------------------------- stage */

export function HowItWorksFlow() {
  return (
    <div className="flow-stage px-6">
      {/*
        The stage runs wider than the prose beneath it from `xl` up. Seven columns of real content
        do not fit a 64ch measure, and the version this replaces was 1360px wide for the same
        reason -- it just took a fixed canvas to get there. The numbered steps stay at `max-w-6xl`,
        because a step is a sentence and a sentence has a reading width.
      */}
      <div data-testid="how-it-works-stage" className="mx-auto max-w-6xl xl:max-w-[84rem]">
        {/*
          One grid, seven columns from `lg` up -- node, gap, node, gap, ... -- and one column below
          it. The connectors are the odd columns, which is why nothing here needs a coordinate.
        */}
        <div
          data-testid="how-it-works-grid"
          className="grid grid-cols-1 items-center gap-x-1 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,3.1fr)_auto_minmax(0,0.95fr)_auto_minmax(0,1.05fr)]"
        >
          {/* You — the redesign's front door, in the order you arrive: an account, the shares
              already in it, then a rule. */}
          <div data-flow-node="you" className="vaaya-card p-4">
            <Eyebrow delay={TIMELINE.you}>Start</Eyebrow>
            <NodeTitle>You</NodeTitle>
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {YOU.map((item) => (
                <Chip key={item}>{item}</Chip>
              ))}
            </div>
          </div>

          <Connector delay={TIMELINE.youToEngine} caption="holdings" className="lg:w-20" />

          {/* Market data sits above the engine because it is an input and not a step, and the two
              are one grid cell so that relationship survives the breakpoint. */}
          <div className="flex flex-col">
            <div data-flow-node="market" className="vaaya-card p-4">
              <Eyebrow delay={TIMELINE.market}>Input</Eyebrow>
              <NodeTitle>Market data</NodeTitle>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {MARKET.map((item) => (
                  <Chip key={item}>{item}</Chip>
                ))}
              </div>
            </div>

            <Connector delay={TIMELINE.marketToEngine} caption="feeds the ranking" vertical />

            <div
              data-flow-node="engine"
              data-testid="how-it-works-engine"
              className="vaaya-card p-4"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-0.5">
                <NodeTitle>Baskfy</NodeTitle>
                <p className="text-[12px] italic text-muted-foreground sm:text-[13px]">
                  one rule in — one order plan out
                </p>
              </div>

              {/*
                Two across until `xl`, not `lg`. At `lg` the middle column is about 390px, so
                four stages would be ~84px each and "Sixty-six factors, point-in-time" would set
                six characters to a line. The 7-column layout still arrives at `lg`; only the
                engine's internal packing waits for the width that makes it readable.
              */}
              <div className="mt-3 grid grid-cols-2 gap-2 xl:grid-cols-4">
                {ENGINE.map((stage, index) => (
                  <div
                    key={stage.name}
                    data-testid={`engine-${stage.name}`}
                    className="rounded-2xl border border-border bg-muted/50 p-2.5"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-x-1">
                      <p className="text-[13px] font-medium sm:text-[14px] xl:text-[13px]">{stage.name}</p>
                      <Eyebrow delay={TIMELINE.stages[index] ?? TIMELINE.stages[0]}>
                        {stage.badge}
                      </Eyebrow>
                    </div>
                    <p className="mt-1.5 text-[12px] leading-snug text-muted-foreground">
                      {stage.body}
                    </p>
                    <p className="mt-2 text-[12px] font-medium leading-snug">{stage.footer}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <Connector delay={TIMELINE.engineToRails} className="lg:w-12" />

          {/* The rails a plan can leave by, with the count of the ones that cannot yet. The
              fan-out is drawn in time rather than in geometry: the three acknowledge in turn as
              the plan reaches them, which needs no coordinates to stay true. */}
          <div
            data-flow-node="rails"
            data-testid="how-it-works-rails"
            className="flex flex-col gap-2"
          >
            <div className="flex flex-wrap justify-center gap-2 lg:flex-col">
              {RAILS.map((rail, index) => (
                <p
                  key={rail}
                  className="vaaya-pill flex items-center justify-center px-2.5 py-1.5 text-[12px] font-normal sm:text-[13px]"
                >
                  <AckDot delay={TIMELINE.rails[index] ?? TIMELINE.rails[0]} />
                  {rail}
                </p>
              ))}
            </div>
            <p className="text-center text-[12px] leading-snug text-muted-foreground sm:text-[13px]">
              {RAILS_CAVEAT}
            </p>
          </div>

          <Connector
            delay={TIMELINE.railsToPortfolios}
            caption="you confirm at your broker"
            className="lg:w-24"
          />

          {/* Where it all lands. Plural, and named after the tabs a reader will actually find. */}
          <div data-flow-node="portfolios" className="vaaya-card p-4">
            <Eyebrow delay={TIMELINE.portfolios}>Result</Eyebrow>
            <NodeTitle>Your portfolios</NodeTitle>
            <p className="mt-1 text-[12px] leading-snug text-muted-foreground sm:text-[13px]">
              Unallocated until you file it.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {SECTION_TABS.portfolio.map((tab) => (
                <Chip key={tab.href} className="px-2 py-[2px]">
                  {tab.label}
                </Chip>
              ))}
            </div>
            <p className="mt-3 border-t border-border pt-2.5 text-[12px] leading-snug text-muted-foreground sm:text-[13px]">
              Daily value and returns. Your own demat — nothing pooled.
            </p>
          </div>
        </div>

        {/* The way back. Dashed rather than solid: holdings are a fact you read, not a call we
            make. It is a row of its own under the grid, so it spans the journey it closes without
            being laid over any of it. The line stretches; its arrowhead is a separate fixed-size
            SVG at the left end, so nothing about it skews on a narrow screen. */}
        <div data-testid="how-it-works-return" className="mt-6 text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <svg aria-hidden viewBox="0 0 12 12" className="h-3 w-3 shrink-0">
              <path className="flow-track" d="M7,2 L2,6 L7,10" />
            </svg>
            <svg
              aria-hidden
              viewBox="0 0 1000 12"
              preserveAspectRatio="none"
              className="h-3 w-full"
            >
              <path className="flow-track" d="M0,6 H1000" strokeDasharray="10 9" />
              <path
                className="flow-sweep"
                d="M1000,6 H0"
                pathLength="100"
                strokeDasharray={dashArray(SWEEP_DASH)}
                style={{ animationDelay: `${TIMELINE.returnSweep}s` }}
              />
            </svg>
          </div>
          <p className="mt-1.5 text-center text-[12px] italic leading-tight sm:text-[13px]">
            holdings sync back
          </p>
        </div>
      </div>

      {/* The same journey in reading order — the sentences, and the only version a screen
          reader gets. */}
      <ol
        data-testid="how-it-works-steps"
        className="mx-auto mt-10 grid max-w-6xl gap-6 sm:grid-cols-2 lg:grid-cols-5"
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
    </div>
  );
}
