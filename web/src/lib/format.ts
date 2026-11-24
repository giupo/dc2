import type { Campaign, CampaignEvent, MapData } from "../api/client";

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

export function eventCategory(kind: string): EventCategory {
  if (kind === "artillery_fire") return "fires";
  if (kind.startsWith("convoy") || kind.startsWith("depot")) return "logistics";
  if (["ai_order", "order_completed", "order_cancelled", "attack_halted"].includes(kind)) return "orders";
  if (
    kind.startsWith("flight") || kind.startsWith("air") || kind.startsWith("package") || kind === "aircraft_lost"
  )
    return "air";
  if (kind.startsWith("player") || kind.startsWith("seat")) return "players";
  if (kind.startsWith("sam") || kind === "harm_strike") return "air";
  if (kind.startsWith("handoff") || kind.startsWith("game")) return "system";
  return "ground";
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
      return `Battle for ${prov(d.province)}: ${fac(d.attacker)} attacks`;
    case "battle_ended":
      return `Battle for ${prov(d.province)} over: ${fac(d.winner)} holds the field (${d.hours} h)`;
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
    case "flight_takeoff":
      return `${d.callsign} airborne (${d.task})`;
    case "flight_landed":
      return `${d.callsign} landed`;
    case "flight_aborted":
      return `${d.callsign} aborts: ${d.reason}`;
    case "flight_cancelled":
      return `${d.callsign} cancelled: ${d.reason}`;
    case "flight_complete":
      return `${d.callsign} mission complete (${d.survivors} aircraft back)`;
    case "air_engagement":
      return `Air combat ${(d.callsigns as string[]).join(" vs ")} — losses ${(d.losses as number[]).join(":")}`;
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
