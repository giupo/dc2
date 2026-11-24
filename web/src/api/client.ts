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
export type Briefing = S["BriefingView"];
export type SamSite = S["SamSiteView"];
export type CampaignEvent = S["EventView"];
export type EventsPage = S["EventsPage"];
export type Time = S["Time"];
/** `tick` message of the SSE stream (not part of the JSON routes, hence not in the schema). */
export interface Tick {
  time: Time;
  live: boolean;
  paused: boolean;
  compression: number;
  rate: number;
}
export type Control = S["ControlView"];
export type Auth = S["AuthView"];
export type SaveStatus = S["SaveView"];
export type Player = S["PlayerView"];
export type Intel = S["IntelView"];
export type Contact = S["ContactView"];
export type ContactInfo = S["ContactInfo"];
export type Polyline = S["Polyline"];

// Empty = same origin (Vite proxies /api in development, FastAPI serves the build).
const BASE: string = import.meta.env.VITE_API_BASE ?? "";

// ---------------------------------------------------------------- API token
// Commands (POST) need `Authorization: Bearer <token>`; reading is open.
// `dynacamp serve` prints a link with `#token=...`: the fragment never reaches
// the server, and is removed from the address bar once stored.
const TOKEN_KEY = "dynacamp.token";

function takeTokenFromUrl(): void {
  const m = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
  if (!m) return;
  localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
  history.replaceState(null, "", window.location.pathname + window.location.search);
}
takeTokenFromUrl();

export const token = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (value: string) => localStorage.setItem(TOKEN_KEY, value.trim()),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

function authHeader(): Record<string, string> {
  const t = token.get();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/** A command was refused for a missing or wrong token (HTTP 401). */
export class AuthError extends Error {}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeader() },
    body: JSON.stringify(body),
  });
  if (r.status === 401) throw new AuthError("API token missing or wrong");
  if (!r.ok) {
    const detail = await r.json().then((j) => j.detail).catch(() => r.statusText);
    throw new Error(typeof detail === "string" ? detail : `HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

async function get<T>(path: string, headers?: Record<string, string>): Promise<T> {
  const r = await fetch(BASE + path, headers ? { headers } : undefined);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** Query string of a faction's fog-of-war view ("" = the umpire's view). */
const fq = (faction: string | null | undefined, sep = "?") => (faction ? `${sep}faction=${encodeURIComponent(faction)}` : "");

export const api = {
  campaign: () => get<Campaign>("/api/campaign"),
  map: () => get<MapData>("/api/map"),
  /** Whether commands need a token and whether the stored one is valid. */
  auth: () => get<Auth>("/api/auth", authHeader()),
  /** Pause / resume, fast-forward (refused with an error while a game is live). */
  control: (change: { paused?: boolean; compression?: number }) => post<Control>("/api/control", change),
  /** Save the campaign now to the server's `--save` file. */
  save: () => post<SaveStatus>("/api/save", {}),
  fronts: (faction?: string | null) => get<Fronts>("/api/fronts" + fq(faction)),
  forces: (faction?: string | null) => get<Forces>("/api/forces" + fq(faction)),
  logistics: (faction?: string | null) => get<Logistics>("/api/logistics" + fq(faction)),
  ato: (faction?: string | null) => get<Ato>("/api/ato" + fq(faction)),
  airDefense: (faction?: string | null) => get<AirDefense>("/api/airdefense" + fq(faction)),
  intel: (faction: string) => get<Intel>("/api/intel" + fq(faction)),
  briefing: (flightId: string, units?: "metric" | "imperial") =>
    get<Briefing>(`/api/ato/flights/${encodeURIComponent(flightId)}/briefing${units ? `?units=${units}` : ""}`),
  events: (since: number, limit = 500, faction?: string | null) =>
    get<EventsPage>(`/api/events?since=${since}&limit=${limit}${fq(faction, "&")}`),
};

/** Server-Sent Events of compact positions (`positions` events) at `hz`. */
export function positionsStreamUrl(hz: number, faction?: string | null): string {
  return `${BASE}/api/positions/stream?hz=${hz}${faction ? `&faction=${encodeURIComponent(faction)}` : ""}`;
}

export function streamUrl(since?: number, faction?: string | null): string {
  const q = [since != null ? `since=${since}` : "", faction ? `faction=${encodeURIComponent(faction)}` : ""].filter(Boolean).join("&");
  return `${BASE}/api/stream${q ? `?${q}` : ""}`;
}
