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
export const alt = `${SITE_NAME} — momentum, ranked`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const BACKGROUND = "#0b0d11";
const FOREGROUND = "#f4f5f7";
const MUTED = "#9aa3b2";
const ACCENT = "#4c8dff";

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
          <div style={{ display: "flex", fontSize: 30, color: ACCENT, letterSpacing: "0.04em" }}>
            {SITE_NAME.toUpperCase()}
          </div>
          <div style={{ display: "flex", fontSize: 58, lineHeight: 1.15, maxWidth: "980px" }}>
            {SITE_TAGLINE}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
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
