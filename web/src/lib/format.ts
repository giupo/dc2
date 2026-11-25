import type { Campaign, CampaignEvent, EventKind, MapData } from "../api/client";

// ---------------------------------------------------------------- colours

const SIDE_COLOR: Record<string, string> = { blue: "#4c8dff", red: "#ff5a5a" };
const SIDE_FILL: Record<string, string> = { blue: "#2563eb", red: "#dc2626" };
export const NEUTRAL = "#9ca3af";

export function sideOf(campaign: Campaign | undefined, faction: string | null | undefined): string {
  return campaign?.factions.find((f) => f.id === faction)?.side ?? faction ?? "";
}
export function factionColor(campaign: Campaign | undefined, faction: string | null | undefined): string {
  return SIDE_COLOR[sideOf(campaign, faction)] ?? NEUTRAL;
}
export function factionFill(campaign: Campaign | undefined, faction: string | null | undefined): string {
  return SIDE_FILL[sideOf(campaign, faction)] ?? NEUTRAL;
}
export function factionName(campaign: Campaign | undefined, faction: string | null | undefined): string {
  return campaign?.factions.find((f) => f.id === faction)?.name ?? faction ?? "?";
}

export function levelColor(v: number): string {
  return v >= 0.66 ? "#22c55e" : v >= 0.33 ? "#eab308" : "#ef4444";
}

export const pct = (v: number) => `${Math.round(v * 100)}%`;

export const TEMPLATE_SHORT: Record<string, string> = {
  tank_brigade: "Armd",
  mech_brigade: "Mech",
  infantry_brigade: "Inf",
  artillery_group: "Arty",
};

// ---------------------------------------------------------------- events

export type EventCategory = "ground" | "fires" | "logistics" | "air" | "players" | "orders" | "system";

/** Category of every event kind. A `Record` over the generated `EventKind`
 * union, so a kind added to the engine fails `tsc` until it is placed here. */
const EVENT_CATEGORY: Record<EventKind, EventCategory> = {
  ai_order: "orders",
  artillery_fire: "fires",
  attack_halted: "orders",
  battle_started: "ground",
  battle_ended: "ground",
  contact_broken: "ground",
  division_destroyed: "ground",
  division_reformed: "ground",
  division_reforming: "ground",
  division_removed: "ground",
  division_retreated: "ground",
  objective_changed: "ground",
  order_cancelled: "orders",
  order_completed: "orders",
  province_captured: "ground",
  withdrawal_completed: "ground",
  convoy_delivered: "logistics",
  convoy_destroyed: "logistics",
  convoy_dispatched: "logistics",
  convoy_returned: "logistics",
  air_engagement: "air",
  air_strike: "air",
  aircraft_delivered: "air",
  aircraft_lost: "air",
  airlift_delivered: "air",
  flight_aborted: "air",
  flight_cancelled: "air",
  flight_complete: "air",
  flight_landed: "air",
  flight_replanned: "air",
  flight_rerouted: "air",
  flight_rtb: "air",
  flight_takeoff: "air",
  package_complete: "air",
  package_ordered: "air",
  package_planned: "air",
  package_retimed: "air",
  scramble: "air",
  harm_strike: "air",
  sam_destroyed: "air",
  sam_engagement: "air",
  sam_relocating: "air",
  sam_replaced: "air",
  sam_struck: "air",
  game_disconnected: "system",
  game_losses: "system",
  handoff: "system",
  handoff_failed: "system",
  handoff_timeout: "system",
  player_joined: "players",
  player_landed: "players",
  player_left: "players",
  player_takeoff: "players",
  seat_released: "players",
  seat_taken: "players",
  campaign_resumed: "system",
  campaign_saved: "system",
  time_control: "system",
};

export function eventCategory(kind: EventKind): EventCategory {
  return EVENT_CATEGORY[kind] ?? "ground";
}

export const CATEGORY_ICON: Record<EventCategory, string> = {
  ground: "⚔",
  fires: "✸",
  logistics: "⛟",
  air: "✈",
  players: "👤",
  orders: "➜",
  system: "⚙",
};

