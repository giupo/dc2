import { useMemo, useState } from "react";
import type { Division, Flight } from "../api/client";
import {
  CATEGORY_ICON,
  describeEvent,
  eventCategory,
  factionColor,
  factionName,
  levelColor,
  pct,
  sideOf,
  TEMPLATE_SHORT,
  type EventCategory,
} from "../lib/format";
import { affiliation, airSidc, samSidc, symbol, unitSidc } from "../lib/symbols";
import type { Selection } from "../lib/selection";
import type { CampaignData } from "../lib/useCampaign";

export interface PanelProps {
  data: CampaignData;
  selected: Selection;
  select: (s: Selection, at?: number[] | null) => void;
  /** Side shown as friendly in NATO symbols. */
  viewpoint: string;
}

/** Inline NATO symbol (APP-6) for lists. */
export function UnitSymbol({ d, data, viewpoint, size = 18 }: { d: Division; data: CampaignData; viewpoint: string; size?: number }) {
  // No text modifiers in lists: the name is right next to it.
  const sym = symbol(unitSidc(d.template, affiliation(sideOf(data.campaign, d.faction), viewpoint)), { size, infoFields: false });
  return <span className="symbol" dangerouslySetInnerHTML={{ __html: sym.svg }} />;
}

export function FlightSymbol({ f, data, viewpoint }: { f: Flight; data: CampaignData; viewpoint: string }) {
  const sym = symbol(airSidc(f.task, f.helo, affiliation(sideOf(data.campaign, f.faction), viewpoint)), { size: 14, infoFields: false });
  return <span className="symbol air" dangerouslySetInnerHTML={{ __html: sym.svg }} />;
}

export function Bar({ value, color, title }: { value: number; color?: string; title?: string }) {
  return (
    <span className="bar" title={title ?? pct(value)}>
      <i style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, background: color ?? levelColor(value) }} />
    </span>
  );
}

const isSel = (s: Selection, kind: string, id: string) => s?.kind === kind && s.id === id;

// ---------------------------------------------------------------- fronts

