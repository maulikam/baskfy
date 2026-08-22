/**
 * The logo, from the vector Maulik supplied, into everything that needs it (M38).
 *
 *   node scripts/build-brand-svg.mjs
 *
 * Stage one of two. This produces the two things the rest of the brand pipeline reads:
 *
 *   public/brand/logo.svg      the mark itself, optimised and squared, served to browsers
 *   brand-src/mark-master.png  a 4096px transparent raster, which `build_brand.py` cuts up
 *
 * WHY THE SVG REPLACED THE PNG AS THE SOURCE
 * ------------------------------------------
 * M37 built the icon set from a 4096px raster on a white background, which meant reconstructing
 * an alpha channel by projecting every pixel onto the line from white toward each brand colour.
 * That worked, and it was solving a problem the vector does not have: the channels between the
 * ribbons are *actually* empty here, so the mark sits correctly on the dark theme without anything
 * being inferred. The matte is gone with the raster it existed for.
 *
 * The trade is one artefact, and it is worth knowing about. The file is an autotrace, and where
 * two ribbons overlap the original art carries a soft translucent shadow that a trace cannot
 * express — so it approximates each one with a hard-edged block. At 900px those blocks are
 * visible. At every size this product actually renders the mark (28px in the header, 180px for
 * the touch icon, 64px on a share card) they are sub-pixel. Recorded in
 * `docs/DECISIONS-MERGE.md` §M38.2 rather than repaired, because editing paths out of somebody's
 * logo on a guess is how a logo quietly stops being the logo.
 *
 * WHY THE COORDINATES ARE ROUNDED TO ONE DECIMAL
 * ----------------------------------------------
 * Measured, not assumed. Against the untouched file rendered at 420px: two decimals differ in 209
 * pixels of 176,400 and one decimal in the same 209 (mean channel error 0.03), while whole numbers
 * differ in 2,131 with a mean of 0.19 — visible edge wobble on the 1024px export. One decimal on a
 * 1833-unit viewBox is 0.005% precision and takes the file from 170 KB to 131 KB, 38 KB gzipped.
 *
 * The `<g>` seam layer is NOT dropped, though removing it saves another 29 KB: it carries the
 * anti-aliasing between adjacent fills, and without it 973 pixels at 420px shift by up to 103.
 */

import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, "..");
const SOURCE = resolve(WEB, "..", "..", "..", "logo.svg");
const SVG_OUT = join(WEB, "public", "brand", "logo.svg");
const RASTER_OUT = join(WEB, "brand-src", "mark-master.png");

/** The master's edge, in pixels. Every exported size is a downsample of this. */
const MASTER = 4096;

/** Air around the art in the square *icon* master. The served SVG is cropped tight; see `crop`. */
const PAD = 0.04;

/** Decimal places kept in path data. See the header. */
const PRECISION = 1;

function optimise(svg) {
  return svg
    .replace(/<\?xml[^>]*\?>\s*/, "")
    .replace(/<!DOCTYPE[^>]*>\s*/, "")
    .replace(/ version="1\.1"/, "")
    .replace(/ vector-effect="non-scaling-stroke"/g, "")
    .replace(/-?\d+\.\d+/g, (n) =>
      Number(n).toFixed(PRECISION).replace(/\.?0+$/, ""),
    )
    .replace(/\s*\n\s*/g, " ")
    .replace(/\s+/g, " ")
    .replace(/" \/>/g, '"/>')
    .replace(/> </g, "><")
    .trim();
}

const round = (v) => Number(v.toFixed(PRECISION));

/**
 * Crop the viewBox to the art's real bounds, keeping its own proportions.
 *
 * The supplied file draws its mark inside a canvas the ink does not fill, and the mark is not
 * square — the current one is a portrait lettermark at roughly 0.69:1. **This is the file the
 * browser gets**, so it keeps that shape: a portrait mark stretched into a square box beside a
 * line of type has to shrink to fit the width it does not need, which is what made the previous
 * one look undersized. The header sizes it by height and lets the width follow.
 */
function crop(svg, box) {
  return svg.replace(
    /viewBox="[^"]*"/,
    `viewBox="${round(box.x)} ${round(box.y)} ${round(box.width)} ${round(box.height)}"`,
  );
}

/**
 * The same art centred in a padded square — the shape an *icon* has to be.
 *
 * Only the raster master is built from this. A favicon, a touch icon and a share card are all
 * square by definition and are cropped and masked by an operating system, so they need both the
 * square canvas and the margin; `PAD` exists for them and for nothing else.
 */
function square(svg, box) {
  const side = Math.max(box.width, box.height) * (1 + 2 * PAD);
  const x = box.x + box.width / 2 - side / 2;
  const y = box.y + box.height / 2 - side / 2;
  return svg.replace(
    /viewBox="[^"]*"/,
    `viewBox="${round(x)} ${round(y)} ${round(side)} ${round(side)}"`,
  );
}

const browser = await chromium.launch();
try {
  const optimised = optimise(readFileSync(SOURCE, "utf8"));

  // Chromium's own `getBBox()` is the only thing here that actually knows where the paths are.
  const measure = await browser.newPage();
  await measure.setContent(`<body style="margin:0">${optimised}</body>`);
  const box = await measure.evaluate(() => {
    const svg = document.querySelector("svg");
    const { x, y, width, height } = svg.getBBox();
    return { x, y, width, height };
  });
  await measure.close();

  const cropped = crop(optimised, box);
  const squared = square(optimised, box);
  mkdirSync(dirname(SVG_OUT), { recursive: true });
  mkdirSync(dirname(RASTER_OUT), { recursive: true });
  writeFileSync(SVG_OUT, `${cropped}\n`);

  const page = await browser.newPage({
    viewport: { width: MASTER, height: MASTER },
    deviceScaleFactor: 1,
  });
  await page.setContent(
    `<style>html,body{margin:0;background:transparent}
     svg{width:${MASTER}px;height:${MASTER}px;display:block}</style>${squared}`,
  );
  writeFileSync(RASTER_OUT, await page.screenshot({ omitBackground: true, type: "png" }));
  await page.close();

  const gzipped = (await import("node:zlib")).gzipSync(cropped, { level: 9 }).length;
  const ratio = (box.width / box.height).toFixed(3);
  console.log(`ink bbox      ${Math.round(box.width)} x ${Math.round(box.height)}  (${ratio}:1)`);
  console.log(`logo.svg      ${cropped.length.toLocaleString()} bytes (${gzipped.toLocaleString()} gzipped)`);
  console.log(`mark-master   ${MASTER}px transparent, squared`);
  console.log(`ASPECT        ${ratio}  <- wordmark.tsx must match`);
} finally {
  await browser.close();
}
