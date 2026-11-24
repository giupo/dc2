# UI checks (headless Chrome over CDP)

Scripts used to verify the web UI without a desktop browser. They start their
own headless Chrome (`$CHROME`, default `google-chrome`) with an isolated profile (`$CHROME_PROFILE`, default
`/tmp/dynacamp-chrome-profile`), never the user's.

Start a test server on spare ports first (and stop it by PID afterwards):

```bash
uv run dynacamp serve scenarios/syria_m5.toml --no-dcs --port 50777 --api-port 8771 --web-dir web/dist --compression 600 &
node tools/ui/screenshot.mjs http://localhost:8771/ /tmp/ui.png 12000 \
  "() => [...document.querySelectorAll('.tabs button')].find(b => b.textContent.startsWith('ATO')).click()"
```

| script | what |
|---|---|
| `screenshot.mjs` | console errors, a few DOM counters, a PNG; optional JS to run first (click a tab, open a briefing...) |
| `print_pdf.mjs` | same, plus the page printed to PDF with the CSS page size (checks the printable briefing) |
| `smoothness.mjs` | records every moving marker's screen position for 2 s of animation frames: `movingFrames` should be ~all frames, `maxStepPx` small |
