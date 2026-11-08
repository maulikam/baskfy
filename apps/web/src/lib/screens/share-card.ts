"use client";

/**
 * Draw a shareable story card on a canvas — no extra dependency (locked stack).
 * Formats: 9:16 story and 1:1 square (§3.3).
 *
 * The disclaimer is painted into the bitmap (not a caption the host app can crop).
 * Layout reserves the footer first so the top 10 cannot overwrite it — the square
 * format is tight enough that a naive top-down stack collides.
 */

export type ShareAspect = "story" | "square";

export interface ShareCardPayload {
  screenName: string;
  storySentence: string;
  asOf: string;
  rows: readonly {
    rank: number;
    symbol: string;
    name?: string;
    score?: string | null;
    ret?: string | null;
  }[];
}

const DISCLAIMER =
  "Not investment advice. Past performance is not indicative of future results. SEBI-registered advisers only.";

const SIZES: Record<ShareAspect, { w: number; h: number }> = {
  story: { w: 1080, h: 1920 },
  square: { w: 1080, h: 1080 },
};

/** Dark-theme tokens. Orange is the accent bar only — never type (docs/11 contrast). */
const INK = "#e6e8ec";
const MUTED = "#98a1b0";
const ROW_FILL = "#22262e";
const BG = "#14161a";
const BAR = "#ff4f00";

function fillRoundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
  ctx.fill();
}

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (ctx.measureText(next).width <= maxWidth) {
      line = next;
      continue;
    }
    if (line) lines.push(line);
    if (ctx.measureText(word).width <= maxWidth) {
      line = word;
      continue;
    }
    // Hard-break an oversized token so it cannot paint past the card edge.
    let chunk = "";
    for (const ch of word) {
      const trial = chunk + ch;
      if (ctx.measureText(trial).width > maxWidth && chunk) {
        lines.push(chunk);
        chunk = ch;
      } else {
        chunk = trial;
      }
    }
    line = chunk;
  }
  if (line) lines.push(line);
  return lines;
}

function fitText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (maxWidth <= 0) return "";
  if (ctx.measureText(text).width <= maxWidth) return text;
  const ell = "…";
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(text.slice(0, mid) + ell).width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return lo === 0 ? ell : `${text.slice(0, lo)}${ell}`;
}

export function renderShareCard(
  canvas: HTMLCanvasElement,
  aspect: ShareAspect,
  payload: ShareCardPayload,
): void {
  const { w, h } = SIZES[aspect];
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = BAR;
  ctx.fillRect(0, 0, w, 12);

  const pad = Math.round(w * 0.08);
  const innerW = w - pad * 2;

  // Measure the disclaimer first so the row stack cannot collide with it.
  const discSize = Math.round(w * 0.022);
  const discLineH = Math.round(w * 0.028);
  ctx.font = `400 ${discSize}px system-ui, sans-serif`;
  const discLines = wrapText(ctx, DISCLAIMER, innerW);
  const discBlockH = Math.max(1, discLines.length) * discLineH;
  const footerTop = h - pad - discBlockH;
  const rowFloor = footerTop - Math.round(h * 0.02);

  let y = pad + 24;

  ctx.fillStyle = INK;
  ctx.font = `600 ${Math.round(w * 0.045)}px system-ui, sans-serif`;
  ctx.fillText("Baskfy", pad, y);
  y += Math.round(h * (aspect === "square" ? 0.036 : 0.04));

  ctx.fillStyle = INK;
  ctx.font = `600 ${Math.round(w * 0.055)}px system-ui, sans-serif`;
  const titleLines = wrapText(ctx, payload.screenName, innerW);
  for (const line of titleLines.slice(0, 2)) {
    ctx.fillText(line, pad, y);
    y += Math.round(w * 0.06);
  }
  y += Math.round(h * 0.01);

  ctx.fillStyle = MUTED;
  ctx.font = `400 ${Math.round(w * 0.032)}px system-ui, sans-serif`;
  const storyLines = wrapText(ctx, payload.storySentence, innerW);
  const storyMax = aspect === "story" ? 4 : 2;
  for (const line of storyLines.slice(0, storyMax)) {
    ctx.fillText(line, pad, y);
    y += Math.round(w * 0.042);
  }
  y += Math.round(h * 0.018);

  ctx.fillStyle = MUTED;
  ctx.font = `500 ${Math.round(w * 0.028)}px system-ui, sans-serif`;
  ctx.fillText(fitText(ctx, `Fresh as of ${payload.asOf}`, innerW), pad, y);
  y += Math.round(h * (aspect === "square" ? 0.028 : 0.035));

  const top = payload.rows.slice(0, 10);
  const preferred = aspect === "story" ? Math.round(h * 0.055) : Math.round(h * 0.048);
  const minRow = Math.round(w * 0.036);
  const remaining = rowFloor - y;
  const fitted = top.length > 0 ? Math.floor(remaining / top.length) : preferred;
  const rowH = Math.max(minRow, Math.min(preferred, fitted));

  const rankSize = Math.round(w * 0.032);
  const symbolSize = Math.round(w * 0.034);
  const metricSize = Math.round(w * 0.03);
  const radius = aspect === "square" ? 8 : 12;

  for (const row of top) {
    if (y + rowH > rowFloor) break;

    ctx.fillStyle = ROW_FILL;
    fillRoundRect(ctx, pad, y, innerW, rowH - 8, radius);

    const baseline = y + rowH * 0.55;
    ctx.fillStyle = INK;
    ctx.font = `600 ${rankSize}px ui-monospace, monospace`;
    ctx.fillText(String(row.rank).padStart(2, " "), pad + 20, baseline);

    const right = row.ret ?? row.score ?? "";
    let metricLeft = pad + innerW - 20;
    if (right) {
      ctx.fillStyle = MUTED;
      ctx.font = `500 ${metricSize}px ui-monospace, monospace`;
      const tw = ctx.measureText(right).width;
      metricLeft = w - pad - 20 - tw;
      ctx.fillText(right, metricLeft, baseline);
    }

    ctx.fillStyle = INK;
    ctx.font = `600 ${symbolSize}px system-ui, sans-serif`;
    const symbolMaxW = metricLeft - (pad + 70) - 16;
    ctx.fillText(fitText(ctx, row.symbol, symbolMaxW), pad + 70, baseline);

    y += rowH;
  }

  ctx.strokeStyle = "#2c313a";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(pad, footerTop - Math.round(h * 0.012));
  ctx.lineTo(w - pad, footerTop - Math.round(h * 0.012));
  ctx.stroke();

  ctx.fillStyle = MUTED;
  ctx.font = `400 ${discSize}px system-ui, sans-serif`;
  let dy = footerTop;
  for (const line of discLines) {
    ctx.fillText(line, pad, dy);
    dy += discLineH;
  }
}

export async function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Could not encode the share card."));
    }, "image/png");
  });
}
