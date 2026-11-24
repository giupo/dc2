# DynaCamp web

React + Vite + TypeScript front-end for the campaign engine: theater map
(MapLibre GL on OpenFreeMap tiles), front lines, logistics, ATO and a live
event feed.

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
| helicopter flight | rotary wing attack |

Flights carry the standard direction-of-movement arrow and their callsign;
brigades their designation (e.g. "15th"). Affiliation is relative to the
viewer: the **View as** switch on the map chooses which side is friendly
(rectangle, blue) and which hostile (diamond, red).

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
