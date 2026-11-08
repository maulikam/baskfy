import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ImageResponse } from "next/og";

import { fetchFactsheet } from "@/lib/instrument/fetch";
import { SITE_NAME } from "@/lib/site";

/**
 * The per-instrument OG image — docs/08 §"Instrument factsheet": "OG image generated per
 * instrument."
 *
 * Generated rather than static because the point of the card is the numbers: a shared link to
 * CUPID should preview CUPID's price and 1-year return, not the site logo. It renders from the
 * same factsheet payload the page does, so the card and the page cannot disagree.
 *
 * `next/og` is part of Next 15 — no dependency outside the stack docs/02 locks.
 */
export const alt = "Instrument factor snapshot";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/* The dark half of M36's palette, and the mark's own orange. Literals rather than CSS variables:
   satori resolves no cascade, so an OG card cannot read `globals.css`. */
const BACKGROUND = "#131310";
const FOREGROUND = "#f0efe8";
const MUTED = "#a3a094";
const ACCENT = "#ff6a00";

/** The mark, inlined off disk — an OG route is not guaranteed egress to its own origin. */
const MARK = `data:image/png;base64,${readFileSync(
  join(process.cwd(), "public", "brand", "logo-mark-192.png"),
).toString("base64")}`;
const POSITIVE = "#3fb950";
const NEGATIVE = "#f85149";

/*
 * Two constraints shape the markup below, both of them satori's (the renderer behind `next/og`):
 *
 *   1. Any element with more than one child needs an explicit `display`. A JSX expression beside
 *      literal text counts as two children, so every line here is a single interpolated string.
 *   2. Glyphs outside the bundled font trigger a network font fetch at render time. `₹` is one of
 *      them, and a build or a test with no egress then fails the whole image — so the currency is
 *      spelled out in the label instead.
 */

function valueOf(cells: { label: string; value?: string | number | null }[], label: string) {
  const found = cells.find((cell) => cell.label === label);
  return found?.value ?? null;
}

export default async function Image({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  const sheet = await fetchFactsheet(symbol);

  const oneYear = valueOf(sheet.returns, "1Y");
  const numeric = oneYear === null ? null : Number(oneYear);
  const returnText = numeric === null || Number.isNaN(numeric) ? "—" : `${numeric > 0 ? "+" : ""}${numeric}%`;
  const returnColour = numeric === null || Number.isNaN(numeric) ? MUTED : numeric >= 0 ? POSITIVE : NEGATIVE;

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: BACKGROUND,
          color: FOREGROUND,
          padding: 72,
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 28, color: MUTED }}>{`${sheet.header.exchange}: ${sheet.symbol}`}</div>
          <div style={{ fontSize: 96, fontWeight: 700, lineHeight: 1 }}>{sheet.symbol}</div>
          <div style={{ fontSize: 34, color: MUTED }}>{sheet.header.name}</div>
        </div>

        <div style={{ display: "flex", gap: 72 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: 24, color: MUTED }}>Close (INR)</div>
            <div style={{ fontSize: 56, fontWeight: 600 }}>{String(sheet.header.close_raw ?? "-")}</div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: 24, color: MUTED }}>1-year return</div>
            <div style={{ fontSize: 56, fontWeight: 600, color: returnColour }}>{returnText}</div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: 24, color: MUTED }}>As of</div>
            <div style={{ fontSize: 56, fontWeight: 600 }}>{sheet.as_of}</div>
          </div>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            fontSize: 24,
            color: MUTED,
            borderTop: `4px solid ${ACCENT}`,
            paddingTop: 22,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {/* satori renders raw <img>; next/image is a React component it cannot resolve. */}
            <img src={MARK} alt="" width={38} height={38} />
            <div style={{ display: "flex", color: FOREGROUND }}>{SITE_NAME}</div>
          </div>
          <div style={{ display: "flex" }}>
            Factual analysis of published market data. Not investment advice.
          </div>
        </div>
      </div>
    ),
    size,
  );
}
