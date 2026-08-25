import {
  COVERAGE_PILLS,
  LANDING_CATEGORIES,
  LANDING_STATS,
  PRODUCT_PILLS,
  TICKER_LINES,
} from "@/lib/marketing/landing-band";

/**
 * The band under the hero, built to the shape Maulik pointed at on vaaya.ai (24 Aug 2026): two
 * pill rows scrolling in opposite directions, a ticker, four figures, four categories.
 *
 * ## Why it wears `vaaya-surface`
 *
 * That token set already exists — M37 added it for the screen editor — and it is exactly the look
 * being asked for: near-white canvas, Helvetica-ish sans, hairline borders, 26px cards, monochrome
 * primary. Reaching for it costs no new colours and no new contrast assertions, whereas inventing
 * a second set of landing-only tokens would put an unverified palette on the most-read page.
 *
 * ## Motion
 *
 * docs/08 §Motion is a rule for the *application* ("<=150 ms, transform/opacity only. No animated
 * tables"), and a looping marquee is none of those things. It is confined to the marketing route,
 * it animates `transform` only, and `prefers-reduced-motion` stops it dead rather than merely
 * shortening it — see the explicit rule in `globals.css`, because the blanket 0.01 ms override
 * would otherwise snap the track to its end position instead of leaving it where it started.
 *
 * ## Duplication in the DOM
 *
 * Each row's list is rendered twice. That is what makes a translate of -50% seamless: the second
 * copy is under the cursor at the instant the first wraps. The duplicate is `aria-hidden`, so a
 * screen reader hears the fourteen lists once, not twenty-eight times.
 */

function PillRow({
  items,
  direction,
  seconds,
  label,
  quiet = false,
}: {
  items: readonly string[];
  direction: "left" | "right";
  seconds: number;
  label: string;
  /** The coverage row is outlined rather than filled, so it reads as evidence under a claim. */
  quiet?: boolean;
}) {
  return (
    <div
      className="relative flex overflow-hidden [mask-image:linear-gradient(to_right,transparent,black_6%,black_94%,transparent)]"
      role="group"
      aria-label={label}
    >
      {[false, true].map((isDuplicate) => (
        <ul
          key={String(isDuplicate)}
          aria-hidden={isDuplicate || undefined}
          className="marquee-track flex shrink-0 items-center gap-2.5 pr-2.5"
          style={{
            animationName: direction === "left" ? "marquee-left" : "marquee-right",
            animationDuration: `${seconds}s`,
          }}
        >
          {items.map((item) => (
            <li
              key={item}
              className={
                quiet
                  ? "whitespace-nowrap rounded-full border border-white/45 bg-white/25 px-4 py-2 text-sm font-normal text-white backdrop-blur-md [text-shadow:0_1px_6px_rgba(0,0,0,0.4)]"
                  : "whitespace-nowrap rounded-full border border-white/25 bg-white/85 px-4 py-2 text-sm font-medium text-[#0a0a0a] shadow-[0_2px_20px_rgba(0,0,0,0.05)] backdrop-blur-xl"
              }
            >
              {item}
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}

/**
 * The two pill rows and the ticker, as a band that sits **over** the foot of the hero photograph.
 *
 * Split out of `LandingBand` so it can be positioned inside the image's own stacking context: on
 * the page this is modelled on, the rows overlap the picture rather than following it, and the
 * ticker rides the picture's bottom edge. Everything here is therefore drawn to read against a
 * photograph — the pills are glass rather than solid, and the ticker is an opaque rail so its small
 * type never has to compete with whatever is behind it.
 *
 * The figures and the four categories stay in normal flow below, in `LandingFigures`.
 */
export function LandingMarquee() {
  return (
    <div aria-label="What Baskfy does, and what it connects to" role="group">
      {/* The two rows are one visual object, so the gap between them is tighter than the gap to
          anything else on the page. */}
      <div className="space-y-2.5 pb-3">
        <PillRow
          items={PRODUCT_PILLS}
          direction="left"
          seconds={64}
          label="What you can run here"
        />
        <PillRow
          items={COVERAGE_PILLS}
          direction="right"
          seconds={52}
          label="What it connects to, and what it covers"
          quiet
        />
      </div>

      {/* The ticker. `//BASKFY` is pinned; the sentence behind it scrolls under it. */}
      <div className="relative flex items-center overflow-hidden border-t border-white/15 bg-[#f8fafc] py-3.5">
        <span className="absolute left-6 z-10 hidden rounded-full bg-white px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-[#6f6f6f] shadow-[0_2px_20px_rgba(0,0,0,0.05)] sm:block">
          //Baskfy
        </span>
        {/*
          The track is masked, the pill is not — hence the extra wrapper. `//Baskfy` is pinned over
          the rail and the sentence runs underneath it, so without a fade the moving text surfaces
          to the *left* of the pill and the whole thing reads as a rendering fault rather than as
          something scrolling past a label. The fade starts wide enough to clear the pill.
        */}
        <div className="flex min-w-0 flex-1 [mask-image:linear-gradient(to_right,transparent_0,transparent_9%,black_19%,black_95%,transparent)]">
          {[false, true].map((isDuplicate) => (
            <div
              key={String(isDuplicate)}
              aria-hidden={isDuplicate || undefined}
              className="marquee-track flex shrink-0 items-center gap-10 pr-10 text-sm text-muted-foreground"
              style={{ animationName: "marquee-left", animationDuration: "44s" }}
            >
              {TICKER_LINES.map((line) => (
                <span key={line} className="flex shrink-0 items-center gap-10 whitespace-nowrap">
                  {line}
                  <span aria-hidden className="text-muted-foreground/60">
                    ✦
                  </span>
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/** The four figures and the four categories — normal flow, under the picture. */
export function LandingFigures() {
  return (
    <section aria-labelledby="landing-band" className="border-b border-border">
      <h2 id="landing-band" className="sr-only">
        What Baskfy does
      </h2>

      <div className="mx-auto w-full max-w-6xl px-5 py-16 sm:px-7 lg:px-10">
        <ul className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {LANDING_STATS.map((stat) => (
            <li key={stat.label} className="vaaya-stat px-6 py-7">
              <p className="text-4xl font-light tracking-tight tabular-nums">{stat.value}</p>
              <p className="mt-2 text-sm text-muted-foreground">{stat.label}</p>
            </li>
          ))}
        </ul>

        <ul className="mt-12 grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
          {LANDING_CATEGORIES.map((category) => (
            <li key={category.title} className="space-y-2">
              <h3 className="text-base font-medium">{category.title}</h3>
              <p className="text-sm leading-relaxed text-muted-foreground">{category.body}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
