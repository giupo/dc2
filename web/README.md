# DynaCamp web

React + Vite + TypeScript front-end for the campaign engine: theater map
(MapLibre GL on OpenFreeMap tiles), front lines, logistics, ATO, air defence,
intelligence and a live event feed.

## Features

- **Map:**
  - territories, fronts and real roads;
  - battles at the contact point and artillery fire;
  - brigades, convoys, flights, airbases and SAM sites as APP-6 symbols;
  - SAM rings and the IADS network;
  - the named waypoints of the selected flight;
  - the bullseye of the "View as" coalition (target rings), with the cursor's
    BULLS call ("BULLS 085/55": magnetic bearing / range in the chosen units)
    at the bottom left. It is computed in the browser (`bullsLabel` in
    `lib/format.ts`) with the same rule as the engine: great circle from the
    bullseye minus the theatre's `magnetic_variation_deg`.

  Every layer can be toggled in the "☰ Layers" menu on the map, closed by
  default (its state is remembered in the browser), which also holds *Fog of
  war* and the display *Units*.
- **Smooth movement:** positions stream at 5 Hz (`/api/positions/stream`) and
  are interpolated at 60 fps (`src/lib/livePositions.ts`).
  - *How:* entity interpolation, as in game netcode. The map is drawn ~2
    samples (~400 ms) in the past, so there is always a sample before and
    after, and every animation frame places each marker between them.
  - *Performance:* markers use sub-pixel positioning, and the animation never
    goes through React state, so nothing re-renders. The full views refresh
    every 3 s.
  - *Debugging:* `window.__dynacampFeed` exposes the feed in the browser
    console.
- **Header:** one row: clock and time controls, *View as*, and a bar of who
  is winning. The bar splits by faction the weighted standing the engine
  computes (`factions[].standing` in `/api/campaign`, from
  `dynacamp/standing.py`: victory points 35%, own objectives 20%, brigades
  20%, aircraft 15%, provinces 10%, each as the faction's share of the total);
  hovering it (or tapping it) shows each faction's numbers and objectives, and
  the pilots online.
- **Time controls** (header): pause/play and 1×, 10×, 60×, 300×, 900×, 3600×
  (`POST /api/control`). **Space** toggles pause (not while typing in a
  field). They are disabled while a game is live, because time must then run
  1:1, and until the API token is known.
- **API token** (padlock next to the time controls): commands need the token
  printed by `dynacamp serve`.
  - *Getting it:* open the printed link `http://…/#token=…` and the UI stores
    the token in `localStorage` (`dynacamp.token`), then removes it from the
    address bar. Or click the closed padlock and paste the token.
  - *Locked:* the time controls stay disabled until the token is valid
    (checked with `GET /api/auth`). A 401 (say, after a server restart with a
    new token) clears the token and asks again.
  - *Forgetting it:* click the open padlock.
- **Save** (floppy button, left of the padlock): saves the campaign now
  (`POST /api/save`) to the server's `--save` file, and flashes a green tick
  when done.
  - *Tooltip:* the file, the latest save (campaign time, local time, and
    whether it was an autosave, a request or a shutdown) and the autosave
    interval. It refreshes with the campaign view.
  - *Disabled:* when the server runs without `--save`, or while commands are
    locked.
- **ATO orders** (need the token; given as the "View as" coalition's faction,
  validated by the server, which explains refusals in red):
  - *New package* (ATO tab): task, a target the coalition knows (or a point picked
    on the map for CAP/AWACS/tanker), squadron; SEAD and escort are added by
    the planner, TOT is the earliest possible.
  - *Edit route* (flight details): draggable waypoints on the map, "+" on a
    leg adds one, double-click removes one. Flown waypoints, landing and
    attack targets are fixed. The bar over the map shows length against fuel.
  - *TOT and cancel* (package details): −15/−5/+5/+15/+60 min or ASAP while
    every flight is on the ground; cancel the grounded flights (two clicks).
  - Code: `src/components/Planner.tsx` (`PlannerContext` in `App.tsx`).
- **View as + Fog of war:** "View as" (header) is the one place that picks
  whose campaign you look at: the friendly coalition (one button per
  coalition, named after its first faction) for the symbols, the ATO and
  Squadrons tabs, the bullseye and the orders. With "Fog of
  war" (Layers menu) on (the default), every request carries `?faction=`, and the UI shows
  only what that coalition's sensors found (see *Symbology*). The **Intel** tab
  lists the contacts with source and age, and the AWACS radar coverage is
  drawn as a dashed circle. With it off you get the umpire view.
- **ATO tab:** the default is a Falcon/BMS-style list:
  - one row per flight, sorted by takeoff time;
  - columns: 4-digit package number, flight, mission, aircraft, T/O, TOT,
    status;
  - takeoffs within 30 min are highlighted, and players are shown;
  - completed flights on request.

  "Packages" groups the flights by package. Only the "View as" coalition's.
- **Side panel tabs:** when they do not fit, the tab strip scrolls sideways
  (mouse wheel, trackpad or drag), and the active tab is always scrolled into
  view; each panel scrolls on its own.
- **Squadrons tab:** the "View as" coalition's squadrons (insignia,
  aircraft, available/strength, flying, in turnaround) and sortie budget, in
  a tab of their own so the ATO list does not jump as they change.
- **Mission briefing:** opens from a flight's details as a kneeboard card.
  **Print** gives an A4 sheet: the card is a portal on `<body>`, and print CSS
  removes the app and sets the page margins.
