import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ImageResponse } from "next/og";

import { SITE_NAME, SITE_TAGLINE } from "@/lib/site";

/**
 * The share card for every marketing, content and legal page — Prompt 18 §4 ("OG images").
 *
 * One image for the whole `(marketing)` group, because these pages share one claim and there is
 * nothing per-page worth putting on a card. The *instrument* pages are the opposite case and have
 * their own generator, which puts the symbol's actual numbers on it
 * (`(app)/instruments/[symbol]/opengraph-image.tsx`).
 *
 * `next/og` is part of Next 15 — no dependency outside the stack docs/02 locks. The same two
 * satori constraints apply as on the instrument card: every element with more than one child needs
 * an explicit `display`, and any glyph outside the bundled font would trigger a network font fetch
 * at render time, which fails a build with no egress. There is no `₹` here for that reason.
 */
export const alt = SITE_NAME;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/* The dark half of M36's palette, and the mark's own orange. Literals rather than CSS variables:
   satori resolves no cascade, so an OG card cannot read `globals.css`. */
const BACKGROUND = "#131310";
const FOREGROUND = "#f0efe8";
const MUTED = "#a3a094";
const ACCENT = "#ff6a00";

/**
 * The mark, inlined.
 *
 * Read off disk and base64'd rather than fetched: an OG route renders during the build and in a
 * serverless function, and neither is guaranteed egress to its own origin. `satori` takes a data
 * URI happily. Same reasoning as the no-network-font constraint above.
 */
const MARK = `data:image/png;base64,${readFileSync(
  join(process.cwd(), "public", "brand", "logo-mark-192.png"),
).toString("base64")}`;


export default function Image() {
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
          padding: "72px",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: "28px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "18px" }}>
            {/* satori renders raw <img>; next/image is a React component it cannot resolve. */}
            <img src={MARK} alt="" width={64} height={64} />
            <div style={{ display: "flex", fontSize: 34, letterSpacing: "-0.02em" }}>
              {SITE_NAME}
            </div>
          </div>
          <div style={{ display: "flex", fontSize: 58, lineHeight: 1.15, maxWidth: "980px" }}>
            {SITE_TAGLINE}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
          <div style={{ display: "flex", height: 5, width: 132, background: ACCENT }} />
          <div style={{ display: "flex", fontSize: 26, color: MUTED }}>
            64 published factors · 14 index universes · point-in-time membership
          </div>
          <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
            Not a SEBI-registered investment adviser. Not investment advice.
          </div>
        </div>
      </div>
    ),
    size,
  );
}
