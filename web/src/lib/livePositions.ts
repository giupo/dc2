// Smooth movement on the map: entity interpolation, as in game netcode.
//
// The server streams compact position samples (`/api/positions/stream`, a few
// per second). The map is drawn a little *in the past* (RENDER_DELAY_SAMPLES
// sample periods) so that there are always two samples around the time being
// drawn, and every frame (requestAnimationFrame, ~60 fps) each marker is placed
// by linear interpolation between them. Nothing goes through React state, so
// animating costs no re-render.
import { useEffect, useRef } from "react";
import { positionsStreamUrl } from "../api/client";

type Group = "d" | "c" | "f" | "s";
type LatLon = number[];

interface Sample {
  at: number; // performance.now() when received
  t: number; // campaign time
  d: Record<string, LatLon>;
  c: Record<string, LatLon>;
  f: Record<string, LatLon>;
  s: Record<string, LatLon>;
}

const HZ = 5;
const RENDER_DELAY_SAMPLES = 2; // draw ~400 ms in the past: robust to jitter
const MAX_SAMPLES = 12;
const MAX_EXTRAPOLATE_MS = 300; // when samples are late, keep moving a little, then wait
const TELEPORT_DEG = 2; // (fast-forward moves jets far between samples: keep animating them) // larger jumps (respawn, new contact position) are not animated

export class LiveFeed {
  samples: Sample[] = [];
  private es?: EventSource;
  private period = 1000 / HZ;

  constructor(faction: string | null) {
    this.es = new EventSource(positionsStreamUrl(HZ, faction));
    this.es.addEventListener("positions", (m) => {
      const s = JSON.parse((m as MessageEvent).data) as Omit<Sample, "at">;
      const now = performance.now();
      const last = this.samples[this.samples.length - 1];
      // Measured period (smoothed): follows the server's real cadence.
      if (last) this.period = 0.8 * this.period + 0.2 * Math.min(2000, Math.max(20, now - last.at));
      this.samples.push({ ...s, at: now });
      if (this.samples.length > MAX_SAMPLES) this.samples.shift();
    });
  }

  close() {
    this.es?.close();
    this.samples = [];
  }

  /** True once samples are flowing. */
  get ready(): boolean {
    return this.samples.length > 0;
  }

  /**
   * Interpolated position of an entity at wall time `now`, as [lng, lat].
   * `key` is "<group>:<id>" (d brigades, c convoys, f flights, s SAM sites).
   * Null if the stream does not (or no longer) carry it.
   */
  position(key: string, now: number): [number, number] | null {
    const n = this.samples.length;
    if (!n) return null;
    const sep = key.indexOf(":");
    const g = key.slice(0, sep) as Group;
    const id = key.slice(sep + 1);
    const latest = this.samples[n - 1];
    if (!latest[g]?.[id]) return null; // gone (or not seen any more)
    const rt = now - RENDER_DELAY_SAMPLES * this.period;
    // first sample after the render time
    let i = this.samples.findIndex((s) => s.at > rt);
    if (i === -1) {
      // render time beyond the newest sample: extrapolate briefly from the last two
      const prev = this.samples[n - 2];
      const a = prev?.[g]?.[id];
      const b = latest[g][id];
      if (!a || n < 2) return [b[1], b[0]];
      const k = Math.min(rt - latest.at, MAX_EXTRAPOLATE_MS) / Math.max(1, latest.at - prev.at);
      return lerp(a, b, 1 + k);
    }
    if (i === 0) {
      const p = this.samples[0][g]?.[id] ?? latest[g][id];
      return [p[1], p[0]];
    }
    const sa = this.samples[i - 1];
    const sb = this.samples[i];
    const a = sa[g]?.[id];
    const b = sb[g]?.[id];
    if (!a || !b) {
      const p = b ?? a ?? latest[g][id];
      return [p[1], p[0]];
    }
    return lerp(a, b, (rt - sa.at) / Math.max(1, sb.at - sa.at));
  }
}

function lerp(a: LatLon, b: LatLon, k: number): [number, number] {
  if (Math.abs(a[0] - b[0]) > TELEPORT_DEG || Math.abs(a[1] - b[1]) > TELEPORT_DEG) return [b[1], b[0]];
  return [a[1] + (b[1] - a[1]) * k, a[0] + (b[0] - a[0]) * k];
}

/** A LiveFeed for a faction's view (null = umpire), reopened when it changes. */
export function useLiveFeed(faction: string | null): React.RefObject<LiveFeed | null> {
  const feed = useRef<LiveFeed | null>(null);
  useEffect(() => {
    const f = new LiveFeed(faction);
    feed.current = f;
    (window as unknown as { __dynacampFeed?: LiveFeed }).__dynacampFeed = f; // for debugging in the console
    return () => {
      f.close();
      if (feed.current === f) feed.current = null;
    };
  }, [faction]);
  return feed;
}
