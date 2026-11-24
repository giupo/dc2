import { useEffect, useRef, useState } from "react";
import {
  api,
  streamUrl,
  type AirDefense,
  type Ato,
  type Campaign,
  type CampaignEvent,
  type Forces,
  type Fronts,
  type Logistics,
  type MapData,
  type Tick,
  type Time,
} from "../api/client";

export interface CampaignData {
  map?: MapData;
  campaign?: Campaign;
  fronts?: Fronts;
  forces?: Forces;
  logistics?: Logistics;
  ato?: Ato;
  airDefense?: AirDefense;
  events: CampaignEvent[];
  clock?: Time;
  live: boolean;
  connected: boolean;
  error?: string;
}

const REFRESH_MS = 3000; // positions move without events: poll the views
const EVENT_FLUSH_MS = 400; // batch stream events into React state
const EVENT_HISTORY = 400;
const EVENT_BACKLOG = 200; // replayed when the page opens

/**
 * Keeps the whole campaign picture up to date:
 * - geometry (`/api/map`) once, and again when a province changes hands;
 * - the dynamic views every few seconds, and soon after relevant events;
 * - clock and event feed from the Server-Sent Events stream.
 */
export function useCampaign(): CampaignData {
  const [data, setData] = useState<CampaignData>({ events: [], live: false, connected: false });
  const inFlight = useRef(false);
  const soon = useRef<number | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const set = (patch: Partial<CampaignData>) => !cancelled && setData((d) => ({ ...d, ...patch }));

    const loadMap = () => api.map().then((map) => set({ map })).catch(() => undefined);

    const refresh = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const [campaign, fronts, forces, logistics, ato, airDefense] = await Promise.all([
          api.campaign(),
          api.fronts(),
          api.forces(),
          api.logistics(),
          api.ato(),
          api.airDefense(),
        ]);
        set({ campaign, fronts, forces, logistics, ato, airDefense, clock: campaign.time, live: campaign.live, connected: true, error: undefined });
      } catch (e) {
        set({ connected: false, error: e instanceof Error ? e.message : String(e) });
      } finally {
        inFlight.current = false;
      }
    };
    const refreshSoon = () => {
      window.clearTimeout(soon.current);
      soon.current = window.setTimeout(refresh, 300);
    };

    loadMap();
    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);

    // ---- event stream
    let es: EventSource | undefined;
    let buffer: CampaignEvent[] = [];
    const flush = window.setInterval(() => {
      if (!buffer.length) return;
      const batch = buffer;
      buffer = [];
      if (batch.some((e) => e.kind === "province_captured")) loadMap();
      if (batch.some((e) => RELEVANT.has(e.kind))) refreshSoon();
      if (!cancelled) setData((d) => ({ ...d, events: [...d.events, ...batch].slice(-EVENT_HISTORY) }));
    }, EVENT_FLUSH_MS);

    api
      .campaign()
      .then((c) => {
        if (cancelled) return;
        es = new EventSource(streamUrl(Math.max(0, c.events - EVENT_BACKLOG)));
        es.addEventListener("event", (m) => buffer.push(JSON.parse((m as MessageEvent).data)));
        es.addEventListener("tick", (m) => {
          const t = JSON.parse((m as MessageEvent).data) as Tick;
          set({ clock: t.time, live: t.live, connected: true });
        });
        es.onerror = () => set({ connected: false });
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.clearInterval(flush);
      window.clearTimeout(soon.current);
      es?.close();
    };
  }, []);

  return data;
}

const RELEVANT = new Set([
  "province_captured",
  "battle_started",
  "battle_ended",
  "division_retreated",
  "division_destroyed",
  "convoy_dispatched",
  "convoy_destroyed",
  "package_planned",
  "flight_takeoff",
  "flight_complete",
  "seat_taken",
  "handoff",
  "sam_engagement",
  "sam_destroyed",
  "sam_relocating",
  "harm_strike",
]);
