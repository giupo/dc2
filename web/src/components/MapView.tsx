import { LngLatBounds, Map as MLMap, Marker, NavigationControl, ScaleControl, setWorkerUrl, type GeoJSONSource, type LngLatLike, type StyleSpecification } from "maplibre-gl";
// MapLibre loads its web worker from a file next to its own module, which does not
// exist once bundled: let Vite ship the worker as an asset and point MapLibre at it.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Feature, FeatureCollection } from "geojson";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import type { Briefing, Flight } from "../api/client";
import { ageLabel, battleTitle, bullsLabel, factionColor, fmtAlt, intelLabel, levelColor, coalitionOf, type Units } from "../lib/format";
import { useLiveFeed, type LiveFeed } from "../lib/livePositions";
import { affiliation, airbaseSidc, airSidc, convoySidc, designation, knownStatus, samSidc, symbol, unitSidc } from "../lib/symbols";
import type { CampaignData } from "../lib/useCampaign";
import type { Layers, Selection } from "../lib/selection";
import { PlannerContext, useRouteEditor } from "./Planner";

setWorkerUrl(workerUrl);

const STYLE: string = import.meta.env.VITE_MAP_STYLE ?? "https://tiles.openfreemap.org/styles/dark";
const FONT = ["Noto Sans Regular"];
const FIRE_FADE_MS = 12_000;
/** Host serving the base map (style, tiles, fonts): named in the notices. */
const BASEMAP_HOST = (() => {
  try {
    return new URL(STYLE, window.location.href).host;
  } catch {
    return STYLE;
  }
})();
/** Plain background used when the base map style cannot be loaded at all. No
 * glyphs: the label layers are skipped, everything else still works offline. */
const FALLBACK_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "background", type: "background", paint: { "background-color": "#141b27" } }],
};
/** Give up on the base map style after this long (a hung request, not a refused one). */
const STYLE_TIMEOUT_MS = 10_000;
/** Our own layers that draw text: dropped when the fonts cannot be loaded. */
const LABEL_LAYERS = ["wp-label", "province-label"];

/** Something wrong with the map rendering, shown as a notice over it.
 *
 * - ``fallback``: the base map style did not load, a plain background replaces it;
 * - ``tiles``: the style loaded but its tiles or fonts are refused (ad blocker...);
 * - ``webgl``: the browser took the WebGL context away, the map stops drawing.
 */
type MapProblem = "fallback" | "tiles" | "webgl";

type LL = number[] | null | undefined;
const lngLat = (ll: LL): [number, number] | null => (ll ? [ll[1], ll[0]] : null);
const line = (lls: number[][] | null | undefined) => (lls ? lls.map((p) => [p[1], p[0]]) : []);
const fc = (features: Feature[]): FeatureCollection => ({ type: "FeatureCollection", features });

interface Props {
  data: CampaignData;
  layers: Layers;
  selected: Selection;
  onSelect: (s: Selection) => void;
  focus?: { lngLat: [number, number]; seq: number };
  /** Side seen as friendly (APP-6 affiliation is relative to the viewer). */
  viewpoint: string;
  /** Mission package of the selected flight: its named waypoints are drawn. */
  briefing?: Briefing;
  /** Display units: also the scale bar's. */
  units: Units;
}

