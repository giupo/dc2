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
  - the named waypoints of the selected flight.

  Every layer can be toggled.
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
- **Time controls** (header): pause/play and 1×, 10×, 60×, 300×, 900×, 3600×
  (`POST /api/control`). They are disabled while a game is live, because time
  must then run 1:1.
- **View as + Fog of war:** "View as" picks the friendly side. With "Fog of
  war" on (the default), every request carries `?faction=`, and the UI shows
  only what that side's sensors found (see *Symbology*). The **Intel** tab
  lists the contacts with source and age, and the AWACS radar coverage is
  drawn as a dashed circle. With it off you get the umpire view.
- **ATO tab:** the default is a Falcon/BMS-style list:
  - one row per flight, sorted by takeoff time;
  - columns: 4-digit package number, flight, mission, aircraft, T/O, TOT,
    status;
  - takeoffs within 30 min are highlighted, and players are shown;
  - completed flights on request.

  "Packages" groups the flights by package.
- **Mission briefing:** opens from a flight's details as a kneeboard card.
  **Print** gives an A4 sheet: the card is a portal on `<body>`, and print CSS
  removes the app and sets the page margins.
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
viewer: the **View as** switch on the map chooses which side is friendly
(rectangle, blue) and which hostile (diamond, red).

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
