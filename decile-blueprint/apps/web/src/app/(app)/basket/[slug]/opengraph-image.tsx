import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ImageResponse } from "next/og";

import { fetchExploreBasket } from "@/lib/explore/fetch";
import { SITE_NAME } from "@/lib/site";

/**
 * Per-basket OG card (AF I.5). Sharing `/basket/[slug]` previews the name, manager and
 * headline return — the same facts the page shows — rather than a generic site tile.
 */

export const alt = "Basket snapshot";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const BACKGROUND = "#131310";
const FOREGROUND = "#f0efe8";
const MUTED = "#a3a094";
const ACCENT = "#ff6a00";
const POSITIVE = "#3fb950";
const NEGATIVE = "#f85149";

const MARK = `data:image/png;base64,${readFileSync(
  join(process.cwd(), "public", "brand", "logo-mark-192.png"),
).toString("base64")}`;

export default async function Image({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  let name = slug;
  let manager = "Baskfy";
  let headlineLabel = "Return";
  let headlineText = "—";
  let headlineColour = MUTED;
  let minAmount = "—";

  try {
    const basket = await fetchExploreBasket(slug);
    name = basket.name;
    manager = basket.manager.name;
    const metrics = basket.metrics;
    if (metrics?.headline_label) headlineLabel = metrics.headline_label;
    if (metrics?.headline_pct != null && metrics.headline_pct !== "") {
      const numeric = Number(metrics.headline_pct);
      if (!Number.isNaN(numeric)) {
        headlineText = `${numeric > 0 ? "+" : ""}${numeric}%`;
        headlineColour = numeric >= 0 ? POSITIVE : NEGATIVE;
      }
    }
    if (metrics?.min_amount != null && metrics.min_amount !== "") {
      minAmount = `INR ${String(metrics.min_amount)}`;
    }
  } catch {
    // A missing basket still renders a card so a broken share does not 500 the crawler.
  }

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
          <div style={{ fontSize: 28, color: MUTED }}>{`Managed by ${manager}`}</div>
          <div style={{ fontSize: 72, fontWeight: 700, lineHeight: 1.05 }}>{name}</div>
        </div>

        <div style={{ display: "flex", gap: 72 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: 24, color: MUTED }}>{headlineLabel}</div>
            <div style={{ fontSize: 56, fontWeight: 600, color: headlineColour }}>{headlineText}</div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ fontSize: 24, color: MUTED }}>Min. amount</div>
            <div style={{ fontSize: 56, fontWeight: 600 }}>{minAmount}</div>
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
