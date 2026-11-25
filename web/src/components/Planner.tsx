// ATO planning from the browser: route editing on the map, TOT and
// cancellation of a package, ordering a new package. Every change is a command
// (POST, needs the API token); the server validates it and explains refusals.
import { Marker, type GeoJSONSource, type Map as MLMap } from "maplibre-gl";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { AuthError, api, type Contact, type Flight, type Package, type PackageRequest } from "../api/client";
import type { CampaignData } from "../lib/useCampaign";
import { fmtDist, type Units } from "../lib/format";
import type { Selection } from "../lib/selection";

type LngLat = [number, number];

/** A route being edited: the flight's waypoints as [lng, lat], takeoff to landing. */
export interface RouteDraft {
  flightId: string;
  callsign: string;
  points: LngLat[];
  targetIndex: number;
  /** Leading waypoints that cannot move (already flown, or the station while on it). */
  locked: number;
  /** CAP/AWACS/tanker stations can move; attack targets cannot. */
  targetMovable: boolean;
  fuelKm: number;
  airborne: boolean;
  /** Current position of an airborne flight ([lng, lat]). */
  pos: LngLat | null;
}

const MAX_WAYPOINTS = 24;
const ORBITS = new Set(["CAP", "AWACS", "TANKER"]);

/** What the planner widgets share (provided by the App). */
export interface Planner {
  /** Faction that orders new packages: the viewpoint side's (null: none). */
  faction: string | null;
  draft: RouteDraft | null;
  setDraft: (d: RouteDraft | null) => void;
  /** Label of the point being picked on the map, if any. */
  picking: string | null;
  /** Ask the user to click a point on the map; ``done`` gets [lat, lon]. */
  pickPoint: (label: string, done: (ll: [number, number]) => void) => void;
  cancelPick: () => void;
  /** The map got the click a pick was waiting for ([lat, lon]). */
  takePick: (ll: [number, number]) => void;
}

export const PlannerContext = createContext<Planner | null>(null);

/** The planner shared state; a hook for the App to provide it. */
export function usePlannerState(faction: string | null): Planner {
  const [draft, setDraft] = useState<RouteDraft | null>(null);
  const [picking, setPicking] = useState<string | null>(null);
  const callback = useRef<((ll: [number, number]) => void) | null>(null);
  return {
    faction,
    draft,
    setDraft,
    picking,
    pickPoint: (label, done) => {
      callback.current = done;
      setPicking(label);
    },
    cancelPick: () => {
      callback.current = null;
      setPicking(null);
    },
    takePick: (ll) => {
      const cb = callback.current;
      callback.current = null;
      setPicking(null);
      cb?.(ll);
    },
  };
}

/** A draft of a flight's current route. */
export function draftOf(f: Flight): RouteDraft | null {
  const ll = f.waypoints.ll;
  if (!ll) return null;
  return {
    flightId: f.id,
    callsign: f.callsign,
    points: ll.map((p) => [p[1], p[0]] as LngLat),
    targetIndex: f.target_index,
    locked: f.locked_waypoints,
    targetMovable: ORBITS.has(f.task) && f.target_index >= f.locked_waypoints,
    fuelKm: f.fuel_km,
    airborne: f.status !== "scheduled",
    pos: f.ll ? [f.ll[1], f.ll[0]] : null,
  };
}

const editable = (f: Flight) => !f.intel && ["scheduled", "airborne", "on_station", "rtb"].includes(f.status);

function errorText(e: unknown): string {
  if (e instanceof AuthError) return "Locked: enter the API token (padlock, top right)";
  return e instanceof Error ? e.message : String(e);
}

// ---------------------------------------------------------------- geometry