export function FrontPanel({ data, selected, select }: PanelProps) {
  const { fronts, campaign, map } = data;
  const prov = (id: string) => map?.provinces.find((p) => p.id === id);
  return (
    <div className="panel">
      <h3>Battles</h3>
      {!fronts?.battles.length && <p className="muted">No battles in progress.</p>}
      {fronts?.battles.map((b) => (
        <div key={b.id} className={`row clickable ${isSel(selected, "battle", b.id) ? "sel" : ""}`} onClick={() => select({ kind: "battle", id: b.id }, b.ll)}>
          <span className="icon">⚔</span>
          <div className="grow">
            <div className="title">
              {b.name} {b.delegated && <span className="chip game">IN GAME</span>}
            </div>
            <div className="sub">
              <span style={{ color: factionColor(campaign, b.attacker) }}>{b.attackers.length} att.</span> vs{" "}
              <span style={{ color: factionColor(campaign, b.defender) }}>{b.defenders.length} def.</span> · {b.hours} h
            </div>
          </div>
        </div>
      ))}
      <h3>Front sectors</h3>
      {campaign?.factions.map((f) => (
        <div key={f.id}>
          <div className="group" style={{ color: factionColor(campaign, f.id) }}>
            {f.name}
          </div>
          {fronts?.sectors
            .filter((s) => s.faction === f.id)
            .sort((a, b) => b.enemy_power - b.own_power - (a.enemy_power - a.own_power))
            .map((s) => {
              const ratio = s.own_power / Math.max(1, s.own_power + s.enemy_power);
              return (
                <div key={s.province} className="row clickable" onClick={() => select({ kind: "province", id: s.province }, prov(s.province)?.ll)}>
                  <div className="grow">
                    <div className="title">{s.name}</div>
                    <div className="sub">
                      own {s.own_power} · enemy {s.enemy_power} · {s.divisions.length} bde
                    </div>
                  </div>
                  <Bar value={ratio} color={ratio >= 0.5 ? "#22c55e" : ratio >= 0.33 ? "#eab308" : "#ef4444"} title={`force ratio ${pct(ratio)}`} />
                </div>
              );
            })}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- forces

export function DivisionRow({ d, data, selected, select, viewpoint }: PanelProps & { d: Division }) {
  const { map } = data;
  const where = map?.provinces.find((p) => p.id === d.location)?.name ?? d.location;
  const target = d.order.target ? (map?.provinces.find((p) => p.id === d.order.target)?.name ?? d.order.target) : "";
  return (
    <div className={`row clickable ${isSel(selected, "division", d.id) ? "sel" : ""}`} onClick={() => select({ kind: "division", id: d.id }, d.ll)}>
      <UnitSymbol d={d} data={data} viewpoint={viewpoint} />
      <div className="grow">
        <div className="title">
          {d.name} {d.authority !== "campaign" && <span className="chip game">IN GAME</span>}
          {d.battle && <span className="chip battle">BATTLE</span>}
        </div>
        <div className="sub">
          {TEMPLATE_SHORT[d.template] ?? d.template} · {where}
          {d.order.kind !== "hold" && ` · ${d.order.kind} → ${target}`}
        </div>
      </div>
      <div className="bars">
        <Bar value={d.strength} title={`strength ${pct(d.strength)}`} />
        <Bar value={d.org} color="#60a5fa" title={`organisation ${pct(d.org)}`} />
        <Bar value={d.supply} color="#facc15" title={`supply ${pct(d.supply)}`} />
      </div>
    </div>
  );
}

export function ForcesPanel(props: PanelProps) {
  const { campaign, forces } = props.data;
  return (
    <div className="panel">
      <div className="legend">
        <span><i style={{ background: "#22c55e" }} /> strength</span>
        <span><i style={{ background: "#60a5fa" }} /> organisation</span>
        <span><i style={{ background: "#facc15" }} /> supply</span>
      </div>
      {campaign?.factions.map((f) => (
        <div key={f.id}>
          <h3 style={{ color: factionColor(campaign, f.id) }}>{f.name}</h3>
          {forces?.divisions
            .filter((d) => d.faction === f.id)
            .map((d) => <DivisionRow key={d.id} d={d} {...props} />)}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- logistics

export function LogisticsPanel({ data, selected, select }: PanelProps) {
  const { logistics, campaign, map } = data;
  const prov = (id: string) => map?.provinces.find((p) => p.id === id)?.name ?? id;
  const short = logistics?.divisions.filter((d) => d.supply < 0.35) ?? [];
  return (
    <div className="panel">
      <h3>Depots</h3>
      {logistics?.depots.map((d) => (
        <div key={d.province} className={`row clickable ${isSel(selected, "depot", d.province) ? "sel" : ""}`} onClick={() => select({ kind: "province", id: d.province }, d.ll)}>
          <span className="icon" style={{ color: factionColor(campaign, d.controller) }}>⛁</span>
          <div className="grow">
            <div className="title">{d.name}</div>
            <div className="sub">
              {d.stock.toLocaleString()} / {d.capacity.toLocaleString()} · +{d.production_per_hour}/h · {d.trucks} trucks
            </div>
          </div>
          <Bar value={d.stock / Math.max(1, d.capacity)} color="#facc15" />
        </div>
      ))}
      <h3>Convoys on the road ({logistics?.convoys.length ?? 0})</h3>
      {logistics?.convoys.map((c) => (
        <div key={c.id} className={`row clickable ${isSel(selected, "convoy", c.id) ? "sel" : ""}`} onClick={() => select({ kind: "convoy", id: c.id }, c.ll)}>
          <span className="icon" style={{ color: factionColor(campaign, c.faction) }}>⛟</span>
          <div className="grow">
            <div className="title">
              {c.id} {c.returning ? "↩ returning" : `→ ${prov(c.destination)}`}
              {c.authority !== "campaign" && <span className="chip game">IN GAME</span>}
            </div>
            <div className="sub">
              {Math.round(c.trucks)} trucks · {Math.round(c.cargo)} supply · from {prov(c.home)}
            </div>
          </div>
        </div>
      ))}
      <h3>Supply warnings</h3>
      {!short.length && <p className="muted">All brigades adequately supplied.</p>}
      {short.map((d) => (
        <div key={d.id} className="row clickable" onClick={() => select({ kind: "division", id: d.id }, data.forces?.divisions.find((x) => x.id === d.id)?.ll)}>
          <span className="icon" style={{ color: factionColor(campaign, d.faction) }}>⚠</span>
          <div className="grow">
            <div className="title">{data.forces?.divisions.find((x) => x.id === d.id)?.name ?? d.id}</div>
            <div className="sub">
              {prov(d.location)} · {d.out_of_supply ? "OUT OF SUPPLY" : pct(d.supply)} · {d.direct ? "depot in range" : d.convoy_incoming ? "convoy inbound" : "no convoy"}
            </div>
          </div>
          <Bar value={d.supply} color="#facc15" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- ATO

const STATUS_LABEL: Record<string, string> = {
  scheduled: "SCHED",
  airborne: "AIRBORNE",
  on_station: "ON STN",
  rtb: "RTB",
  done: "DONE",
  cancelled: "CNX",
};

function FlightRow({ f, data, selected, select, viewpoint }: PanelProps & { f: Flight }) {
  const humans = f.players.filter((p) => p.state !== "left");
  return (
    <div className={`row flight-row clickable ${isSel(selected, "flight", f.id) ? "sel" : ""}`} onClick={() => select({ kind: "flight", id: f.id }, f.ll)}>
      <FlightSymbol f={f} data={data} viewpoint={viewpoint} />
      <span className={`chip task task-${f.task}`}>{f.task}</span>
      <div className="grow">
        <div className="title">
          {f.callsign} <span className="muted">{f.size}× {f.aircraft_name}</span>
          {f.authority !== "campaign" && <span className="chip game">IN GAME</span>}
        </div>
        <div className="sub">
          T/O {f.takeoff.label.slice(3)} · TOT {f.tot.label.slice(3)}
          {humans.length > 0 && ` · 👤 ${humans.map((p) => p.name).join(", ")}`}
          {f.seats_free > 0 && <span className="seats"> · {f.seats_free} seats open</span>}
        </div>
      </div>
      <span className={`chip status st-${f.status}`}>{STATUS_LABEL[f.status] ?? f.status}</span>
      {f.faction && <span className="side-dot" style={{ background: factionColor(data.campaign, f.faction) }} />}
    </div>
  );
}

export function AtoPanel(props: PanelProps) {
  const { data, selected, select } = props;
  const { ato, campaign } = data;
  const [faction, setFaction] = useState<string>("all");
  const packages = (ato?.packages ?? []).filter((p) => faction === "all" || p.faction === faction);
  return (
    <div className="panel">
      <div className="tabs small">
        {["all", ...(campaign?.factions.map((f) => f.id) ?? [])].map((f) => (
          <button key={f} className={faction === f ? "on" : ""} onClick={() => setFaction(f)} style={f !== "all" ? { color: factionColor(campaign, f) } : undefined}>
            {f === "all" ? "All" : factionName(campaign, f)}
          </button>
        ))}
      </div>
      <h3>
        Packages ({packages.length}) · airborne {ato?.flights_airborne ?? 0}
      </h3>
      {packages.map((p) => (
        <div key={p.id} className={`package ${isSel(selected, "package", p.id) ? "sel" : ""}`}>
          <div className="package-head clickable" onClick={() => select({ kind: "package", id: p.id }, p.ll)}>
            <span className="side-dot" style={{ background: factionColor(campaign, p.faction) }} />
            <span className={`chip task task-${p.task}`}>{p.task}</span>
            <span className="grow title">{p.target}</span>
            <span className="muted">TOT {p.tot.label.slice(3)}</span>
          </div>
          {p.flights.map((f) => (
            <FlightRow key={f.id} f={f} {...props} />
          ))}
        </div>
      ))}
      <h3>Squadrons</h3>
      <table className="table">
        <thead>
          <tr>
            <th></th>
            <th>Squadron</th>
            <th>Type</th>
            <th title="available / strength">Avail</th>
            <th title="being turned around">Svc</th>
          </tr>
        </thead>
        <tbody>
          {ato?.squadrons
            .filter((s) => faction === "all" || s.faction === faction)
            .map((s) => (
              <tr key={s.id}>
                <td>
                  <span className="side-dot" style={{ background: factionColor(campaign, s.faction) }} />
                </td>
                <td>{s.name}</td>
                <td>{s.aircraft_name}</td>
                <td>
                  {s.available}/{s.strength}
                </td>
                <td>{s.servicing || ""}</td>
              </tr>
            ))}
        </tbody>
      </table>
      <p className="muted">
        Sortie budget left (24 h):{" "}
        {Object.entries(ato?.sortie_budget_left ?? {})
          .map(([f, n]) => `${factionName(campaign, f)} ${n}`)
          .join(" · ")}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- events

const CATEGORIES: EventCategory[] = ["ground", "fires", "logistics", "air", "players", "system", "orders"];

export function EventsPanel({ data }: PanelProps) {
  const [shown, setShown] = useState<Set<EventCategory>>(new Set(["ground", "air", "players", "system"]));
  const events = useMemo(
    () => [...data.events].reverse().filter((e) => shown.has(eventCategory(e.kind))),
    [data.events, shown],
  );
  const toggle = (c: EventCategory) =>
    setShown((s) => {
      const n = new Set(s);
      if (n.has(c)) n.delete(c);
      else n.add(c);
      return n;
    });
  return (
    <div className="panel">
      <div className="tabs small">
        {CATEGORIES.map((c) => (
          <button key={c} className={shown.has(c) ? "on" : ""} onClick={() => toggle(c)}>
            {CATEGORY_ICON[c]} {c}
          </button>
        ))}
      </div>
      {events.map((e) => (
        <div key={e.seq} className={`event ev-${eventCategory(e.kind)}`}>
          <span className="ev-time">{e.time.label.slice(3)}</span>
          <span className="ev-icon">{CATEGORY_ICON[eventCategory(e.kind)]}</span>
          <span className="ev-text">{describeEvent(e, data.campaign, data.map)}</span>
        </div>
      ))}
      {!events.length && <p className="muted">Waiting for events…</p>}
    </div>
  );
}

// ---------------------------------------------------------------- air defence

const SAM_STATUS: Record<string, string> = { active: "ACTIVE", setting_up: "SETUP", moving: "MOVING", suppressed: "SUPPR" };
const ROLE_LABEL: Record<string, string> = { long: "Long range", medium: "Medium range", short: "Short range", ewr: "Early warning" };

export function AirDefensePanel({ data, selected, select, viewpoint }: PanelProps) {
  const { airDefense, campaign, map } = data;
  const prov = (id: string) => map?.provinces.find((p) => p.id === id)?.name ?? id;
  return (
    <div className="panel">
      {campaign?.factions.map((f) => {
        const sites = (airDefense?.sites ?? []).filter((s) => s.faction === f.id);
        const linked = sites.filter((s) => s.networked_to).length;
        return (
          <div key={f.id}>
            <h3 style={{ color: factionColor(campaign, f.id) }}>
              {f.name} · {sites.length} sites · {linked} on IADS
            </h3>
            {(["long", "medium", "short", "ewr"] as const).map((role) => {
              const list = sites.filter((s) => s.role === role);
              if (!list.length) return null;
              return (
                <div key={role}>
                  <div className="group muted">{ROLE_LABEL[role]}</div>
                  {list.map((s) => {
                    const sym = symbol(samSidc(s.role, affiliation(sideOf(campaign, s.faction), viewpoint)), { size: 15, infoFields: false });
                    return (
                      <div key={s.id} className={`row clickable ${isSel(selected, "sam", s.id) ? "sel" : ""}`} onClick={() => select({ kind: "sam", id: s.id }, s.ll)}>
                        <span className="symbol" dangerouslySetInnerHTML={{ __html: sym.svg }} />
                        <div className="grow">
                          <div className="title">
                            {s.system_name} {s.authority !== "campaign" && <span className="chip game">IN GAME</span>}
                          </div>
                          <div className="sub">
                            {s.name} · {prov(s.location)}
                            {s.range_km > 0 && ` · ${s.range_km} km`}
                            {s.networked_to && " · IADS"}
                          </div>
                        </div>
                        {s.missiles_max > 0 && <Bar value={s.missiles / s.missiles_max} color="#e879f9" title={`missiles ${Math.round(s.missiles)}/${s.missiles_max}`} />}
                        <span className={`chip st-${s.status}`}>{SAM_STATUS[s.status] ?? s.status}</span>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