- **Squadron insignia:** in the briefing header and in a flight's details.
  Files in `public/insignia/` (served at `/insignia/<file>`), named by the
  scenario's `insignia` for each squadron. Sources and licences are in
  `public/insignia/CREDITS.md`: only free files (public domain, CC0) from
  Wikimedia Commons; rasters are scaled to 256 px WebP.
- **Phones and narrow screens** (under 760 px, `@media` block at the end of
  `styles.css`): the map fills the screen and the header packs into rows
  (clock and speed controls scroll sideways; the standing bar takes the full
  width, tap it for the details). The tabs
  sit under the map: tap one to open its panel as a sheet (45% of the screen,
  and the map shrinks above it), tap it again to close it. The layer list folds
  behind "☰ Layers", as on a desktop. Details and the briefing card take the full width; the
  briefing's flight plan scrolls sideways. The cursor's BULLS readout is hidden
  (no cursor on a touch screen).
- **Events tab:** an event about a package or a flight is clickable. It opens
  the package or flight details, centres the map, and highlights every event
  about the same flight. Air combat events show one button per callsign.
  Packages that are over and gone from the ATO are fetched from the engine's
  archive (`GET /api/ato/packages/{id}`, `GET /api/ato/flights/{id}/package`).
  They are shown as "over (archived)", without orders or briefing. An enemy
  flight is only shown while it is a radar track: the engine does not give
  away the enemy's ATO.
- **Units:** "Units" switches between SI and kts/ft/NM, and the choice is
  remembered in `localStorage`. It applies to briefings, SAM ranges, waypoint
  altitudes and the map scale. The default comes from the campaign
  (`units` in `/api/campaign`).

## Symbology

Units, convoys, flights and airbases use NATO APP-6 / MIL-STD-2525C symbols
rendered by [milsymbol](https://github.com/spatialillusions/milsymbol)
(`src/lib/symbols.ts` maps campaign types to SIDC codes):

| campaign | symbol |
|---|---|
| tank brigade | armour, echelon brigade (X) |
| mech brigade | mechanised infantry, brigade |
| infantry brigade | motorised infantry, brigade |
| artillery group | field artillery, regiment/group (III) |
| convoy | transportation |
| airbase | installation: airport / air base |
| CAP / ESCORT flight | fixed wing fighter (F) |
| CAS / BAI / SEAD flight | fixed wing attack (A) |
| STRIKE flight | bomber (B) |
| helicopter flight | rotary wing attack (transport helicopters: cargo) |
| AWACS | airborne early warning (W) |
| tanker | tanker (K) |
| transport | cargo airlift (C) |
| SAM site / EWR | air defence missile long / medium / short range, radar |

Flights carry the standard direction-of-movement arrow and their callsign;
brigades their designation (e.g. "15th"). Affiliation is relative to the
viewer: the **View as** switch in the header chooses which coalition is friendly
(rectangle, blue) and which hostile (diamond, red). Faction colours on the map
come from the scenario (`color` in `[[factions]]`, served in
`/api/campaign` `factions[].color`).

With fog of war on:
- enemies appear only at their **last known position**;
- a **stale** contact uses the APP-6 *anticipated/suspected* status (dashed
  frame), is faded, and shows the age of the information;
- an **unidentified radar track** uses the yellow *unknown* affiliation with a
  generic aircraft icon, until a friendly aircraft gets close enough to
  identify it.

## Develop

```bash
# 1. engine + API (no DCS needed), from the repository root
uv run dynacamp serve scenarios/syria_m5.toml --no-dcs --compression 600

# 2. front-end with hot reload, in web/
npm install
npm run dev            # http://localhost:5173 (proxies /api to :8080)
```

`DYNACAMP_API=http://host:port npm run dev` points the proxy elsewhere.

## Build

```bash
npm run build          # -> web/dist
```

`dynacamp serve` serves `web/dist` at `/` when it exists, so after a build the
whole thing is one process: http://localhost:8080/.

## API types

`src/api/schema.d.ts` is generated from the FastAPI OpenAPI schema. After
changing the backend API:

```bash
npm run gen:api
```

## Configuration (Vite env)

| variable | default | |
|---|---|---|
| `VITE_MAP_STYLE` | `https://tiles.openfreemap.org/styles/dark` | any MapLibre style URL |
| `VITE_API_BASE` | same origin | API base URL if served elsewhere |

## When the base map fails

The base map (style, tiles, fonts) comes from a third-party host, which ad
blockers (uBlock Origin has been seen blocking `tiles.openfreemap.org/planet`),
strict tracking protection or an outage can cut off. The campaign layers are
inline GeoJSON and HTML markers, so they do not depend on it. `MapView` handles:

| problem | what the map does | notice |
|---|---|---|
| style document refused or not loaded in 10 s | switches to a plain background; province labels hidden (no fonts) | *Base map unavailable* |
| tiles or fonts refused after the style loaded | keeps going; drops our label layers if the fonts fail | *Base map incomplete* |
| WebGL context lost (GPU reset, too many 3D tabs) | effects pause (`map` = null) until MapLibre rebuilds the style, then refill every source | *Map rendering stopped*, with Reload |

Notices can be dismissed. To test: block `*tiles.openfreemap.org*` in DevTools
(Network → Block request URL), or call
`dynacampMap.getCanvas().getContext('webgl2').getExtension('WEBGL_lose_context').loseContext()`
in the console (`restoreContext()` on the same extension brings it back).