function km(a: LngLat, b: LngLat): number {
  const r = Math.PI / 180;
  const dLat = (b[1] - a[1]) * r;
  const dLon = (b[0] - a[0]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

const pathKm = (pts: LngLat[]) => pts.slice(1).reduce((acc, p, i) => acc + km(pts[i], p), 0);

/** Route length as the server counts it: flown legs, then from the flight's position. */
export function routeKm(d: RouteDraft): number {
  if (!d.airborne || !d.pos || d.locked < 1) return pathKm(d.points);
  const flown = d.points.slice(0, d.locked);
  return pathKm(flown) + km(flown[flown.length - 1], d.pos) + pathKm([d.pos, ...d.points.slice(d.locked)]);
}

// ---------------------------------------------------------------- map editor

/** Draggable waypoints of the route being edited, "+" handles on the legs to
 * add one, double-click on a waypoint to remove it. */
export function useRouteEditor(map: MLMap | null, draft: RouteDraft | null, setDraft: (d: RouteDraft | null) => void) {
  useEffect(() => {
    if (!map) return;
    if (!map.getSource("edit-route")) {
      map.addSource("edit-route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "edit-route", type: "line", source: "edit-route", layout: { "line-join": "round" }, paint: { "line-color": "#fcd34d", "line-width": 3, "line-opacity": 0.9 } });
    }
    const src = map.getSource("edit-route") as GeoJSONSource;
    const markers: Marker[] = [];
    if (!draft) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const pts = draft.points.map((p) => [...p] as LngLat);
    const redraw = () => src.setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: pts } });
    redraw();
    const last = pts.length - 1;
    pts.forEach((p, i) => {
      const kind = i === 0 ? "home" : i === last ? "land" : i === draft.targetIndex ? "target" : "turn";
      const fixed = i < draft.locked || i === last || (i === draft.targetIndex && !draft.targetMovable);
      const removable = !fixed && kind === "turn";
      const el = document.createElement("div");
      el.className = `wp-handle ${kind} ${fixed ? "fixed" : ""}`;
      el.textContent = kind === "target" ? "T" : kind === "land" ? "L" : kind === "home" ? "H" : String(i);
      el.title = fixed
        ? i < draft.locked ? "Already flown: cannot move" : kind === "land" ? "Landing at home base: cannot move" : "Target: cannot move (order a new package for another one)"
        : removable ? "Drag to move · double-click to remove" : "Drag to move the station";
      el.addEventListener("click", (e) => e.stopPropagation());
      if (removable) {
        el.addEventListener("dblclick", (e) => {
          e.stopPropagation();
          e.preventDefault();
          setDraft({ ...draft, points: pts.filter((_, j) => j !== i), targetIndex: draft.targetIndex - (i < draft.targetIndex ? 1 : 0) });
        });
      }
      const m = new Marker({ element: el, draggable: !fixed, subpixelPositioning: true }).setLngLat(p).addTo(map);
      m.on("drag", () => {
        const at = m.getLngLat();
        pts[i] = [at.lng, at.lat];
        redraw();
      });
      m.on("dragend", () => setDraft({ ...draft, points: pts.map((q) => [...q] as LngLat) }));
      markers.push(m);
    });
    if (pts.length < MAX_WAYPOINTS) {
      for (let i = Math.max(0, draft.locked - 1); i < last; i++) {
        const el = document.createElement("div");
        el.className = "wp-add";
        el.textContent = "+";
        el.title = "Add a waypoint here";
        const mid: LngLat = [(pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2];
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          const at = i + 1;
          const points = [...pts.slice(0, at), mid, ...pts.slice(at)];
          setDraft({ ...draft, points, targetIndex: draft.targetIndex + (at <= draft.targetIndex ? 1 : 0) });
        });
        markers.push(new Marker({ element: el, subpixelPositioning: true }).setLngLat(mid).addTo(map));
      }
    }
    return () => markers.forEach((m) => m.remove());
  }, [map, draft, setDraft]);
}

/** Bar over the map while a route is edited: length against fuel, save, cancel. */
export function RouteBar({ units }: { units: Units }) {
  const planner = useContext(PlannerContext);
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const draft = planner?.draft;
  useEffect(() => setError(undefined), [draft]);
  if (!planner || !draft) return null;
  const length = routeKm(draft);
  const over = length > draft.fuelKm;
  const save = () => {
    setSaving(true);
    api
      .setRoute(draft.flightId, { waypoints: draft.points.map((p) => ({ ll: [p[1], p[0]] })), target_index: draft.targetIndex })
      .then(() => planner.setDraft(null))
      .catch((e) => setError(errorText(e)))
      .finally(() => setSaving(false));
  };
  return (
    <div className="route-bar">
      <div>
        <b>Route of {draft.callsign}</b> · {draft.points.length} waypoints ·{" "}
        <span className={over ? "warn" : ""} title="Twice the combat radius (more with a tanker on station). The server refuses longer routes, unless the current one already is.">
          {fmtDist(length, units)} / fuel {fmtDist(draft.fuelKm, units)}
        </span>
      </div>
      <div className="muted">Drag the waypoints · click + on a leg to add one · double-click a waypoint to remove it</div>
      {error && <div className="error">{error}</div>}
      <div className="route-bar-buttons">
        <button onClick={() => planner.setDraft(null)}>Discard</button>
        <button className="primary" disabled={saving} onClick={save}>{saving ? "Sending…" : "Send to the flight"}</button>
      </div>
    </div>
  );
}

