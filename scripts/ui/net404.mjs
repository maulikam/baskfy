import puppeteer from "puppeteer-core";
const b = await puppeteer.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: "new" });
const p = await b.newPage();
const bad = [];
p.on("response", r => { if (r.status() >= 400) bad.push(`${r.status()} ${r.url()}`); });
await p.goto(process.argv[2] + "/performance", { waitUntil: "networkidle0" });
console.log("  failed requests:", bad.length ? bad : "none");
await b.close();
