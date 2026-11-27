import type { ReactNode } from "react";
import { battleTitle, factionColor, factionName, fmtAlt, fmtDist, pct, coalitionOf, intelLabel } from "../lib/format";
import { affiliation, designation, symbol, unitSidc } from "../lib/symbols";
import type { PanelProps } from "./Panels";
import { Bar, DivisionRow } from "./Panels";
import { PackageOrders } from "./Planner";

function KV({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="kv">
      <span>{k}</span>
      <b>{children}</b>
    </div>
  );
}

/** Floating card describing whatever is selected on the map or in a list. */
export function Details(props: PanelProps & { close: () => void; openBriefing?: (flightId: string) => void }) {
  const { data, selected, select, close, viewpoint, openBriefing, units, archive } = props;
  const { campaign, map, forces, logistics, ato, fronts } = data;
  if (!selected) return null;
  const prov = (id: string | null | undefined) => map?.provinces.find((p) => p.id === id);
  const provName = (id: string | null | undefined) => prov(id)?.name ?? id ?? "—";
  // Live packages first, then the finished ones fetched from the archive.
  const packages = [...(ato?.packages ?? []), ...archive.filter((a) => !ato?.packages.some((p) => p.id === a.id))];
  const flights = [...packages.flatMap((p) => p.flights), ...(ato?.tracks ?? [])];

  let title = "";
  let color = "#9ca3af";
  let body: ReactNode = null;

  switch (selected.kind) {
    case "division": {
      const d = forces?.divisions.find((x) => x.id === selected.id);
      if (!d) break;
      title = d.name;
      color = factionColor(campaign, d.faction);
      body = (
        <>
          <div className="detail-head">
            <span
              dangerouslySetInnerHTML={{
                __html: symbol(unitSidc(d.template, affiliation(coalitionOf(campaign, d.faction), viewpoint)), { size: 34, uniqueDesignation: designation(d.name) }).svg,
              }}
            />
            <div>
              <div>{d.template_name}</div>
              <div className="muted">{factionName(campaign, d.faction)}</div>
            </div>
          </div>
          <KV k="Location">{provName(d.location)}</KV>
          {d.intel && <KV k="Intel">{intelLabel(d.intel)}</KV>}
          {d.order.kind !== "unknown" && <KV k="Order">{d.order.kind === "hold" ? "hold" : `${d.order.kind} → ${provName(d.order.target)}`}</KV>}
          <KV k={d.intel ? "Strength (est.)" : "Strength"}><Bar value={d.strength} /> {pct(d.strength)}</KV>
          {d.org != null && <KV k="Organisation"><Bar value={d.org} color="#60a5fa" /> {pct(d.org)}</KV>}
          {d.supply != null && <KV k="Supply"><Bar value={d.supply} color="#facc15" /> {pct(d.supply)}</KV>}
          {d.battle && <KV k="Battle">{d.battle}</KV>}
          <KV k="Simulated by">{d.authority === "campaign" ? "campaign" : `game (${d.authority})`}</KV>
          <div className="elements">
            {Object.entries(d.elements)
              .filter(([, n]) => n >= 0.5)
              .map(([k, n]) => (
                <span key={k}>
                  {k} <b>{Math.round(n)}</b>
                </span>
              ))}
          </div>
        </>
      );
      break;
    }
    case "flight":
    case "package": {
      const pkg = selected.kind === "package" ? packages.find((p) => p.id === selected.id) : packages.find((p) => p.flights.some((f) => f.id === selected.id));
      const f = selected.kind === "flight" ? flights.find((x) => x.id === selected.id) : undefined;
      if (f?.intel) {
        // an enemy radar track: only what the sensors tell
        color = f.intel.identified ? factionColor(campaign, f.faction) : "#facc15";
        title = f.intel.identified ? `${f.callsign} — ${f.aircraft_name}` : f.callsign;
        body = (
          <>
            <KV k="Intel">{intelLabel(f.intel)}</KV>
            <KV k="Type">{f.aircraft_name}</KV>
            {f.intel.identified && <KV k="Task">{f.task}</KV>}
            <KV k="Size">{f.intel.identified ? f.size : f.size <= 1 ? "single" : f.size === 2 ? "pair" : "group"}</KV>
            <KV k="First seen">{f.takeoff.label}</KV>
          </>
        );
        break;
      }
      if (!pkg) break;
      color = factionColor(campaign, pkg.faction);
      title = f ? `${f.callsign} — ${f.aircraft_name}` : `Package ${pkg.number} · ${pkg.task}`;
      body = (
        <>
          <KV k="Package">#{pkg.number} · {pkg.task} · {pkg.target}</KV>
          <KV k="TOT">{pkg.tot.label}</KV>
          {pkg.archived && <KV k="Mission">over (archived)</KV>}
          {!pkg.archived && <PackageOrders pkg={pkg} flight={f} />}
          {f && openBriefing && !pkg.archived && (
            <button className="primary" onClick={() => openBriefing(f.id)}>
              📋 Mission briefing
            </button>
          )}
          {f && (
            <>
              <KV k="Task">{f.task}</KV>
              <KV k="Status">{f.status}</KV>
              <KV k="Squadron">
                {f.squadron_insignia && <img className="kv-insignia" src={f.squadron_insignia} alt="" />}
                {f.squadron_name}
              </KV>
              <KV k="Aircraft">{f.size} ({f.ai} AI, {f.players.filter((p) => p.state !== "left").length} players)</KV>
              <KV k="Takeoff">{f.takeoff.label}</KV>
              {f.on_station_until && <KV k="On station until">{f.on_station_until.label}</KV>}
              <KV k="Open seats">{f.seats_free}</KV>
              {f.players.map((p) => (
                <KV key={p.id} k="Pilot">{p.name} · {p.state}</KV>
              ))}
            </>
          )}
          <div className="sub-list">
            {pkg.flights.map((x) => (
              <div key={x.id} className={`row clickable ${x.id === f?.id ? "sel" : ""}`} onClick={() => select({ kind: "flight", id: x.id }, x.ll)}>
                <span className={`chip task task-${x.task}`}>{x.task}</span>
                <span className="grow">{x.callsign} · {x.size}× {x.aircraft_name}</span>
                <span className="muted">{x.status}</span>
              </div>
            ))}
          </div>
        </>
      );
      break;
    }
    case "convoy": {
      const c = logistics?.convoys.find((x) => x.id === selected.id);
      if (!c) break;
      title = `Convoy ${c.id}`;
      color = factionColor(campaign, c.faction);
      body = (
        <>
          <KV k="Route">{provName(c.home)} → {provName(c.destination)}</KV>
          <KV k="Heading to">{provName(c.heading_to)} {c.returning && "(returning)"}</KV>
          {c.intel && <KV k="Intel">{intelLabel(c.intel)}</KV>}
          <KV k="Trucks">{Math.round(c.trucks)}</KV>
          {!c.intel && <KV k="Cargo">{Math.round(c.cargo)} supply</KV>}
          <KV k="Simulated by">{c.authority === "campaign" ? "campaign" : `game (${c.authority})`}</KV>
        </>
      );
      break;
    }
    case "province": {
      const p = prov(selected.id);
      if (!p) break;
      title = p.name;
      color = factionColor(campaign, p.controller);
      const depot = logistics?.depots.find((d) => d.province === p.id);
      const here = forces?.divisions.filter((d) => d.location === p.id) ?? [];
      body = (
        <>
          <KV k="Held by">{factionName(campaign, p.controller)}</KV>
          {p.owner !== p.controller && <KV k="Owner">{factionName(campaign, p.owner)}</KV>}
          <KV k="Terrain">{p.terrain}</KV>
          <KV k="Infrastructure">{p.infrastructure} / 5</KV>
          <KV k="Victory points">{p.victory_points}</KV>
          {depot && <KV k="Depot">{depot.stock != null ? `${depot.stock.toLocaleString()} / ${depot.capacity.toLocaleString()} · ${depot.trucks} trucks` : "enemy depot"}</KV>}
          <KV k="Neighbours">{p.neighbors.map(provName).join(", ")}</KV>
          {here.length > 0 && <div className="sub-list">{here.map((d) => <DivisionRow key={d.id} d={d} {...props} />)}</div>}
        </>
      );
      break;
    }
    case "battle": {
      const b = fronts?.battles.find((x) => x.id === selected.id);
      if (!b) break;
      title = battleTitle(b);
      color = "#f59e0b";
      const divs = (ids: string[]) => forces?.divisions.filter((d) => ids.includes(d.id)) ?? [];
      body = (
        <>
          <KV k="Started">{b.started.label} ({b.hours} h)</KV>
          {b.delegated && <KV k="Fought by">the game (player bubble)</KV>}
          <div className="group" style={{ color: factionColor(campaign, b.attacker) }}>Attackers · {factionName(campaign, b.attacker)}</div>
          <div className="sub-list">{divs(b.attackers).map((d) => <DivisionRow key={d.id} d={d} {...props} />)}</div>
          <div className="group" style={{ color: factionColor(campaign, b.defender) }}>Defenders · {factionName(campaign, b.defender)}</div>
          <div className="sub-list">{divs(b.defenders).map((d) => <DivisionRow key={d.id} d={d} {...props} />)}</div>
        </>
      );
      break;
    }
    case "sam": {
      const u = data.airDefense?.sites.find((x) => x.id === selected.id);
      if (!u) break;
      title = `${u.system_name} — ${u.name}`;
      color = factionColor(campaign, u.faction);
      const node = u.networked_to ? data.airDefense?.sites.find((x) => x.id === u.networked_to) : undefined;
      body = (
        <>
          <KV k="Status">{u.status.replace("_", " ")}{u.emitting ? " · radar on" : " · radar off"}</KV>
          <KV k="Location">{provName(u.location)}</KV>
          {u.range_km > 0 && <KV k="Engagement range">{fmtDist(u.range_km, units)} · up to {fmtAlt(u.ceiling_m, units)}</KV>}
          <KV k="Detection">{fmtDist(u.detection_km, units)}</KV>
          {u.intel && <KV k="Intel">{intelLabel(u.intel)}</KV>}
          {u.missiles_max > 0 && u.missiles != null && (
            <KV k="Missiles">
              <Bar value={u.missiles / u.missiles_max} color="#e879f9" /> {Math.round(u.missiles)}/{u.missiles_max}
            </KV>
          )}
          <KV k="IADS">{node ? `cued by ${node.system_name} (${node.name})` : u.role === "ewr" ? "network node" : "autonomous"}</KV>
          <KV k="Simulated by">{u.authority === "campaign" ? "campaign" : `game (${u.authority})`}</KV>
          <div className="elements">
            {Object.entries(u.elements).map(([k, n]) => (
              <span key={k}>
                {k.replace("_", " ")} <b>{Math.round(n)}</b>
              </span>
            ))}
          </div>
        </>
      );
      break;
    }
    case "airbase": {
      const a = map?.airbases.find((x) => x.id === selected.id);
      if (!a) break;
      title = a.name;
      color = factionColor(campaign, a.owner);
      const sqs = ato?.squadrons.filter((s) => s.airbase === a.id) ?? [];
      body = (
        <>
          <KV k="Owner">{factionName(campaign, a.owner)}</KV>
          {a.province && <KV k="Province">{provName(a.province)}</KV>}
          {sqs.map((s) => (
            <KV key={s.id} k={s.name}>
              {s.aircraft_name} · {s.available}/{s.strength} ready
            </KV>
          ))}
        </>
      );
      break;
    }
  }

  if (!body) return null;
  return (
    <div className="details" style={{ borderTopColor: color }}>
      <div className="details-title">
        <span style={{ color }}>{title}</span>
        <button onClick={close} title="close">✕</button>
      </div>
      <div className="details-body">{body}</div>
    </div>
  );
}