/** One human readable line per event. */
export function describeEvent(e: CampaignEvent, campaign?: Campaign, map?: MapData): string {
  const d = e.data as Record<string, unknown>;
  const prov = (id: unknown) => map?.provinces.find((p) => p.id === id)?.name ?? String(id);
  const fac = (id: unknown) => factionName(campaign, id as string);
  switch (e.kind) {
    case "province_captured":
      return `${fac(d.faction)} captured ${prov(d.province)}`;
    case "battle_started":
      return d.battle_kind === "meeting"
        ? `Meeting engagement near ${prov(d.province)}: ${fac(d.attacker)} runs into ${fac(d.defender)}`
        : `Battle for ${prov(d.province)}: ${fac(d.attacker)} attacks`;
    case "battle_ended":
      return d.battle_kind === "meeting"
        ? `Meeting engagement near ${prov(d.province)} over: ${fac(d.winner)} holds the road (${d.hours} h)`
        : `Battle for ${prov(d.province)} over: ${fac(d.winner)} holds the field (${d.hours} h)`;
    case "contact_broken":
      return `${d.division} breaks contact and falls back`;
    case "division_retreated":
      return `${d.division} retreats from ${prov(d.origin)} to ${prov(d.destination)}`;
    case "division_destroyed":
      return `${d.division} destroyed at ${prov(d.province)} (${d.reason})`;
    case "sam_engagement":
      return `${d.system} (${d.sam}) fires ${d.fired} at ${d.callsign}${d.kills ? ` — ${d.kills} down` : " — missed"}${d.networked ? " [IADS]" : ""}`;
    case "harm_strike":
      return `${d.by}: anti-radiation strike on ${d.system} (${d.sam}) — ${d.result}`;
    case "sam_struck":
      return `${d.by} strikes ${d.system} (${d.sam}) — ${d.result}`;
    case "sam_destroyed":
      return `${d.system} (${d.sam}) destroyed${d.reason ? ` (${d.reason})` : ""}`;
    case "sam_relocating":
      return `${d.system} (${d.sam}) relocating ${prov(d.origin)} → ${prov(d.destination)}`;
    case "sam_replaced":
      return `Replacement ${d.system} battery at ${prov(d.province)}`;
    case "aircraft_delivered":
      return `${fac(d.faction)}: replacement aircraft for ${d.squadron} (${d.aircraft})`;
    case "artillery_fire":
      return `${d.shooter} shells ${d.target} (${d.km} km)`;
    case "division_reforming":
      return `${d.name} destroyed: re-forming in ${d.hours} h`;
    case "division_reformed":
      return `${d.name} re-formed at ${prov(d.province)}`;
    case "withdrawal_completed":
      return `${d.entity} back on its start line at ${prov(d.province)}`;
    case "attack_halted":
      return `${d.division} halts its attack`;
    case "objective_changed":
      return `${fac(d.faction)} ${d.achieved ? "takes" : "loses"} objective ${prov(d.province)}`;
    case "convoy_dispatched":
      return `Convoy ${d.convoy} ${prov(d.origin)} → ${prov(d.destination)} (${d.cargo} supply)`;
    case "convoy_delivered":
      return `Convoy ${d.convoy} delivered ${d.amount} at ${prov(d.province)}`;
    case "convoy_destroyed":
      return `Convoy ${d.convoy} destroyed (${d.reason})`;
    case "convoy_returned":
      return `Convoy ${d.convoy} back at depot`;
    case "package_planned":
      return `${d.number ? `PKG ${d.number} · ` : ""}${d.task}: ${d.target}, TOT ${d.tot} — ${(d.flights as string[]).join(", ")}`;
    case "package_complete":
      return `${d.number ? `PKG ${d.number} · ` : ""}${d.task}: ${d.target} — mission over`;
    case "package_ordered":
      return `${fac(d.faction)} planner orders PKG ${d.number} · ${d.task}: ${d.target}`;
    case "package_retimed":
      return `PKG ${d.number}: new TOT ${d.tot}`;
    case "flight_replanned":
      return `${d.callsign}: new route, ${d.waypoints} waypoints, ${d.length_km} km (takeoff ${d.takeoff})`;
    case "flight_takeoff":
      return `${d.callsign} airborne (${d.task})`;
    case "flight_landed":
      return `${d.callsign} landed`;
    case "flight_rtb":
      return `${d.callsign} heads home: ${d.reason}`;
    case "flight_aborted":
      return `${d.callsign} aborts: ${d.reason}`;
    case "scramble":
      return `SCRAMBLE ${d.callsign} from ${d.base}: ${d.aircraft} over ${d.threatened}`;
    case "flight_rerouted":
      return `${d.callsign} reroutes around ${d.reason}`;
    case "flight_cancelled":
      return `${d.callsign} cancelled: ${d.reason}`;
    case "flight_complete":
      return `${d.callsign} mission complete (${d.survivors} aircraft back)`;
    case "air_engagement":
      return `Air combat ${(d.callsigns as string[]).join(" vs ")}${d.phase ? ` (${d.phase})` : ""} — losses ${(d.losses as number[]).join(":")}`;
    case "air_strike": {
      const hits = d.divisions ? `on ${(d.divisions as string[]).join(", ") || "nothing"}` : d.convoy ? `on convoy ${d.convoy}` : d.depot ? `on depot ${prov(d.depot)}` : "";
      return `${d.task} strike ${hits}${d.lost ? `, ${d.lost} lost to air defence` : ""}`;
    }
    case "aircraft_lost":
      return `${fac(d.faction)} lost ${d.count} aircraft (${d.cause})`;
    case "player_joined":
      return `${d.name ?? d.player} joined (${d.side})`;
    case "player_left":
      return `${d.player} left`;
    case "seat_taken":
      return `${d.player} takes a seat in ${d.callsign}`;
    case "seat_released":
      return `${d.player} leaves ${d.flight}`;
    case "handoff":
      return `${d.entity} handed to ${d.to}`;
    case "ai_order":
      return `${fac(d.faction)}: ${d.division} ${d.order} → ${prov(d.target)}`;
    case "order_completed":
      return `${d.division} reached ${prov(d.province)}`;
    default:
      return `${e.kind} ${JSON.stringify(d)}`;
  }
}

