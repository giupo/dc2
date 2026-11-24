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
  ageLabel,
  intelLabel,
  fmtDist,
  type EventCategory,
  type Units,
} from "../lib/format";
import { affiliation, airSidc, knownStatus, samSidc, symbol, unitSidc } from "../lib/symbols";
import type { Selection } from "../lib/selection";
import type { CampaignData } from "../lib/useCampaign";

export interface PanelProps {
  data: CampaignData;
  selected: Selection;
  select: (s: Selection, at?: number[] | null) => void;
  /** Side shown as friendly in NATO symbols. */
  viewpoint: string;
  /** Display units (SI or kts/ft/NM). */
  units: Units;
}

/** Inline NATO symbol (APP-6) for lists. */
export function UnitSymbol({ d, data, viewpoint, size = 18 }: { d: Division; data: CampaignData; viewpoint: string; size?: number }) {
  // No text modifiers in lists: the name is right next to it.
  const sym = symbol(unitSidc(d.template, affiliation(sideOf(data.campaign, d.faction), viewpoint), knownStatus(d.intel)), { size, infoFields: false });
  return <span className="symbol" dangerouslySetInnerHTML={{ __html: sym.svg }} />;
}

export function FlightSymbol({ f, data, viewpoint }: { f: Flight; data: CampaignData; viewpoint: string }) {
  const unidentified = f.intel != null && !f.intel.identified;
  const aff = unidentified ? "U" : affiliation(sideOf(data.campaign, f.faction), viewpoint);
  const sym = symbol(airSidc(unidentified ? null : f.task, f.helo, aff, knownStatus(f.intel)), { size: 14, infoFields: false });
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
      {campaign?.factions.filter((f) => fronts?.sectors.some((s) => s.faction === f.id)).map((f) => (
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
          {d.intel && <span className={`chip intel ${d.intel.stale ? "stale" : ""}`} title={intelLabel(d.intel)}>{d.intel.stale ? `seen ${ageLabel(d.intel.age_s)} ago` : "located"}</span>}
          {d.battle && <span className="chip battle">BATTLE</span>}
        </div>
        <div className="sub">
          {TEMPLATE_SHORT[d.template] ?? d.template} · {where}
          {d.order.kind !== "hold" && d.order.kind !== "unknown" && ` · ${d.order.kind} → ${target}`}
        </div>
      </div>
      <div className="bars">
        <Bar value={d.strength} title={`strength ${pct(d.strength)}`} />
        {d.org != null && <Bar value={d.org} color="#60a5fa" title={`organisation ${pct(d.org)}`} />}
        {d.supply != null && <Bar value={d.supply} color="#facc15" title={`supply ${pct(d.supply)}`} />}
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
              {d.stock != null ? `${d.stock.toLocaleString()} / ${d.capacity.toLocaleString()} · +${d.production_per_hour}/h · ${d.trucks} trucks` : "enemy depot: stock unknown"}
            </div>
          </div>
          {d.stock != null && <Bar value={d.stock / Math.max(1, d.capacity)} color="#facc15" />}
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

/** Falcon/BMS-style ATO: every flight on one line, by takeoff time. */
function AtoList({ packages, data, selected, select }: PanelProps & { packages: NonNullable<CampaignData["ato"]>["packages"] }) {
  const [showDone, setShowDone] = useState(false);
  const now = data.clock?.t ?? 0;
  const rows = packages
    .flatMap((p) => p.flights.map((f) => ({ p, f })))
    .filter(({ f }) => showDone || (f.status !== "done" && f.status !== "cancelled"))
    .sort((a, b) => a.f.takeoff.t - b.f.takeoff.t || a.p.number.localeCompare(b.p.number));
  return (
    <>
      <label className="muted check">
        <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> show completed
      </label>
      <table className="table ato-list">
        <thead>
          <tr>
            <th>Pkg</th>
            <th>Flight</th>
            <th>Mission</th>
            <th>Aircraft</th>
            <th title="takeoff time">T/O</th>
            <th title="time on target">TOT</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ p, f }) => {
            const soon = f.status === "scheduled" && f.takeoff.t - now < 1800;
            const humans = f.players.filter((x) => x.state !== "left").length;
            return (
              <tr
                key={f.id}
                className={`clickable ${isSel(selected, "flight", f.id) ? "sel" : ""} ${f.status === "done" || f.status === "cancelled" ? "faded" : ""}`}
                onClick={() => select({ kind: "flight", id: f.id }, f.ll)}
                title={`${p.target}${f.seats_free ? ` — ${f.seats_free} seats open` : ""}`}
              >
                <td className="mono" style={{ color: factionColor(data.campaign, p.faction) }}>{p.number}</td>
                <td>
                  {f.callsign}
                  {humans > 0 && <span title="players"> 👤{humans}</span>}
                </td>
                <td><span className={`chip task task-${f.task}`}>{f.task}</span></td>
                <td className="muted ac" title={`${f.size}× ${f.aircraft_name}`}>{f.size}× {f.aircraft_name}</td>
                <td className={`mono ${soon ? "soon" : ""}`}>{f.takeoff.label.slice(3)}</td>
                <td className="mono">{f.tot.label.slice(3)}</td>
                <td><span className={`chip status st-${f.status}`}>{STATUS_LABEL[f.status] ?? f.status}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {!rows.length && <p className="muted pad">No flights.</p>}
    </>
  );
}

export function AtoPanel(props: PanelProps) {
  const { data, selected, select } = props;
  const { ato, campaign } = data;
  const [faction, setFaction] = useState<string>("all");
  const [view, setView] = useState<"list" | "packages">("list");
  const packages = (ato?.packages ?? []).filter((p) => faction === "all" || p.faction === faction);
  return (
    <div className="panel">
      <div className="tabs small">
        {["all", ...(campaign?.factions.map((f) => f.id) ?? [])].map((f) => (
          <button key={f} className={faction === f ? "on" : ""} onClick={() => setFaction(f)} style={f !== "all" ? { color: factionColor(campaign, f) } : undefined}>
            {f === "all" ? "All" : factionName(campaign, f)}
          </button>
        ))}
        <span className="grow" />
        <button className={view === "list" ? "on" : ""} onClick={() => setView("list")} title="Falcon/BMS-style list of flights by takeoff time">ATO</button>
        <button className={view === "packages" ? "on" : ""} onClick={() => setView("packages")}>Packages</button>
      </div>
      <h3>
        Packages ({packages.length}) · airborne {ato?.flights_airborne ?? 0}
      </h3>
      {view === "list" && <AtoList {...props} packages={packages} />}
      {view === "packages" && packages.map((p) => (
        <div key={p.id} className={`package ${isSel(selected, "package", p.id) ? "sel" : ""}`}>
          <div className="package-head clickable" onClick={() => select({ kind: "package", id: p.id }, p.ll)}>
            <span className="side-dot" style={{ background: factionColor(campaign, p.faction) }} />
            <span className="mono pkg-no">{p.number}</span>
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

export function AirDefensePanel({ data, selected, select, viewpoint, units }: PanelProps) {
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
                            {s.range_km > 0 && ` · ${fmtDist(s.range_km, units)}`}
                            {s.networked_to && " · IADS"}
                          </div>
                        </div>
                        {s.missiles_max > 0 && s.missiles != null && <Bar value={s.missiles / s.missiles_max} color="#e879f9" title={`missiles ${Math.round(s.missiles)}/${s.missiles_max}`} />}
                        {s.intel && <span className={`chip intel ${s.intel.stale ? "stale" : ""}`} title={intelLabel(s.intel)}>{ageLabel(s.intel.age_s)}</span>}
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

// ---------------------------------------------------------------- intel

const KIND_LABEL: Record<string, string> = { division: "Brigades", sam: "Air defence", flight: "Air tracks", convoy: "Convoys" };

/** Fog of war: what the viewing faction knows about the enemy, and its sensors. */
export function IntelPanel({ data, selected, select }: PanelProps) {
  const intel = data.intel;
  if (!data.faction || !intel) {
    return <p className="muted pad">Umpire view: everything is visible. Pick a side with fog of war on to see only what its sensors found.</p>;
  }
  const kinds = ["flight", "sam", "division", "convoy"];
  const selKind: Record<string, NonNullable<Selection>["kind"]> = { division: "division", sam: "sam", flight: "flight", convoy: "convoy" };
  const awacs = intel.sensors.filter((s) => s.kind === "awacs");
  return (
    <div>
      <h3>Picture of {factionName(data.campaign, data.faction)}</h3>
      <div className="sub pad">
        {intel.contacts.length} contacts · {intel.sensors.filter((s) => s.kind === "ground_radar").length} ground radars ·{" "}
        {awacs.length ? `AWACS on station (${awacs.map((a) => a.name.replace("AWACS ", "")).join(", ")})` : "no AWACS on station"}
      </div>
      {kinds.map((k) => {
        const list = intel.contacts.filter((c) => c.kind === k).sort((a, b) => a.age_s - b.age_s);
        if (!list.length) return null;
        return (
          <div key={k}>
            <h3>{KIND_LABEL[k]} <span className="muted">{list.length}</span></h3>
            {list.map((c) => {
              const det = c.detail as Record<string, string | number | boolean | undefined>;
              const name = (det.name ?? det.aircraft_name ?? det.system_name ?? (c.kind === "flight" ? `Unknown ${det.helo ? "helicopter" : "aircraft"} (${det.size_class})` : c.id)) as string;
              return (
                <div key={c.id} className={`row clickable contact ${c.stale ? "stale" : ""} ${isSel(selected, selKind[c.kind], c.id) ? "sel" : ""}`} onClick={() => select({ kind: selKind[c.kind], id: c.id } as Selection, c.ll)}>
                  <span className="icon" style={{ color: c.identified ? factionColor(data.campaign, c.faction) : "#facc15" }}>{c.identified ? "◆" : "?"}</span>
                  <div className="grow">
                    <div className="title">{name}</div>
                    <div className="sub">{c.source}{c.kind === "flight" && det.heading_deg != null ? ` · hdg ${det.heading_deg}°` : ""}</div>
                  </div>
                  <span className={`chip intel ${c.stale ? "stale" : ""}`}>{ageLabel(c.age_s)}</span>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
