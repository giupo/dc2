// Drive headless Chrome over CDP: console/errors, a probe expression, a screenshot.
// Screenshot + probe: node tools/ui/screenshot.mjs <url> <out.png> [waitMs] [jsFunctionToRunFirst]
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";

const [url, out, waitMs = "12000", click] = process.argv.slice(2);
const dir = new URL(".", import.meta.url).pathname;
const port = 9333;
const chrome = spawn(process.env.CHROME ?? "google-chrome", [
  "--headless=new", "--no-sandbox", `--user-data-dir=${process.env.CHROME_PROFILE ?? "/tmp/dynacamp-chrome-profile"}`, "--no-first-run", "--disable-sync",
  "--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--window-size=1600,950", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws;
for (let i = 0; i < 50 && !ws; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find((t) => t.type === "page");
    if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
  } catch { await sleep(200); }
}
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.consoleAPICalled") logs.push(`console.${msg.params.type}: ${msg.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  if (msg.method === "Runtime.exceptionThrown") logs.push(`EXCEPTION: ${msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text}`);
  if (msg.method === "Log.entryAdded") logs.push(`log.${msg.params.entry.level}: ${msg.params.entry.text} ${msg.params.entry.url ?? ""}`);
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });

await send("Runtime.enable"); await send("Log.enable"); await send("Page.enable");
await send("Page.navigate", { url });
await sleep(Number(waitMs));
if (click) {
  await send("Runtime.evaluate", { expression: `(${click})()`, awaitPromise: true });
  await sleep(2500);
}
const probe = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `(() => {
    const c = document.createElement('canvas');
    return {
      webgl2: !!c.getContext('webgl2'), webgl: !!c.getContext('webgl'),
      canvas: [...document.querySelectorAll('.maplibregl-canvas')].map(x => x.width + 'x' + x.height),
      markers: document.querySelectorAll('.mk').length,
      units: document.querySelectorAll('.mk.unit').length,
      flights: document.querySelectorAll('.mk.flight').length,
      battles: document.querySelectorAll('.mk.battle').length,
      viewport: innerWidth + 'x' + innerHeight, app: document.querySelector('.app')?.getBoundingClientRect().height,
    };
  })()`,
});
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(out, Buffer.from(shot.result.data, "base64"));
console.log(JSON.stringify(probe.result.result.value));
for (const l of logs.slice(0, 25)) console.log(l.slice(0, 300));
ws.close(); chrome.kill();
process.exit(0);