export function MapView({ data, layers, selected, onSelect, focus, viewpoint, briefing, units }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<MLMap | null>(null);
  const fitted = useRef(false);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const { campaign } = data;

  const scaleRef = useRef<ScaleControl | null>(null);
  useEffect(() => scaleRef.current?.setUnit(units === "imperial" ? "nautical" : "metric"), [units, map]);
  const [problems, setProblems] = useState<Set<MapProblem>>(new Set());
  const [dismissed, setDismissed] = useState<Set<MapProblem>>(new Set());
  const report = (p: MapProblem, on = true) =>
    setProblems((s) => {
      if (s.has(p) === on) return s;
      const next = new Set(s);
      if (on) next.add(p);
      else next.delete(p);
      return next;
    });
  // ---- create the map once
  useEffect(() => {
    if (!container.current) return;
    const m = new MLMap({ container: container.current, style: STYLE, center: [38, 36], zoom: 6, attributionControl: { compact: true } });
    m.addControl(new NavigationControl({ visualizePitch: false }), "top-right");
    const scale = new ScaleControl({ unit: "metric" });
    scaleRef.current = scale;
    m.addControl(scale, "bottom-right");

    // ---- resilience: the base map comes from a third-party host that ad
    // blockers, firewalls or an outage can cut off. Our own layers are inline
    // GeoJSON (no URL), so every failed request is the base map's.
    // ``style.load`` = the style document arrived; ``load`` additionally waits
    // for its sources, so a hung tile server would hold our layers back forever.
    let styleParsed = false;
    let loaded = false;
    let fellBack = false;
    const fallBack = () => {
      if (loaded || fellBack) return;
      fellBack = true;
      report("fallback");
      m.setStyle(FALLBACK_STYLE);
    };
    const styleTimer = window.setTimeout(fallBack, STYLE_TIMEOUT_MS);
    m.on("style.load", () => (styleParsed = true));
    m.on("error", (e) => {
      // Failed requests are AJAXErrors, which carry the URL.
      const url = (e.error as { url?: string } | undefined)?.url;
      if (!url) return;
      if (!styleParsed) {
        fallBack();
        return;
      }
      report("tiles");
      // Text needs the fonts: a geojson tile whose glyphs fail is dropped whole,
      // dots and all, so give up on our labels rather than lose the symbols.
      if (url.includes("/fonts/") || url.includes("glyph")) {
        for (const id of LABEL_LAYERS) if (m.getLayer(id)) m.removeLayer(id);
      }
    });
    // A lost context destroys the style (map.style = null) until MapLibre rebuilds
    // it from a snapshot on restore: hide the map from the effects meanwhile, and
    // hand it back once the rebuilt style is in, so they refill every source.
    m.on("webglcontextlost", () => {
      report("webgl");
      setMap(null);
    });
    m.on("webglcontextrestored", () => {
      report("webgl", false);
      if (!loaded) return;
      let back = false;
      const ready = () => {
        if (back) return;
        back = true;
        setMap(m);
      };
      m.once("style.load", ready);
      m.once("idle", ready);
    });

    m.on("load", () => {
      loaded = true;
      window.clearTimeout(styleTimer);
      const hasGlyphs = !!m.getStyle().glyphs;
      for (const id of ["territory", "links", "fronts", "routes", "flightpaths", "provinces", "fires", "ad-rings", "iads", "wps"]) {
        m.addSource(id, { type: "geojson", data: fc([]) });
      }
      // Territories go under the base map's water: the sea hides the part of a
      // coastal province's cell that lies offshore, so coasts clip them for free.
      const water = m.getStyle().layers.find((l) => l.type === "fill" && l.id.includes("water"))?.id;
      m.addLayer({ id: "territory-fill", type: "fill", source: "territory", paint: { "fill-color": ["get", "color"], "fill-opacity": ["case", ["get", "selected"], 0.42, 0.22] } }, water);
      m.addLayer({ id: "territory-line", type: "line", source: "territory", paint: { "line-color": "#0b0f17", "line-opacity": 0.5, "line-width": 0.8 } }, water);
      m.addLayer({ id: "links-road", type: "line", source: "links", filter: ["!=", ["get", "kind"], "track"], paint: { "line-color": "#8b93a1", "line-width": ["case", ["==", ["get", "kind"], "rail"], 2, 1.3], "line-opacity": 0.7 } });
      m.addLayer({ id: "links-track", type: "line", source: "links", filter: ["==", ["get", "kind"], "track"], paint: { "line-color": "#8b93a1", "line-width": 1.1, "line-opacity": 0.6, "line-dasharray": [3, 2] } });
      m.addLayer({ id: "front-glow", type: "line", source: "fronts", layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#f59e0b", "line-width": 10, "line-opacity": 0.22, "line-blur": 4 } }, water);
      m.addLayer({ id: "front", type: "line", source: "fronts", layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#fcd34d", "line-width": 2.6 } }, water);
      m.addLayer({ id: "routes", type: "line", source: "routes", paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "selected"], 3, 1.4], "line-opacity": ["case", ["get", "selected"], 1, 0.55], "line-dasharray": [2, 1.5] } });
      m.addLayer({ id: "flightpaths", type: "line", source: "flightpaths", paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "selected"], 2.5, 1], "line-opacity": ["case", ["get", "selected"], 0.95, 0.4], "line-dasharray": [1, 2] } });
      // Air defence: engagement rings (solid when the radar is on), detection ring
      // of the selected site, and IADS links from early-warning radars to sites.
      m.addLayer({ id: "ad-ring-fill", type: "fill", source: "ad-rings", filter: ["==", ["get", "ring"], "range"], paint: { "fill-color": ["get", "color"], "fill-opacity": ["case", ["get", "active"], 0.06, 0.0] } }, water);
      m.addLayer({ id: "ad-ring", type: "line", source: "ad-rings", filter: ["==", ["get", "ring"], "range"], paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "selected"], 2.2, 1.1], "line-opacity": ["case", ["get", "active"], 0.75, 0.35] } });
      m.addLayer({ id: "ad-ring-inactive", type: "line", source: "ad-rings", filter: ["all", ["==", ["get", "ring"], "range"], ["!", ["get", "active"]]], paint: { "line-color": "#9ca3af", "line-width": 1, "line-opacity": 0.5, "line-dasharray": [2, 2] } });
      m.addLayer({ id: "ad-detect", type: "line", source: "ad-rings", filter: ["==", ["get", "ring"], "detect"], paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.45, "line-dasharray": [1, 3] } });
      m.addLayer({ id: "sensor-ring", type: "line", source: "ad-rings", filter: ["==", ["get", "ring"], "awacs"], paint: { "line-color": "#67e8f9", "line-width": 1.5, "line-opacity": 0.7, "line-dasharray": [4, 3] } });
      m.addLayer({ id: "iads-link", type: "line", source: "iads", paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.5, "line-dasharray": [0.5, 2] } });
      m.addLayer({ id: "fire-arc", type: "line", source: "fires", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#f97316", "line-width": 2, "line-opacity": ["*", 0.9, ["get", "opacity"]], "line-dasharray": [2, 1.2] } });
      m.addLayer({ id: "fire-impact", type: "circle", source: "fires", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": ["+", 4, ["*", 8, ["-", 1, ["get", "opacity"]]]], "circle-color": "#fb923c", "circle-blur": 0.4, "circle-opacity": ["*", 0.85, ["get", "opacity"]], "circle-stroke-color": "#fed7aa", "circle-stroke-width": 1.5, "circle-stroke-opacity": ["get", "opacity"] } });
      m.addLayer({ id: "wp-dot", type: "circle", source: "wps", paint: { "circle-radius": 5, "circle-color": "#0b0f17", "circle-stroke-color": "#fcd34d", "circle-stroke-width": 2 } });
      if (hasGlyphs) m.addLayer({ id: "wp-label", type: "symbol", source: "wps", layout: { "text-field": ["get", "label"], "text-font": FONT, "text-size": 11, "text-offset": [0, -1.2], "text-anchor": "bottom", "text-allow-overlap": true }, paint: { "text-color": "#fde68a", "text-halo-color": "#05070b", "text-halo-width": 1.5 } });
      m.addLayer({ id: "province-dot", type: "circle", source: "provinces", paint: { "circle-radius": ["+", 3, ["*", 0.6, ["get", "vp"]]], "circle-color": ["get", "color"], "circle-stroke-color": ["case", ["get", "depot"], "#facc15", "#0b0f17"], "circle-stroke-width": ["case", ["get", "depot"], 2.5, 1] } });
      if (hasGlyphs) m.addLayer({ id: "province-label", type: "symbol", source: "provinces", layout: { "text-field": ["get", "name"], "text-font": FONT, "text-size": 12, "text-offset": [0, 1.2], "text-anchor": "top", "text-allow-overlap": false }, paint: { "text-color": "#e5e7eb", "text-halo-color": "#05070b", "text-halo-width": 1.4 } });

      const pick = (layer: string, kind: NonNullable<Selection>["kind"]) => {
        m.on("click", layer, (e: { features?: { properties?: Record<string, unknown> }[] }) => {
          const id = e.features?.[0]?.properties?.id;
          if (id) selectRef.current({ kind, id: String(id) });
        });
        m.on("mouseenter", layer, () => (m.getCanvas().style.cursor = "pointer"));
        m.on("mouseleave", layer, () => (m.getCanvas().style.cursor = ""));
      };
      pick("territory-fill", "province");
      setMap(m);
      // Handle for debugging / automated screenshots (window.dynacampMap.jumpTo(...)).
      (window as unknown as { dynacampMap?: MLMap }).dynacampMap = m;
    });
    return () => {
      window.clearTimeout(styleTimer);
      m.remove();
    };
  }, []);

  // ---- fit to the theater once geometry is known
  useEffect(() => {
    if (!map || !data.map || fitted.current) return;
    const pts = data.map.provinces.map((p) => lngLat(p.ll)).filter(Boolean) as [number, number][];
    if (!pts.length) return;
    const b = pts.reduce((acc, p) => acc.extend(p), new LngLatBounds(pts[0], pts[0]));
    map.fitBounds(b, { padding: 60, duration: 0 });
    fitted.current = true;
  }, [map, data.map]);

  // ---- fly to focus requests
  useEffect(() => {
    if (map && focus) map.flyTo({ center: focus.lngLat, zoom: Math.max(map.getZoom(), 8), speed: 1.4 });
  }, [map, focus]);

  // ---- GeoJSON layers
  useEffect(() => {
    if (!map || !data.map) return;
    const src = (id: string) => map.getSource(id) as GeoJSONSource | undefined;
    const sel = (kind: string, id: string) => selected?.kind === kind && selected.id === id;

    src("territory")?.setData(
      fc(
        data.map.provinces.map((p) => ({
          type: "Feature",
          properties: { id: p.id, color: factionColor(campaign, p.controller), selected: sel("province", p.id) },
          geometry: { type: "Polygon", coordinates: [[...line(p.territory.ll), line(p.territory.ll)[0]].filter(Boolean)] },
        })),
      ),
    );
    src("provinces")?.setData(
      fc(
        data.map.provinces.map((p) => ({
          type: "Feature",
          properties: { id: p.id, name: p.name, vp: p.victory_points, depot: p.depot, color: factionColor(campaign, p.controller) },
          geometry: { type: "Point", coordinates: lngLat(p.ll) ?? [0, 0] },
        })),
      ),
    );
    src("links")?.setData(
      fc(data.map.links.map((l) => ({ type: "Feature", properties: { kind: l.kind, river: l.river }, geometry: { type: "LineString", coordinates: line(l.ll) } }))),
    );
    src("fronts")?.setData(
      fc((data.fronts?.lines ?? []).map((f) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: line(f.ll) } }))),
    );
    const routes: Feature[] = [];
    for (const d of data.forces?.divisions ?? []) {
      if (d.route?.ll) routes.push({ type: "Feature", properties: { color: factionColor(campaign, d.faction), selected: sel("division", d.id) }, geometry: { type: "LineString", coordinates: line(d.route.ll) } });
    }
    for (const c of data.logistics?.convoys ?? []) {
      if (c.route?.ll && sel("convoy", c.id)) routes.push({ type: "Feature", properties: { color: "#facc15", selected: true }, geometry: { type: "LineString", coordinates: line(c.route.ll) } });
    }
    src("routes")?.setData(fc(routes));
    const paths: Feature[] = [];
    for (const p of data.ato?.packages ?? []) {
      for (const f of p.flights) {
        if (f.status === "done" || f.status === "cancelled" || !f.waypoints.ll) continue;
        const selectedPath = sel("flight", f.id) || sel("package", p.id);
        paths.push({ type: "Feature", properties: { color: factionColor(campaign, f.faction), selected: selectedPath }, geometry: { type: "LineString", coordinates: line(f.waypoints.ll) } });
      }
    }
    src("flightpaths")?.setData(fc(paths));

    const rings: Feature[] = [];
    for (const u of data.airDefense?.sites ?? []) {
      if (!u.ll) continue;
      const color = factionColor(campaign, u.faction);
      const isSel = sel("sam", u.id);
      const active = u.status === "active";
      if (u.range_km > 0) {
        rings.push({ type: "Feature", properties: { ring: "range", color, active, selected: isSel }, geometry: { type: "Polygon", coordinates: [circle(u.ll, u.range_km * 1000)] } });
      }
      if (isSel || u.role === "ewr") {
        rings.push({ type: "Feature", properties: { ring: "detect", color }, geometry: { type: "LineString", coordinates: circle(u.ll, u.detection_km * 1000) } });
      }
    }
    // Fog of war: the reach of the viewer's AWACS radars.
    for (const s of data.intel?.sensors ?? []) {
      if (s.kind !== "awacs" || !s.ll) continue;
      rings.push({ type: "Feature", properties: { ring: "awacs" }, geometry: { type: "LineString", coordinates: circle(s.ll, s.range_km * 1000) } });
    }
    src("ad-rings")?.setData(fc(rings));
    src("iads")?.setData(
      fc((data.airDefense?.links ?? []).map((l) => ({ type: "Feature", properties: { color: factionColor(campaign, l.faction) }, geometry: { type: "LineString", coordinates: line(l.ll) } }))),
    );
  }, [map, data.map, data.fronts, data.forces, data.logistics, data.ato, data.airDefense, data.intel, campaign, selected]);

  // ---- artillery fire missions: remembered client side and faded in *real* time,
  // so they stay visible a few seconds whatever the time compression.
  const seenFires = useRef(new Map<string, { coords: number[][]; color: string; seen: number }>());
  const lastFireSeq = useRef(-1);
  useEffect(() => {
    // Every mission comes through the event stream; draw it between the
    // shooter's and the target's current positions.
    const now = performance.now();
    const where = new Map((data.forces?.divisions ?? []).map((d) => [d.id, d.ll]));
    for (const e of data.events) {
      if (e.seq <= lastFireSeq.current) continue;
      lastFireSeq.current = e.seq;
      if (e.kind !== "artillery_fire") continue;
      const ev = e.data as { shooter: string; target: string; faction: string };
      const a = lngLat(where.get(ev.shooter));
      const b = lngLat(where.get(ev.target));
      if (a && b) seenFires.current.set(`${e.seq}`, { coords: [a, b], color: factionColor(campaign, ev.faction), seen: now });
    }
  }, [data.events, data.forces, campaign]);
  useEffect(() => {
    if (!map) return;
    const timer = window.setInterval(() => {
      const now = performance.now();
      const features: Feature[] = [];
      for (const [id, f] of seenFires.current) {
        const age = (now - f.seen) / FIRE_FADE_MS;
        if (age >= 1) {
          seenFires.current.delete(id);
          continue;
        }
        const opacity = 1 - age;
        features.push({ type: "Feature", properties: { color: f.color, opacity }, geometry: { type: "LineString", coordinates: f.coords } });
        features.push({ type: "Feature", properties: { opacity }, geometry: { type: "Point", coordinates: f.coords[1] } });
      }
      (map.getSource("fires") as GeoJSONSource | undefined)?.setData(fc(features));
    }, 250);
    return () => window.clearInterval(timer);
  }, [map]);

  // ---- named waypoints of the selected flight
  useEffect(() => {
    if (!map) return;
    const feats: Feature[] = (briefing?.waypoints ?? []).flatMap((w) =>
      w.ll ? [{ type: "Feature", properties: { label: `WP${w.index} ${w.name} ${w.eta.label.slice(3)}${w.alt_m ? ` ${fmtAlt(w.alt_m, units)}` : ""}` }, geometry: { type: "Point", coordinates: [w.ll[1], w.ll[0]] } } as Feature] : [],
    );
    (map.getSource("wps") as GeoJSONSource | undefined)?.setData(fc(feats));
  }, [map, briefing, units]);

  // ---- layer visibility
  useEffect(() => {
    if (!map) return;
    const vis = (ids: string[], on: boolean) => ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", on ? "visible" : "none"));
    vis(["territory-fill", "territory-line"], layers.territory);
    vis(["links-road", "links-track"], layers.links);
    vis(["front", "front-glow"], layers.fronts);
    vis(["fire-arc", "fire-impact"], layers.fires);
    vis(["routes"], layers.divisions);
    vis(["flightpaths"], layers.air);
    vis(["ad-ring-fill", "ad-ring", "ad-ring-inactive", "ad-detect", "iads-link"], layers.airDefense);
    vis(["province-label"], layers.labels);
    vis(["sensor-ring"], layers.air);
  }, [map, layers]);

  // ---- HTML markers: NATO APP-6 symbols (milsymbol)
  const aff = (faction: string | null | undefined) => affiliation(coalitionOf(campaign, faction), viewpoint);

  const divisionMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.divisions) return [];
    // Brigades at the same spot (same province, not on the road) are fanned out side by side.
    const spot = (pos: number[]) => `${Math.round(pos[0] / 500)},${Math.round(pos[1] / 500)}`;
    const stacks = new Map<string, string[]>();
    for (const d of data.forces?.divisions ?? []) {
      stacks.set(spot(d.pos), [...(stacks.get(spot(d.pos)) ?? []), d.id]);
    }
    const SPREAD = 34;
    return (data.forces?.divisions ?? []).flatMap((d) => {
      const at = lngLat(d.ll);
      if (!at) return [];
      const sym = symbol(unitSidc(d.template, aff(d.faction), knownStatus(d.intel)), { size: 26, uniqueDesignation: designation(d.name) });
      const org = d.org != null ? `<i style="width:${d.org * 100}%;background:#60a5fa"></i>` : "";
      const html =
        `<div class="unit-symbol">${sym.svg}</div>` +
        `<div class="unit-bars" style="margin-left:${sym.anchor[0] - 16}px"><i style="width:${d.strength * 100}%;background:${levelColor(d.strength)}"></i>${org}</div>` +
        `<div class="unit-label">${d.name}${d.intel?.stale ? ` <small>${ageLabel(d.intel.age_s)}</small>` : ""}</div>`;
      const cls = ["unit", d.battle ? "in-battle" : "", d.authority !== "campaign" ? "in-game" : "", d.intel?.stale ? "stale" : "", selectedIs(selected, "division", d.id) ? "selected" : ""].join(" ");
      const stack = stacks.get(spot(d.pos)) ?? [d.id];
      const shift: [number, number] = [(stack.indexOf(d.id) - (stack.length - 1) / 2) * SPREAD, 0];
      return [{ id: d.id, at, html, cls, anchor: sym.anchor, shift, title: `${d.name} — ${d.template_name}`, select: { kind: "division", id: d.id }, live: `d:${d.id}` }];
    });
  }, [data.forces, campaign, selected, layers.divisions, viewpoint]);

  const logisticsMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.logistics) return [];
    return (data.logistics?.convoys ?? []).flatMap((c) => {
      const at = lngLat(c.ll);
      if (!at) return [];
      const sym = symbol(convoySidc(aff(c.faction), knownStatus(c.intel)), { size: 16 });
      const cls = ["convoy", c.authority !== "campaign" ? "in-game" : "", c.intel?.stale ? "stale" : "", selectedIs(selected, "convoy", c.id) ? "selected" : ""].join(" ");
      const title = c.intel ? `Enemy convoy: ${Math.round(c.trucks)} trucks — ${intelLabel(c.intel)}` : `Convoy ${c.id}: ${Math.round(c.trucks)} trucks, ${Math.round(c.cargo)} supply`;
      return [{ id: c.id, at, html: sym.svg, cls, anchor: sym.anchor, title, select: { kind: "convoy", id: c.id }, live: `c:${c.id}` }];
    });
  }, [data.logistics, campaign, selected, layers.logistics, viewpoint]);

  const battleMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.fronts) return [];
    return (data.fronts?.battles ?? []).flatMap((b) => {
      const at = lngLat(b.ll);
      return at ? [{ id: b.id, at, html: `<span>⚔</span>`, cls: `battle ${b.delegated ? "in-game" : ""} ${selectedIs(selected, "battle", b.id) ? "selected" : ""}`, title: `${battleTitle(b)} (${b.hours} h)`, select: { kind: "battle", id: b.id } as Selection }] : [];
    });
  }, [data.fronts, selected, layers.fronts]);

  const airMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.air) return [];
    const out: MarkerSpec[] = [];
    for (const b of data.map?.airbases ?? []) {
      const at = lngLat(b.ll);
      if (!at) continue;
      const sym = symbol(airbaseSidc(aff(b.owner)), { size: 17 });
      out.push({ id: `ab-${b.id}`, at, html: sym.svg, cls: `airbase ${selectedIs(selected, "airbase", b.id) ? "selected" : ""}`, anchor: sym.anchor, title: b.name, select: { kind: "airbase", id: b.id } });
    }
    for (const p of data.ato?.packages ?? []) {
      for (const f of p.flights) {
        if (!["airborne", "on_station", "rtb"].includes(f.status)) continue;
        const at = lngLat(f.ll);
        if (!at) continue;
        const sym = symbol(airSidc(f.task, f.helo, aff(f.faction)), {
          size: 19,
          uniqueDesignation: f.callsign,
          direction: f.status === "on_station" ? undefined : Math.round(bearing(f)),
        });
        out.push({ id: f.id, at, html: sym.svg, cls: `flight ${f.authority !== "campaign" ? "in-game" : ""} ${selectedIs(selected, "flight", f.id) ? "selected" : ""}`, anchor: sym.anchor, title: `${f.callsign} ${f.aircraft_name} ${f.task} (${f.status})`, select: { kind: "flight", id: f.id }, live: `f:${f.id}` });
      }
    }
    // Fog of war: enemy aircraft as radar tracks (yellow "unknown" until identified).
    for (const f of data.ato?.tracks ?? []) {
      const at = lngLat(f.ll);
      if (!at || !f.intel) continue;
      const ident = f.intel.identified;
      const sym = symbol(airSidc(ident ? f.task : null, f.helo, ident ? aff(f.faction) : "U", knownStatus(f.intel)), { size: 17, uniqueDesignation: ident ? f.aircraft_name : "" });
      const cls = `flight track ${f.intel.stale ? "stale" : ""} ${selectedIs(selected, "flight", f.id) ? "selected" : ""}`;
      out.push({ id: `trk-${f.id}`, at, html: sym.svg, cls, anchor: sym.anchor, title: `${f.callsign}: ${f.aircraft_name} — ${intelLabel(f.intel)}`, select: { kind: "flight", id: f.id }, live: `f:${f.id}` });
    }
    for (const pl of campaign?.players ?? []) {
      const at = lngLat(pl.ll);
      if (at) out.push({ id: `pl-${pl.id}`, at, html: `<b>◉</b><span class="flight-label">${pl.name}</span>`, cls: "player", title: `${pl.name} (${pl.coalition}, ${pl.kind})` });
    }
    return out;
  }, [data.map, data.ato, campaign, selected, layers.air, viewpoint]);

  const samMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.airDefense) return [];
    return (data.airDefense?.sites ?? []).flatMap((u) => {
      const at = lngLat(u.ll);
      if (!at) return [];
      const sym = symbol(samSidc(u.role, aff(u.faction), knownStatus(u.intel)), { size: u.role === "short" ? 15 : 18, uniqueDesignation: u.system_name.split(" ")[0] });
      const cls = ["sam", `st-${u.status}`, u.authority !== "campaign" ? "in-game" : "", u.intel?.stale ? "stale" : "", selectedIs(selected, "sam", u.id) ? "selected" : ""].join(" ");
      const title = `${u.name} — ${u.system_name} (${u.status})${u.intel ? ` — ${intelLabel(u.intel)}` : ""}`;
      return [{ id: `sam-${u.id}`, at, html: sym.svg, cls, anchor: sym.anchor, title, select: { kind: "sam", id: u.id } as Selection, live: `s:${u.id}` }];
    });
  }, [data.airDefense, campaign, selected, layers.airDefense, viewpoint]);

  // ---- ATO planning: route editor, and clicks that pick a point for an order
  const planner = useContext(PlannerContext);
  const noDraft = useRef(() => undefined).current;
  useRouteEditor(map, planner?.draft ?? null, planner?.setDraft ?? noDraft);
  const picking = planner?.picking ?? null;
  const takePick = useRef(planner?.takePick);
  takePick.current = planner?.takePick;
  useEffect(() => {
    if (!map || !picking) return;
    const canvas = map.getCanvas();
    canvas.classList.add("picking");
    const click = (e: { lngLat: { lat: number; lng: number } }) => takePick.current?.([e.lngLat.lat, e.lngLat.lng]);
    map.once("click", click);
    return () => {
      canvas.classList.remove("picking");
      map.off("click", click);
    };
  }, [map, picking]);

  // Bullseye of the viewpoint coalition: a marker, and the BULLS call of the cursor
  // (written straight into the DOM: no React render on every mouse move).
  const bulls = campaign?.factions.find((f) => f.coalition === viewpoint)?.bullseye.ll ?? null;
  const variation = campaign?.magnetic_variation_deg ?? 0;
  const bullsMarkers = useMemo<MarkerSpec[]>(() => {
    const at = lngLat(bulls);
    if (!at || !bulls) return [];
    const html = `<svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="13" cy="13" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M13 0 V26 M0 13 H26" stroke="currentColor" stroke-width="1"/></svg>`;
    return [{ id: "bullseye", at, html, cls: "bullseye", title: `Bullseye (${viewpoint}): ${bulls[0].toFixed(4)}, ${bulls[1].toFixed(4)} — reference of BULLS calls` }];
  }, [bulls?.[0], bulls?.[1], viewpoint]);
  const readout = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!map || !bulls) return;
    const move = (e: { lngLat: { lat: number; lng: number } }) => {
      if (readout.current) readout.current.textContent = `BULLS ${bullsLabel(bulls, [e.lngLat.lat, e.lngLat.lng], variation, units)}`;
    };
    const out = () => { if (readout.current) readout.current.textContent = ""; };
    map.on("mousemove", move);
    map.getCanvas().addEventListener("mouseleave", out);
    return () => {
      map.off("mousemove", move);
      map.getCanvas().removeEventListener("mouseleave", out);
    };
  }, [map, bulls?.[0], bulls?.[1], variation, units]);

  // Smooth movement: positions streamed a few times a second, interpolated at 60 fps.
  const feed = useLiveFeed(data.faction);
  useHtmlMarkers(map, logisticsMarkers, onSelect, feed);
  useHtmlMarkers(map, samMarkers, onSelect, feed);
  useHtmlMarkers(map, divisionMarkers, onSelect, feed);
  useHtmlMarkers(map, battleMarkers, onSelect);
  useHtmlMarkers(map, airMarkers, onSelect, feed);
  useHtmlMarkers(map, bullsMarkers, onSelect);

  const shown = [...problems].filter((p) => !dismissed.has(p));
  return (
    <>
      <div ref={container} className="map" />
      {bulls && <div ref={readout} className="bulls-readout" title={`Cursor from the ${viewpoint} bullseye: magnetic bearing / range`} />}
      {shown.length > 0 && (
        <div className="map-notices">
          {shown.map((p) => (
            <div key={p} className={`map-notice ${p}`} role="alert">
              <div>{noticeText(p)}</div>
              <div className="map-notice-buttons">
                {p === "webgl" && <button onClick={() => window.location.reload()}>Reload</button>}
                <button title="Dismiss" onClick={() => setDismissed((s) => new Set(s).add(p))}>✕</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/** What a map problem means for the user, and what to do about it. */
function noticeText(p: MapProblem) {
  const host = <code>{BASEMAP_HOST}</code>;
  switch (p) {
    case "fallback":
      return (
        <>
          <b>Base map unavailable</b> — requests to {host} fail, so a plain background is shown and province names are
          hidden. Usually an ad blocker (uBlock Origin, Privacy Badger…) or strict tracking protection: allow {host} for
          this page and reload. Campaign layers work regardless.
        </>
      );
    case "tiles":
      return (
        <>
          <b>Base map incomplete</b> — some requests to {host} are blocked or failing (tiles or fonts). Usually an ad
          blocker such as uBlock Origin: allow {host} for this page and reload. Campaign layers work regardless.
        </>
      );
    case "webgl":
      return (
        <>
          <b>Map rendering stopped</b> — the browser took the WebGL context away (GPU driver reset, too many 3D tabs…).
          It may come back by itself; otherwise reload. If it keeps happening, check <code>about:support</code> →
          Graphics (Firefox) or <code>chrome://gpu</code>.
        </>
      );
  }
}

// ---------------------------------------------------------------- helpers

/** Circle of ``radius`` metres around ``ll`` ([lat, lon]) as a [lon, lat] ring. */
function circle(ll: number[], radius: number, n = 72): number[][] {
  const [lat, lon] = ll;
  const k = Math.cos((lat * Math.PI) / 180);
  const pts: number[][] = [];
  for (let i = 0; i <= n; i++) {
    const a = (2 * Math.PI * i) / n;
    pts.push([lon + (radius * Math.sin(a)) / (111_320 * k), lat + (radius * Math.cos(a)) / 111_320]);
  }
  return pts;
}

function selectedIs(s: Selection, kind: string, id: string) {
  return s?.kind === kind && s.id === id;
}

function bearing(f: Flight): number {
  const next = f.waypoints.pos[Math.min(f.next_waypoint, f.waypoints.pos.length - 1)];
  if (!next) return 0;
  const dx = next[0] - f.pos[0];
  const dy = next[1] - f.pos[1];
  return (Math.atan2(dx, dy) * 180) / Math.PI;
}

interface MarkerSpec {
  id: string;
  at: LngLatLike;
  html: string;
  cls: string;
  /** Point of the content (px from its top-left) that sits on the coordinate; default: centre. */
  anchor?: [number, number];
  /** Extra pixel offset (e.g. to fan out stacked symbols). */
  shift?: [number, number];
  title?: string;
  color?: string;
  select?: Selection;
  /** Key in the live position feed ("d:<id>", "f:<id>"...): animated at 60 fps. */
  live?: string;
}

/** Keep a set of HTML markers in sync with ``items`` (by id), touching the DOM only on change. */
function useHtmlMarkers(map: MLMap | null, items: MarkerSpec[], onSelect: (s: Selection) => void, feed?: React.RefObject<LiveFeed | null>) {
  const markers = useRef(new Map<string, { marker: Marker; html: string; cls: string; spec: MarkerSpec; at?: [number, number] }>());

  // Animation: every frame, markers with a live key follow the interpolated feed.
  useEffect(() => {
    if (!map || !feed) return;
    let raf = 0;
    const frame = () => {
      const f = feed.current;
      if (f?.ready) {
        const now = performance.now();
        for (const entry of markers.current.values()) {
          if (!entry.spec.live) continue;
          const p = f.position(entry.spec.live, now);
          if (!p) continue;
          // skip sub-pixel changes (~1 m)
          if (entry.at && Math.abs(entry.at[0] - p[0]) < 1e-5 && Math.abs(entry.at[1] - p[1]) < 1e-5) continue;
          entry.at = p;
          entry.marker.setLngLat(p);
        }
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [map, feed]);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  useEffect(() => {
    if (!map) return;
    const seen = new Set<string>();
    for (const it of items) {
      seen.add(it.id);
      let entry = markers.current.get(it.id);
      if (!entry) {
        // MapLibre owns the outer element (classes, transform); our content and
        // classes live on an inner element so we never clobber its positioning.
        const el = document.createElement("div");
        el.appendChild(document.createElement("div"));
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          const spec = markers.current.get(it.id)?.spec;
          if (spec?.select) selectRef.current(spec.select);
        });
        const marker = new Marker({ element: el, anchor: it.anchor ? "top-left" : "center", subpixelPositioning: true }).setLngLat(it.at).addTo(map);
        entry = { marker, html: "", cls: "", spec: it };
        markers.current.set(it.id, entry);
      }
      const el = entry.marker.getElement().firstElementChild as HTMLElement;
      // Animated markers are placed by the feed; the (slower) views only place
      // them when the feed does not carry them (yet).
      if (!(it.live && entry.at && feed?.current?.position(it.live, performance.now()))) {
        entry.marker.setLngLat(it.at);
        entry.at = undefined;
      }
      entry.spec = it;
      if (entry.html !== it.html) {
        el.innerHTML = it.html;
        entry.html = it.html;
      }
      const cls = `mk ${it.cls}`;
      if (entry.cls !== cls) {
        el.className = cls;
        entry.cls = cls;
      }
      const [sx, sy] = it.shift ?? [0, 0];
      entry.marker.setOffset(it.anchor ? [sx - it.anchor[0], sy - it.anchor[1]] : [sx, sy]);
      if (it.title) el.title = it.title;
      if (it.live) entry.marker.getElement().dataset.live = it.live;
      if (it.color) el.style.color = it.color;
    }
    for (const [id, entry] of markers.current) {
      if (!seen.has(id)) {
        entry.marker.remove();
        markers.current.delete(id);
      }
    }
  }, [map, items]);

  useEffect(
    () => () => {
      for (const entry of markers.current.values()) entry.marker.remove();
      markers.current.clear();
    },
    [],
  );
}
