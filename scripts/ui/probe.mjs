import puppeteer from "puppeteer-core";
const b = await puppeteer.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: "new" });
const p = await b.newPage();
await p.setViewport({ width: 390, height: 900 });
await p.goto("http://127.0.0.1:8484/regime", { waitUntil: "networkidle0" });
console.log(await p.evaluate(() => {
  const t = [...document.querySelectorAll("table")].find(x => x.getBoundingClientRect().width > 400);
  if (!t) return "no wide table";
  const cs = getComputedStyle(t);
  const chain = [];
  for (let n = t; n && n !== document.body; n = n.parentElement) {
    const c = getComputedStyle(n);
    chain.push(`${n.tagName.toLowerCase()}.${(n.className||"").toString().split(" ")[0]} ` +
      `w=${Math.round(n.getBoundingClientRect().width)} display=${c.display} ` +
      `minW=${c.minWidth} maxW=${c.maxWidth} width=${c.width} overflowX=${c.overflowX}`);
  }
  return chain.join("\n");
}));
await b.close();
