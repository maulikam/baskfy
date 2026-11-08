import puppeteer from "puppeteer-core";
const [base, path, sel] = process.argv.slice(2);
const b = await puppeteer.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: "new" });
const p = await b.newPage();
await p.setViewport({ width: 1440, height: 900 });
await p.goto(base + path, { waitUntil: "networkidle0" });
console.log(await p.evaluate((sel) => [...document.querySelectorAll(sel)].slice(0, 6)
  .map(e => `${e.className.padEnd(14)} "${e.textContent.trim().slice(0,12)}" -> ${getComputedStyle(e).color}`).join("\n"), sel));
await b.close();
