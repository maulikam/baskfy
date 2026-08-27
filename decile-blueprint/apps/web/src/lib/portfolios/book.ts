/**
 * One book, many boxes.
 *
 * The product already tracks two ledgers: curated baskets you marked as invested
 * (`cb_investment`) and named CSV books you can split into sleeves (`portfolio` /
 * `portfolio_sleeve`). This module composes them into one overview. It does not invent a third
 * table, a live broker feed, or a unit count.
 *
 * A box is one slice of capital, tracked on its own:
 *
 * - **Manager** — a published catalog basket you hold.
 * - **My screen** — a screen you wrote (private SCAN/SCREEN basket, or a `kind=screen` sleeve).
 * - **Manual holdings** — leftover CSV holdings, a private MANUAL basket, or a `kind=manual` sleeve.
 *
 * Sleeve capital is a standing instruction, not a mark. Investment current value is a mark.
 * Adding the two is not a combined NAV, and the UI must not present it as one.
 */

import type { PortfolioNodeOut, SleeveListOut, SleeveOut } from "@baskfy/api-client";

import { addDecimalStrings, roundDecimalString } from "@/lib/portfolios/decimal";

export const BOX_KIND = {
  manager: "manager",
  rule: "rule",
  manual: "manual",
} as const;

export type BoxKind = (typeof BOX_KIND)[keyof typeof BOX_KIND];

export const BOX_KIND_LABEL: Record<BoxKind, string> = {
  manager: "Manager",
  rule: "My screen",
  manual: "Manual holdings",
};

/** The investment fields the book needs — a slice of the list payload, safe to pass to the client. */
export interface BookInvestment {
  id: string;
  basket_name: string;
  status: string;
  basket_source: string | null;
  visibility: string | null;
  snapshot: {
    money_put_in: string;
    current_value: string;
    current_returns_pct: string;
    xirr: string | null;
    xirr_displayable: boolean;
  } | null;
}

export interface BookBox {
  id: string;
  name: string;
  kind: BoxKind;
  /** Money put in, or sleeve capital. Null when the ledger has no rupee figure. */
  capital: string | null;
  /** Live mark, investments only. */
  currentValue: string | null;
  returnsPct: string | null;
  xirr: string | null;
  holdingsCount: number | null;
  valueIsLiveMark: boolean;
  href: string;
  portfolioId?: number;
}

export interface BookSection {
  id: string;
  title: string;
  portfolioId?: number;
  holdingsCount: number;
  boxes: BookBox[];
  /** How deep this book sits in the portfolio tree. 0 for a root and for the baskets section. */
  depth: number;
  /** The books above it, outermost first — so a nested section can say where it lives. */
  ancestors: string[];
  /** Its parent is not the caller's: unreachable through the API, and reported rather than hidden. */
  orphan: boolean;
}

export interface BookTotals {
  boxCount: number;
  /** Sum of investment current values that are live marks. */
  basketValue: string | null;
  /** Sum of sleeve capitals. Not a mark. */
  sleeveCapital: string | null;
  hasLiveMarks: boolean;
}

export interface Book {
  sections: BookSection[];
  totals: BookTotals;
}

export function classifyInvestment(row: {
  basket_source?: string | null;
  visibility?: string | null;
}): BoxKind {
  const visibility = (row.visibility ?? "").toUpperCase();
  const source = (row.basket_source ?? "").toUpperCase();
  if (visibility === "PUBLISHED") return BOX_KIND.manager;
  if (source === "SCREEN" || source === "SCAN") return BOX_KIND.rule;
  if (source === "MANUAL") return BOX_KIND.manual;
  return BOX_KIND.manager;
}

export function classifySleeve(kind: string): BoxKind {
  return kind.toLowerCase() === "manual" ? BOX_KIND.manual : BOX_KIND.rule;
}

/**
 * Add money strings **exactly** — CLAUDE.md house rule 9, "money is `numeric`, never `float`".
 *
 * This used to be `reduce((acc, v) => acc + Number(v), 0).toFixed(2)`, which turned every rupee
 * figure into a double on the way past. At the sizes this page shows — a crore is 1e7, a decade of
 * SIPs is more — that is a silent rounding of somebody's book. The sum is now integer arithmetic
 * on the scaled decimals (see `./decimal`), and the result keeps at least the two places the API
 * stores, and more if the inputs carried more.
 */
