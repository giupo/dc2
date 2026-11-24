import { factionColor } from "../lib/format";
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
        {live ? <span className="badge live">LIVE · 1:1</span> : <span className="badge sim">SIMULATION</span>}
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