/** Hint over the map while a point is being picked. */
export function PickHint() {
  const planner = useContext(PlannerContext);
  if (!planner?.picking) return null;
  return (
    <div className="route-bar pick-hint">
      Click on the map: <b>{planner.picking}</b> <button onClick={planner.cancelPick}>Cancel</button>
    </div>
  );
}

// ---------------------------------------------------------------- package card

const minutes = (s: number) => s * 60;

/** Orders on a package (and the selected flight): edit route, move TOT, cancel. */
export function PackageOrders({ pkg, flight }: { pkg: Package; flight?: Flight }) {
  const planner = useContext(PlannerContext);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    setError(undefined);
    setConfirm(false);
  }, [pkg.id, flight?.id]);
  if (!planner) return null;
  const live = pkg.flights.filter((f) => f.status !== "done" && f.status !== "cancelled");
  const grounded = live.filter((f) => f.status === "scheduled");
  const retimable = live.length > 0 && grounded.length === live.length;
  const run = (p: Promise<unknown>) => {
    setBusy(true);
    setError(undefined);
    p.catch((e) => setError(errorText(e))).finally(() => {
      setBusy(false);
      setConfirm(false);
    });
  };
  const tot = (t: number) => run(api.setTot(pkg.id, t));
  const editing = planner.draft?.flightId === flight?.id;
  return (
    <div className="orders">
      {flight && editable(flight) && (
        <button
          className={editing ? "on" : ""}
          disabled={!flight.waypoints.ll}
          onClick={() => planner.setDraft(editing ? null : draftOf(flight))}
          title="Move, add or remove waypoints on the map; the AI flies the new route, players get a new briefing"
        >
          ✏️ {editing ? "Editing route…" : `Edit route of ${flight.callsign}`}
        </button>
      )}
      {retimable && (
        <div className="tot-row" title={`Moves every flight of the package; earliest ${pkg.earliest_tot.label}`}>
          <span className="muted">TOT</span>
          {[-15, -5, 5, 15, 60].map((m) => (
            <button key={m} disabled={busy || pkg.tot.t + minutes(m) < pkg.earliest_tot.t} onClick={() => tot(pkg.tot.t + minutes(m))}>
              {m > 0 ? `+${m}` : m}′
            </button>
          ))}
          <button disabled={busy || pkg.tot.t - pkg.earliest_tot.t < 60} onClick={() => tot(Math.ceil(pkg.earliest_tot.t / 60) * 60)}>
            ASAP
          </button>
        </div>
      )}
      {grounded.length > 0 && (
        <button className={confirm ? "danger" : ""} disabled={busy} onClick={() => (confirm ? run(api.cancelPackage(pkg.id)) : setConfirm(true))}>
          {confirm ? `Confirm: cancel ${grounded.map((f) => f.callsign).join(", ")}` : `✕ Cancel package ${pkg.number}`}
        </button>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- new package

const TASKS = ["CAS", "BAI", "STRIKE", "SEAD", "DEAD", "CAP", "AWACS", "TANKER", "TRANSPORT"] as const;
type Kind = PackageRequest["target"]["kind"];

interface TargetOption {
  kind: Kind;
  id: string;
  label: string;
  ll?: number[] | null;
}

function targetOptions(task: string, faction: string, data: CampaignData, contacts: Contact[]): TargetOption[] {
  const ago = (c: Contact) => (c.age_s < 120 ? "now" : `${Math.round(c.age_s / 60)} min ago`);
  switch (task) {
    case "CAS":
      return (data.fronts?.battles ?? [])
        .filter((b) => b.attacker === faction || b.defender === faction)
        .map((b) => ({ kind: "battle", id: b.id, label: `Battle for ${b.name}`, ll: b.ll }));
    case "BAI":
      return contacts
        .filter((c) => c.kind === "division" || c.kind === "convoy")
        .map((c) => ({
          kind: c.kind as Kind,
          id: c.id,
          label: c.kind === "convoy" ? `Convoy (${c.detail.trucks ?? "?"} trucks) · ${ago(c)}` : `${c.detail.name ?? c.id} · ${ago(c)}`,
          ll: c.ll,
        }));
    case "STRIKE":
      return (data.logistics?.depots ?? []).filter((d) => d.controller !== faction).map((d) => ({ kind: "depot", id: d.province, label: `Depot ${d.name}`, ll: d.ll }));
    case "SEAD":
    case "DEAD":
      return contacts.filter((c) => c.kind === "sam").map((c) => ({ kind: "sam", id: c.id, label: `${c.detail.system_name ?? "SAM"} · ${ago(c)}`, ll: c.ll }));
    case "TRANSPORT":
      return [
        ...(data.map?.airbases ?? []).filter((a) => a.owner === faction).map((a) => ({ kind: "airlift" as Kind, id: a.id, label: `Airlift to ${a.name}`, ll: a.ll })),
        ...(data.forces?.divisions ?? []).filter((d) => d.faction === faction).map((d) => ({ kind: "resupply" as Kind, id: d.id, label: `Resupply ${d.name} (helicopters)`, ll: d.ll })),
      ];
    default:
      return [];
  }
}

/** Form to order a new package; targets come from what the faction knows. */
export function NewPackage({ data, select }: { data: CampaignData; select: (s: Selection, at?: number[] | null) => void }) {
  const planner = useContext(PlannerContext);
  const [open, setOpen] = useState(false);
  const [task, setTask] = useState<string>("CAS");
  const [target, setTarget] = useState("");
  const [point, setPoint] = useState<[number, number] | null>(null);
  const [squadron, setSquadron] = useState("");
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const faction = planner?.faction ?? null;

  useEffect(() => {
    if (!open || !faction) return;
    api.intel(faction).then((i) => setContacts(i.contacts)).catch(() => setContacts([]));
  }, [open, faction, task]);
  useEffect(() => {
    setTarget("");
    setSquadron("");
    setError(undefined);
  }, [task]);

  if (!planner || !faction) return null;
  if (!open) {
    return (
      <button className="primary" onClick={() => setOpen(true)} title="Order a package yourself, like the ATO screen of Falcon/BMS">
        ＋ New package
      </button>
    );
  }
  const byPoint = ORBITS.has(task);
  const options = byPoint ? [] : targetOptions(task, faction, data, contacts);
  const chosen = options.find((o) => `${o.kind}:${o.id}` === target);
  const squadrons = (data.ato?.squadrons ?? []).filter((s) => s.faction === faction && (s.tasks as string[]).includes(task === "DEAD" ? "STRIKE" : task)); // DEAD: pick the strikers
  const ready = byPoint ? !!point : !!chosen;

  const submit = () => {
    const body: PackageRequest = {
      faction,
      task: task as PackageRequest["task"],
      target: byPoint ? { kind: "point", id: "", point: { ll: point! } } : { kind: chosen!.kind, id: chosen!.id },
      squadron: squadron || null,
    };
    setBusy(true);
    setError(undefined);
    api
      .orderPackage(body)
      .then((pkg) => {
        setOpen(false);
        setPoint(null);
        select({ kind: "package", id: pkg.id }, pkg.ll);
      })
      .catch((e) => setError(errorText(e)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="new-package">
      <div className="new-package-head">
        <b>New package</b>
        <button onClick={() => setOpen(false)} title="close">✕</button>
      </div>
      <label>
        Task
        <select value={task} onChange={(e) => setTask(e.target.value)}>
          {TASKS.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </label>
      {byPoint ? (
        <label>
          {task === "CAP" ? "Station" : "Orbit"}
          <span className="grow">{point ? `${point[0].toFixed(3)}, ${point[1].toFixed(3)}` : "—"}</span>
          <button onClick={() => planner.pickPoint(`${task} ${task === "CAP" ? "station" : "orbit"}`, (ll) => setPoint(ll))}>Pick on map</button>
        </label>
      ) : (
        <label>
          Target
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{options.length ? "— choose —" : "nothing known for this task"}</option>
            {options.map((o) => (
              <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        Squadron
        <select value={squadron} onChange={(e) => setSquadron(e.target.value)}>
          <option value="">best available</option>
          {squadrons.map((s) => (
            <option key={s.id} value={s.id} disabled={s.available < 1}>
              {s.name} · {s.aircraft_name} · {s.available}/{s.strength}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">Routes go around known SAMs when they can; SEAD (with ECM) and escort are added when the target or the route calls for them. DEAD = strikers on a SAM site behind a SEAD flight. TOT is the earliest possible (move it afterwards).</p>
      {error && <div className="error">{error}</div>}
      <button className="primary" disabled={!ready || busy} onClick={submit}>
        {busy ? "Planning…" : "Order package"}
      </button>
    </div>
  );
}