export function addMoney(values: Array<string | null | undefined>): string | null {
  const sum = addDecimalStrings(values);
  if (sum === null) return null;
  const fraction = sum.split(".")[1] ?? "";
  return roundDecimalString(sum, Math.max(2, fraction.length));
}

function investmentBox(row: BookInvestment): BookBox | null {
  if (row.status !== "ACTIVE") return null;
  const snap = row.snapshot;
  return {
    id: `inv-${row.id}`,
    name: row.basket_name,
    kind: classifyInvestment(row),
    capital: snap?.money_put_in ?? null,
    currentValue: snap?.current_value ?? null,
    returnsPct: snap?.current_returns_pct ?? null,
    xirr: snap?.xirr_displayable ? (snap.xirr ?? null) : null,
    holdingsCount: null,
    valueIsLiveMark: snap?.current_value != null && snap.current_value !== "",
    href: `/portfolio/${row.id}`,
  };
}

function sleeveBox(portfolio: PortfolioNodeOut, sleeve: SleeveOut): BookBox {
  return {
    id: `sleeve-${portfolio.id}-${sleeve.id}`,
    name: sleeve.name,
    kind: classifySleeve(sleeve.kind),
    capital: sleeve.capital,
    currentValue: null,
    returnsPct: null,
    xirr: null,
    holdingsCount: null,
    valueIsLiveMark: false,
    href: `/portfolios/${portfolio.id}/sleeves`,
    portfolioId: portfolio.id,
  };
}

function leftoverHoldingsBox(portfolio: PortfolioNodeOut): BookBox {
  return {
    id: `holdings-${portfolio.id}`,
    name: portfolio.name,
    kind: BOX_KIND.manual,
    capital: null,
    currentValue: null,
    returnsPct: null,
    xirr: null,
    holdingsCount: portfolio.holdings_count,
    valueIsLiveMark: false,
    href: `/portfolios/${portfolio.id}/rebalance`,
    portfolioId: portfolio.id,
  };
}

export interface BookPortfolioInput {
  portfolio: PortfolioNodeOut;
  sleeves: SleeveListOut | null | undefined;
  /** Where it sits in the forest. Supplied by the caller, which is the thing that walked the tree. */
  depth?: number;
  ancestors?: string[];
  orphan?: boolean;
}

export function composeBook(input: {
  portfolios: BookPortfolioInput[];
  investments: BookInvestment[];
}): Book {
  const sections: BookSection[] = [];

  const investmentBoxes = input.investments
    .map(investmentBox)
    .filter((box): box is BookBox => box !== null);

  if (investmentBoxes.length > 0) {
    sections.push({
      id: "baskets",
      title: "Baskets you hold",
      holdingsCount: investmentBoxes.length,
      boxes: investmentBoxes,
      depth: 0,
      ancestors: [],
      orphan: false,
    });
  }

  for (const { portfolio, sleeves, depth, ancestors, orphan } of input.portfolios) {
    const sleeveRows = sleeves?.sleeves ?? [];
    const boxes =
      sleeveRows.length > 0
        ? sleeveRows.map((sleeve) => sleeveBox(portfolio, sleeve))
        : portfolio.holdings_count > 0
          ? [leftoverHoldingsBox(portfolio)]
          : [];
    sections.push({
      id: `pf-${portfolio.id}`,
      title: portfolio.name,
      portfolioId: portfolio.id,
      holdingsCount: portfolio.holdings_count,
      boxes,
      depth: depth ?? 0,
      ancestors: ancestors ?? [],
      orphan: orphan ?? false,
    });
  }

  const sleeveBoxes = sections.flatMap((section) =>
    section.boxes.filter((box) => box.id.startsWith("sleeve-")),
  );

  return {
    sections,
    totals: {
      boxCount: sections.reduce((count, section) => count + section.boxes.length, 0),
      basketValue: addMoney(investmentBoxes.map((box) => box.currentValue)),
      sleeveCapital: addMoney(sleeveBoxes.map((box) => box.capital)),
      hasLiveMarks: investmentBoxes.some((box) => box.valueIsLiveMark),
    },
  };
}
