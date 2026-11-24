// Typed access to the DynaCamp HTTP API. Types come from the FastAPI OpenAPI
// schema: regenerate them with `npm run gen:api` after changing the backend.
import type { components } from "./schema";

type S = components["schemas"];
export type Campaign = S["CampaignView"];
export type MapData = S["MapView"];
export type Province = S["ProvinceView"];
export type Fronts = S["FrontsView"];
export type Battle = S["BattleView"];
export type Forces = S["ForcesView"];
export type Division = S["DivisionView"];
export type Logistics = S["LogisticsView"];
export type Convoy = S["ConvoyView"];
export type Depot = S["DepotView"];
export type Ato = S["AtoView"];
export type Package = S["PackageView"];
export type Flight = S["FlightView"];
export type Squadron = S["SquadronView"];
export type AirDefense = S["AirDefenseView"];
export type SamSite = S["SamSiteView"];
export type CampaignEvent = S["EventView"];
export type EventsPage = S["EventsPage"];
export type Time = S["Time"];
/** `tick` message of the SSE stream (not part of the JSON routes, hence not in the schema). */
export interface Tick {
  time: Time;
  live: boolean;
}
export type Player = S["PlayerView"];
export type Polyline = S["Polyline"];

// Empty = same origin (Vite proxies /api in development, FastAPI serves the build).
const BASE: string = import.meta.env.VITE_API_BASE ?? "";

async function get<T>(path: string): Promise<T> {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export const api = {
  campaign: () => get<Campaign>("/api/campaign"),
  map: () => get<MapData>("/api/map"),
  fronts: () => get<Fronts>("/api/fronts"),
  forces: () => get<Forces>("/api/forces"),
  logistics: () => get<Logistics>("/api/logistics"),
  ato: () => get<Ato>("/api/ato"),
  airDefense: () => get<AirDefense>("/api/airdefense"),
  events: (since: number, limit = 500) => get<EventsPage>(`/api/events?since=${since}&limit=${limit}`),
};

export function streamUrl(since?: number): string {
  return `${BASE}/api/stream${since != null ? `?since=${since}` : ""}`;
}
