/**
 * Layout audit. Answers the questions a screenshot cannot: what is actually
 * overflowing, and by how much.
 */
import puppeteer from "puppeteer-core";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const base = process.argv[2] || "http://127.0.0.1:8480";
const paths = (process.argv[3] || "/performance,/,/regime,/options,/ops").split(",");
const widths = (process.argv[4] || "390,768,1440").split(",").map(Number);

const browser = await puppeteer.launch({ executablePath: CHROME, headless: "new" });
let problems = 0;

for (const w of widths) {
  for (const path of paths) {
    const page = await browser.newPage();
    await page.setViewport({ width: w, height: 1000, deviceScaleFactor: 1 });
    await page.goto(base + path, { waitUntil: "networkidle0" });
    const r = await page.evaluate(() => {
      const de = document.documentElement;
      const vw = de.clientWidth;
      const offenders = [];
      for (const el of document.querySelectorAll("body *")) {
        const b = el.getBoundingClientRect();
        if (b.width === 0 && b.height === 0) continue;
        const over = Math.round(b.right - vw);
        if (over > 1) {
          // an element that scrolls inside its own box is fine, and so is anything
          // INSIDE one: a nav that scrolls horizontally has children past the edge by
          // design. Only report boxes that widen the page itself.
          let inScroller = false;
          for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
            const ox = getComputedStyle(n).overflowX;
            if (ox === "auto" || ox === "scroll") { inScroller = true; break; }
          }
          if (inScroller) continue;
          offenders.push({
            tag: el.tagName.toLowerCase(),
            cls: (el.className && el.className.toString().slice(0, 60)) || "",
            over, w: Math.round(b.width),
          });
        }
      }
      offenders.sort((a, b) => b.over - a.over);
      return { vw, scrollW: de.scrollWidth, offenders: offenders.slice(0, 6) };
    });
    const bad = r.scrollW > r.vw + 1 || r.offenders.length;
    if (bad) problems++;
    console.log(`  ${String(w).padEnd(5)} ${path.padEnd(14)} viewport=${r.vw} scrollWidth=${r.scrollW}` +
                (bad ? "  <-- OVERFLOW" : "  ok"));
    for (const o of r.offenders)
      console.log(`         +${o.over}px  <${o.tag} class="${o.cls}"> w=${o.w}`);
    await page.close();
  }
}
await browser.close();
console.log(problems ? `\n  ${problems} page/width combinations overflow` : "\n  no overflow at any width");
