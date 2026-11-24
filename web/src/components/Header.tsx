import { useState } from "react";
import { api } from "../api/client";
import { factionColor } from "../lib/format";

// Campaign seconds per real second; labels as in flight sims' time acceleration.
const SPEEDS = [1, 10, 60, 300, 900, 3600];

/** Pause / play and fast-forward. Disabled while a game is live (time runs 1:1). */
function TimeControls({ data }: { data: CampaignData }) {
  const c = data.control;
  const [error, setError] = useState<string>();
  if (!c) return null;
  const change = (x: { paused?: boolean; compression?: number }) =>
    api.control(x).then(() => setError(undefined)).catch((e: Error) => setError(e.message));
  const live = data.live;
  const title = live ? "A game is live: time runs 1:1" : undefined;
  return (
    <div className="time-controls" title={error ?? title}>
      <button className={`play ${c.paused ? "" : "on"}`} disabled={live} onClick={() => change({ paused: !c.paused })} title={c.paused ? "Resume" : "Pause"}>
        {c.paused ? "▶" : "⏸"}
      </button>
      {SPEEDS.map((s) => (
        <button key={s} className={!c.paused && c.compression === s ? "on" : ""} disabled={live} onClick={() => change({ paused: false, compression: s })} title={`${s} s of campaign per second`}>
          {s}×
        </button>
      ))}
      {error && <span className="err">!</span>}
    </div>
  );
}
import type { CampaignData } from "../lib/useCampaign";

export function Header({ data }: { data: CampaignData }) {
  const { campaign, clock, live, connected } = data;
  return (
    <header className="header">
      <div className="brand">
        <span className="logo">DYNACAMP</span>
        <span className="scenario">{campaign?.name ?? "connecting…"}</span>
        {campaign && <span className="theater">{campaign.theater}</span>}
      </div>

      <div className="clock">
        <span className="time">{clock?.label ?? "--"}</span>
        {live ? (
          <span className="badge live">LIVE · 1:1</span>
        ) : data.control?.paused ? (
          <span className="badge paused">PAUSED</span>
        ) : (
          <span className="badge sim">SIMULATION {data.control ? `${data.control.compression}×` : ""}</span>
        )}
        <TimeControls data={data} />
        <span className={`conn ${connected ? "ok" : "ko"}`} title={connected ? "connected" : data.error ?? "disconnected"} />
      </div>

      <div className="scores">
        {campaign?.factions.map((f) => (
          <div key={f.id} className="score" style={{ borderColor: factionColor(campaign, f.id) }}>
            <div className="score-name" style={{ color: factionColor(campaign, f.id) }}>
              {f.name}
            </div>
            <div className="score-stats">
              <span title="victory points">★ {f.victory_points}</span>
              <span title="provinces">▦ {f.provinces}</span>
              <span title="brigades">⚑ {f.divisions}</span>
              <span title="aircraft">✈ {f.aircraft}</span>
            </div>
            <div className="objectives">
              {f.objectives.map((o) => (
                <span key={o.province} className={`objective ${o.achieved ? "done" : ""}`} title={`priority ${o.priority}`}>
                  {o.achieved ? "✔" : "◇"} {o.name}
                </span>
              ))}
            </div>
          </div>
        ))}
        {!!campaign?.players.length && (
          <div className="score players">
            <div className="score-name">Pilots online</div>
            <div className="objectives">
              {campaign.players.map((p) => (
                <span key={p.id} className="objective" style={{ color: factionColor(campaign, p.side === "blue" ? "blue" : "red") }}>
                  {p.name}
                  {p.flight ? ` · ${p.flight}` : ""}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </header>
  );
}
