import puppeteer from "puppeteer-core";
const [base, path, width, out, y, h] = process.argv.slice(2);
const b = await puppeteer.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: "new" });
const p = await b.newPage();
await p.setViewport({ width: Number(width), height: 1000, deviceScaleFactor: 2 });
await p.goto(base + path, { waitUntil: "networkidle0" });
await p.screenshot({ path: out, clip: { x: 0, y: Number(y), width: Number(width), height: Number(h) } });
await b.close();
console.log("wrote", out);
