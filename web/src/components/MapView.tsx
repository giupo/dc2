import { LngLatBounds, Map as MLMap, Marker, NavigationControl, ScaleControl, setWorkerUrl, type GeoJSONSource, type LngLatLike } from "maplibre-gl";
// MapLibre loads its web worker from a file next to its own module, which does not
// exist once bundled: let Vite ship the worker as an asset and point MapLibre at it.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Feature, FeatureCollection } from "geojson";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Flight } from "../api/client";
import { factionColor, levelColor, sideOf } from "../lib/format";
import { affiliation, airbaseSidc, airSidc, convoySidc, designation, samSidc, symbol, unitSidc } from "../lib/symbols";
import type { CampaignData } from "../lib/useCampaign";
import type { Layers, Selection } from "../lib/selection";

setWorkerUrl(workerUrl);

const STYLE: string = import.meta.env.VITE_MAP_STYLE ?? "https://tiles.openfreemap.org/styles/dark";
const FONT = ["Noto Sans Regular"];
const FIRE_FADE_MS = 12_000;

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
}

export function MapView({ data, layers, selected, onSelect, focus, viewpoint }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<MLMap | null>(null);
  const fitted = useRef(false);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const { campaign } = data;

  // ---- create the map once
  useEffect(() => {
    if (!container.current) return;
    const m = new MLMap({ container: container.current, style: STYLE, center: [38, 36], zoom: 6, attributionControl: { compact: true } });
    m.addControl(new NavigationControl({ visualizePitch: false }), "top-right");
    m.addControl(new ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("load", () => {
      for (const id of ["territory", "links", "fronts", "routes", "flightpaths", "provinces", "fires", "ad-rings", "iads"]) {
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
      m.addLayer({ id: "iads-link", type: "line", source: "iads", paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.5, "line-dasharray": [0.5, 2] } });
      m.addLayer({ id: "fire-arc", type: "line", source: "fires", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#f97316", "line-width": 2, "line-opacity": ["*", 0.9, ["get", "opacity"]], "line-dasharray": [2, 1.2] } });
      m.addLayer({ id: "fire-impact", type: "circle", source: "fires", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": ["+", 4, ["*", 8, ["-", 1, ["get", "opacity"]]]], "circle-color": "#fb923c", "circle-blur": 0.4, "circle-opacity": ["*", 0.85, ["get", "opacity"]], "circle-stroke-color": "#fed7aa", "circle-stroke-width": 1.5, "circle-stroke-opacity": ["get", "opacity"] } });
      m.addLayer({ id: "province-dot", type: "circle", source: "provinces", paint: { "circle-radius": ["+", 3, ["*", 0.6, ["get", "vp"]]], "circle-color": ["get", "color"], "circle-stroke-color": ["case", ["get", "depot"], "#facc15", "#0b0f17"], "circle-stroke-width": ["case", ["get", "depot"], 2.5, 1] } });
      m.addLayer({ id: "province-label", type: "symbol", source: "provinces", layout: { "text-field": ["get", "name"], "text-font": FONT, "text-size": 12, "text-offset": [0, 1.2], "text-anchor": "top", "text-allow-overlap": false }, paint: { "text-color": "#e5e7eb", "text-halo-color": "#05070b", "text-halo-width": 1.4 } });

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
    return () => m.remove();
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
    src("ad-rings")?.setData(fc(rings));
    src("iads")?.setData(
      fc((data.airDefense?.links ?? []).map((l) => ({ type: "Feature", properties: { color: factionColor(campaign, l.faction) }, geometry: { type: "LineString", coordinates: line(l.ll) } }))),
    );
  }, [map, data.map, data.fronts, data.forces, data.logistics, data.ato, data.airDefense, campaign, selected]);

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
  }, [map, layers]);

  // ---- HTML markers: NATO APP-6 symbols (milsymbol)
  const aff = (faction: string | null | undefined) => affiliation(sideOf(campaign, faction), viewpoint);

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
      const sym = symbol(unitSidc(d.template, aff(d.faction)), { size: 26, uniqueDesignation: designation(d.name) });
      const html =
        `<div class="unit-symbol">${sym.svg}</div>` +
        `<div class="unit-bars" style="margin-left:${sym.anchor[0] - 16}px"><i style="width:${d.strength * 100}%;background:${levelColor(d.strength)}"></i><i style="width:${d.org * 100}%;background:#60a5fa"></i></div>` +
        `<div class="unit-label">${d.name}</div>`;
      const cls = ["unit", d.battle ? "in-battle" : "", d.authority !== "campaign" ? "in-game" : "", selectedIs(selected, "division", d.id) ? "selected" : ""].join(" ");
      const stack = stacks.get(spot(d.pos)) ?? [d.id];
      const shift: [number, number] = [(stack.indexOf(d.id) - (stack.length - 1) / 2) * SPREAD, 0];
      return [{ id: d.id, at, html, cls, anchor: sym.anchor, shift, title: `${d.name} — ${d.template_name}`, select: { kind: "division", id: d.id } }];
    });
  }, [data.forces, campaign, selected, layers.divisions, viewpoint]);

  const logisticsMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.logistics) return [];
    return (data.logistics?.convoys ?? []).flatMap((c) => {
      const at = lngLat(c.ll);
      if (!at) return [];
      const sym = symbol(convoySidc(aff(c.faction)), { size: 16 });
      const cls = ["convoy", c.authority !== "campaign" ? "in-game" : "", selectedIs(selected, "convoy", c.id) ? "selected" : ""].join(" ");
      return [{ id: c.id, at, html: sym.svg, cls, anchor: sym.anchor, title: `Convoy ${c.id}: ${Math.round(c.trucks)} trucks, ${Math.round(c.cargo)} supply`, select: { kind: "convoy", id: c.id } }];
    });
  }, [data.logistics, campaign, selected, layers.logistics, viewpoint]);

  const battleMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.fronts) return [];
    return (data.fronts?.battles ?? []).flatMap((b) => {
      const at = lngLat(b.ll);
      return at ? [{ id: b.id, at, html: `<span>⚔</span>`, cls: `battle ${b.delegated ? "in-game" : ""} ${selectedIs(selected, "battle", b.id) ? "selected" : ""}`, title: `Battle for ${b.name} (${b.hours} h)`, select: { kind: "battle", id: b.id } as Selection }] : [];
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
        out.push({ id: f.id, at, html: sym.svg, cls: `flight ${f.authority !== "campaign" ? "in-game" : ""} ${selectedIs(selected, "flight", f.id) ? "selected" : ""}`, anchor: sym.anchor, title: `${f.callsign} ${f.aircraft_name} ${f.task} (${f.status})`, select: { kind: "flight", id: f.id } });
      }
    }
    for (const pl of campaign?.players ?? []) {
      const at = lngLat(pl.ll);
      if (at) out.push({ id: `pl-${pl.id}`, at, html: `<b>◉</b><span class="flight-label">${pl.name}</span>`, cls: "player", title: `${pl.name} (${pl.side}, ${pl.kind})` });
    }
    return out;
  }, [data.map, data.ato, campaign, selected, layers.air, viewpoint]);

  const samMarkers = useMemo<MarkerSpec[]>(() => {
    if (!layers.airDefense) return [];
    return (data.airDefense?.sites ?? []).flatMap((u) => {
      const at = lngLat(u.ll);
      if (!at) return [];
      const sym = symbol(samSidc(u.role, aff(u.faction)), { size: u.role === "short" ? 15 : 18, uniqueDesignation: u.system_name.split(" ")[0] });
      const cls = ["sam", `st-${u.status}`, u.authority !== "campaign" ? "in-game" : "", selectedIs(selected, "sam", u.id) ? "selected" : ""].join(" ");
      return [{ id: `sam-${u.id}`, at, html: sym.svg, cls, anchor: sym.anchor, title: `${u.name} — ${u.system_name} (${u.status})`, select: { kind: "sam", id: u.id } as Selection }];
    });
  }, [data.airDefense, campaign, selected, layers.airDefense, viewpoint]);

  useHtmlMarkers(map, logisticsMarkers, onSelect);
  useHtmlMarkers(map, samMarkers, onSelect);
  useHtmlMarkers(map, divisionMarkers, onSelect);
  useHtmlMarkers(map, battleMarkers, onSelect);
  useHtmlMarkers(map, airMarkers, onSelect);

  return <div ref={container} className="map" />;
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
}

/** Keep a set of HTML markers in sync with ``items`` (by id), touching the DOM only on change. */
function useHtmlMarkers(map: MLMap | null, items: MarkerSpec[], onSelect: (s: Selection) => void) {
  const markers = useRef(new Map<string, { marker: Marker; html: string; cls: string; spec: MarkerSpec }>());
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
        const marker = new Marker({ element: el, anchor: it.anchor ? "top-left" : "center" }).setLngLat(it.at).addTo(map);
        entry = { marker, html: "", cls: "", spec: it };
        markers.current.set(it.id, entry);
      }
      const el = entry.marker.getElement().firstElementChild as HTMLElement;
      entry.marker.setLngLat(it.at);
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
