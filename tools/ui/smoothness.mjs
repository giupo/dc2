// Drive headless Chrome over CDP: console/errors, a probe expression, a screenshot.
// Measure marker motion per animation frame (movingFrames, maxStepPx): node tools/ui/smoothness.mjs <url> <out.png> [waitMs]
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";

const [url, out, waitMs = "12000", click] = process.argv.slice(2);
const dir = new URL(".", import.meta.url).pathname;
const port = 9333;
const chrome = spawn("google-chrome", [
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
const probeRaw = await send("Runtime.evaluate", {
  returnByValue: true, awaitPromise: true,
  expression: `(async () => {
    const pick = () => [...document.querySelectorAll('.maplibregl-marker')].filter(m => m.querySelector('.mk.flight, .mk.unit, .mk.convoy'));
    const els = pick();
    const pos = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top]; };
    const tracks = els.map(() => []);
    const t0 = performance.now(); let frames = 0;
    await new Promise(res => { const f = () => { frames++; els.forEach((e, i) => tracks[i].push(pos(e))); performance.now() - t0 < 2000 ? requestAnimationFrame(f) : res(); }; requestAnimationFrame(f); });
    return tracks.map((tr, ix) => {
      const steps = []; for (let k = 1; k < tr.length; k++) if (tr[k] && tr[k-1]) steps.push(Math.hypot(tr[k][0]-tr[k-1][0], tr[k][1]-tr[k-1][1]));
      const moving = steps.filter(x => x > 0.01).length;
      return { cls: els[ix].querySelector(".mk").className.split(" ")[1], frames, movingFrames: moving, maxStepPx: +Math.max(0, ...steps).toFixed(2), meanStepPx: +(steps.reduce((a,b)=>a+b,0)/Math.max(1,moving)).toFixed(2) };
    });
  })()`,
});
const probe = { result: { result: { value: probeRaw.result.result.value.filter(x => x.movingFrames > 0) } } };
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(out, Buffer.from(shot.result.data, "base64"));
console.log(JSON.stringify(probe.result.result.value));
for (const l of logs.slice(0, 25)) console.log(l.slice(0, 300));
ws.close(); chrome.kill();
process.exit(0);
