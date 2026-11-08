import { cn } from "@/lib/utils";

/**
 * A drawn mark per strategy family, replacing the two-letter monogram.
 *
 * The monogram it replaces took the first letter of the first two words, so "Broad Market Sharpe"
 * and "Liquid Momentum" became BM and LM — initials that identify nothing and that a reader has
 * to decode back into a name they already knew. A shape says what the strategy *does*, is
 * recognisable at a glance down a column of cards, and is the same shape for two baskets that do
 * the same thing, which is information rather than noise.
 *
 * **Drawn, not coloured, and that is deliberate.** The brief asks for "distinct but restrained
 * colours" per strategy category. This product's palette makes the opposite decision on purpose
 * (`app/globals.css`): the accent is near-black and *colour is reserved for meaning* — green up,
 * red down, amber caution — with structure carried by ink, hairlines and space. `contrast.test.ts`
 * parses that file and enforces it, and `no-brand-as-text.test.ts` fails the build on coloured
 * type. Giving eight strategy families eight hues would put eight decorative colours in front of
 * the three that mean something, and would make the mark's own orange one signal among many.
 * Form carries the distinction instead, which is what the palette leaves free.
 */

export type StrategyFamily =
  | "momentum"
  | "trend"
  | "low-volatility"
  | "broad-market"
  | "liquidity"
  | "quality"
  | "short-window"
  | "general";

/**
 * Category tag → family, in priority order.
 *
 * Ordered rather than a lookup because a basket carries several tags — `["momentum", "liquidity"]`
 * — and the mark has to pick one. The first match down this list wins, so the more specific
 * qualifier beats the family everything in this catalogue shares.
 */
const FAMILY_ORDER: readonly { family: StrategyFamily; tags: readonly string[] }[] = [
  { family: "low-volatility", tags: ["low-volatility", "risk-trimmed", "defensive"] },
  { family: "quality", tags: ["quality"] },
  { family: "liquidity", tags: ["liquidity", "liquid"] },
  { family: "trend", tags: ["trend", "regime"] },
  { family: "short-window", tags: ["short-window"] },
  { family: "broad-market", tags: ["broad-market", "diversified", "index"] },
  { family: "momentum", tags: ["momentum"] },
];

export function familyOf(categories: readonly string[]): StrategyFamily {
  const owned = new Set(categories.map((tag) => tag.toLowerCase()));
  for (const entry of FAMILY_ORDER) {
    if (entry.tags.some((tag) => owned.has(tag))) return entry.family;
  }
  return "general";
}

/** What the shape means, in words — the mark is never the only carrier. */
export const FAMILY_LABEL: Record<StrategyFamily, string> = {
  momentum: "Momentum — holds what has been rising",
  trend: "Trend — follows the market's direction and steps aside",
  "low-volatility": "Lower volatility — trimmed for smaller swings",
  "broad-market": "Broad market — spread across the whole index",
  liquidity: "Liquidity — restricted to easily traded stocks",
  quality: "Quality — filtered on company fundamentals",
  "short-window": "Short window — ranked over a recent period",
  general: "Strategy basket",
};

function Shape({ family }: { family: StrategyFamily }) {
  switch (family) {
    case "momentum":
      return (
        <>
          <rect x="4" y="14" width="3.5" height="6" rx="1" />
          <rect x="10.25" y="9" width="3.5" height="11" rx="1" />
          <rect x="16.5" y="4" width="3.5" height="16" rx="1" />
        </>
      );
    case "trend":
      return (
        <>
          <path d="M4 17.5 L9.5 11.5 L13.5 14.5 L20 6.5" fill="none" strokeWidth="2" />
          <path d="M15.5 6.5 H20 V11" fill="none" strokeWidth="2" />
        </>
      );
    case "low-volatility":
      return (
        <path
          d="M3.5 12.5 C6 9.5, 8 15, 10.5 12.5 C13 10, 15 15, 17.5 12.5 C19 11, 20 12, 20.5 12.5"
          fill="none"
          strokeWidth="2"
        />
      );
    case "broad-market":
      return (
        <>
          {[6, 12, 18].map((cy) =>
            [6, 12, 18].map((cx) => <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r="1.6" />),
          )}
        </>
      );
    case "liquidity":
      return (
        <>
          <circle cx="12" cy="12" r="2" />
          <path d="M12 6.5 a5.5 5.5 0 0 1 0 11" fill="none" strokeWidth="1.75" />
          <path d="M12 3 a9 9 0 0 1 0 18" fill="none" strokeWidth="1.75" opacity="0.45" />
        </>
      );
    case "quality":
      return (
        <path
          d="M12 3.5 L19.5 7 V12.5 C19.5 16.5, 16 19.5, 12 20.5 C8 19.5, 4.5 16.5, 4.5 12.5 V7 Z"
          fill="none"
          strokeWidth="1.9"
        />
      );
    case "short-window":
      return (
        <>
          <circle cx="12" cy="12" r="8" fill="none" strokeWidth="1.9" />
          <path d="M12 7.5 V12 L15.5 14" fill="none" strokeWidth="1.9" />
        </>
      );
    default:
      return (
        <>
          <circle cx="12" cy="12" r="7.5" fill="none" strokeWidth="1.9" />
          <circle cx="12" cy="12" r="2.25" />
        </>
      );
  }
}

/**
 * The mark, with its meaning available to a screen reader and on hover.
 *
 * `role="img"` with a `<title>`, not `aria-hidden`: the shape is the only thing distinguishing
 * two otherwise identical cards at a glance, so a reader who cannot see it is owed the sentence.
 */
export function StrategyMark({
  categories,
  size = 40,
  className,
}: {
  categories: readonly string[];
  size?: number;
  className?: string;
}) {
  const family = familyOf(categories);
  const label = FAMILY_LABEL[family];

  return (
    <span
      className={cn(
        "grid shrink-0 place-items-center rounded-lg border border-border bg-muted/60 text-foreground/80",
        className,
      )}
      style={{ width: size, height: size }}
      data-testid="strategy-mark"
      data-family={family}
      title={label}
    >
      <svg
        viewBox="0 0 24 24"
        width={size * 0.6}
        height={size * 0.6}
        fill="currentColor"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        role="img"
        aria-label={label}
      >
        <title>{label}</title>
        <Shape family={family} />
      </svg>
    </span>
  );
}