/** Age of a piece of intelligence: "now", "12 min", "3 h". */
export function ageLabel(seconds: number): string {
  if (seconds < 90) return "now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

/** One-line description of how a contact is known. */
export function intelLabel(i: { source: string; age_s: number; stale: boolean; identified: boolean }): string {
  return `${i.identified ? "" : "unidentified · "}${i.source} · ${ageLabel(i.age_s)}${i.stale ? " (stale)" : ""}`;
}

// ---------------------------------------------------------------- units

/** Display units: SI, or aviation imperial (NM, ft, kts). */
export type Units = "metric" | "imperial";

export function fmtDist(km: number, units: Units): string {
  return units === "imperial" ? `${Math.round(km / 1.852)} NM` : `${Math.round(km)} km`;
}

export function fmtAlt(m: number, units: Units): string {
  return units === "imperial" ? `${(Math.round(m / 0.3048 / 100) * 100).toLocaleString()} ft` : `${Math.round(m).toLocaleString()} m`;
}

export function fmtSpeed(kmh: number, units: Units): string {
  return units === "imperial" ? `${Math.round(kmh / 1.852 / 5) * 5} kts` : `${Math.round(kmh)} km/h`;
}

/** BULLS call of a point, "045/32": magnetic bearing (great circle from the
 * bullseye, minus the theatre's magnetic variation) and range in NM or km.
 * Same rule as the engine's `dynacamp.bullseye.label`. */
export function bullsLabel(bulls: number[], at: number[], variationDeg: number, units: Units): string {
  const rad = Math.PI / 180;
  const [p1, p2] = [bulls[0] * rad, at[0] * rad];
  const dl = (at[1] - bulls[1]) * rad;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  const brg = ((Math.atan2(y, x) / rad - variationDeg) % 360 + 360) % 360;
  const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  const km = 2 * 6371.0088 * Math.asin(Math.sqrt(a));
  const dist = units === "imperial" ? km / 1.852 : km;
  return `${String(Math.round(brg) % 360).padStart(3, "0")}/${Math.round(dist)}`;
}

/** Title of a battle: an assault on a province, or a meeting engagement on the road near one. */
export function battleTitle(b: { kind?: string; name: string }): string {
  return b.kind === "meeting" ? `Meeting engagement near ${b.name}` : `Battle for ${b.name}`;
}
