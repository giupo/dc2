import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type Briefing } from "../api/client";
import { fmtAlt, fmtSpeed, type Units } from "../lib/format";

const SEVERITY_COLOR: Record<string, string> = { high: "#ef4444", medium: "#f59e0b", low: "#9ca3af" };

/** Hook: the mission package of a flight, refreshed every 10 s while shown. */
export function useBriefing(flightId: string | null, units: Units): Briefing | undefined {
  const [b, setB] = useState<Briefing>();
  useEffect(() => {
    setB(undefined);
    if (!flightId) return;
    let alive = true;
    const load = () => api.briefing(flightId, units).then((x) => alive && setB(x)).catch(() => undefined);
    load();
    const t = window.setInterval(load, 10_000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [flightId, units]);
  return b;
}

/** Kneeboard-style mission briefing (printable). */
export function BriefingCard({ b, close }: { b: Briefing; close: () => void }) {
  // Rendered on <body>, outside the app layout: on paper it is the only thing
  // printed, with no offset or clipping from the map container.
  return createPortal(
    <div className="briefing-backdrop" onClick={close}>
      <article className="briefing" onClick={(e) => e.stopPropagation()}>
        <header className="briefing-head">
          {b.squadron_insignia && <img className="briefing-insignia" src={b.squadron_insignia} alt={`${b.squadron} insignia`} title={b.squadron} />}
          <div className="briefing-title">
            <div className="briefing-kicker">PACKAGE {b.package_number} · MISSION BRIEFING · {b.task}</div>
            <h2>{b.title}</h2>
            <div className="briefing-sub">
              <b>{b.callsign}</b> · {b.size}× {b.aircraft} · {b.squadron} · {b.base}
            </div>
          </div>
          <div className="briefing-times">
            <div>
              <span>TAKEOFF</span>
              <b>{b.takeoff.label}</b>
            </div>
            <div>
              <span>TOT</span>
              <b>{b.tot.label}</b>
            </div>
          </div>
          <div className="briefing-actions no-print">
            <button onClick={() => window.print()}>Print</button>
            <button onClick={close}>✕</button>
          </div>
        </header>

        <section>
          <h3>Mission</h3>
          <p>{b.mission}</p>
          <h3>Situation</h3>
          <ul>{b.situation.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </section>

        <section className="two">
          <div>
            <h3>Target</h3>
            <p>
              <b>{b.target.name}</b>
            </p>
            <p className="mono">{b.target.coordinates}</p>
            <p className="muted">{b.target.from_base} · BULLS {b.target.bulls}</p>
            {b.target.description && <p>{b.target.description}</p>}
            {!!b.target.composition.length && <ul className="compact">{b.target.composition.map((c, i) => <li key={i}>{c}</li>)}</ul>}
          </div>
          <div>
            <h3>Threats</h3>
            {!b.threats.length && <p className="muted">None known.</p>}
            <ul className="threats">
              {b.threats.map((t, i) => (
                <li key={i}>
                  <span className="sev" style={{ background: SEVERITY_COLOR[t.severity] }}>{t.severity}</span>
                  <b>{t.name}</b> <span className="mono">BULLS {t.bulls}</span> <span className="muted">{t.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section>
          <h3>Flight plan</h3>
          <p className="muted">Bullseye <span className="mono">{b.bullseye.coordinates}</span>; BULLS bearings magnetic, ranges in {b.units === "imperial" ? "NM" : "km"}.</p>
          <table className="table wp">
            <thead>
              <tr>
                <th>WP</th>
                <th>Name</th>
                <th>ETA</th>
                <th>Alt</th>
                <th>Speed</th>
                <th>Position</th>
                <th>BULLS</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {b.waypoints.map((w) => (
                <tr key={w.index} className={w.name === "TARGET" || w.name === "CAP STATION" ? "hl" : ""}>
                  <td>{w.index}</td>
                  <td>{w.name}</td>
                  <td className="mono">{w.eta.label}</td>
                  <td className="mono">{w.alt_m ? fmtAlt(w.alt_m, b.units) : "-"}</td>
                  <td className="mono">{fmtSpeed(w.speed_kmh, b.units)}</td>
                  <td className="mono">{w.ll ? `${w.ll[0].toFixed(4)}, ${w.ll[1].toFixed(4)}` : "-"}</td>
                  <td className="mono">{w.bulls}</td>
                  <td className="muted">{w.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="two">
          <div>
            <h3>Ordnance</h3>
            {b.loadout.length ? (
              <ul className="compact">
                {b.loadout.map((s, i) => (
                  <li key={i}>
                    <b>{s.count}×</b> {s.store}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">{b.loadout_note}</p>
            )}
          </div>
          <div>
            <h3>Package</h3>
            <ul className="compact">
              {b.package.map((p, i) => (
                <li key={i} className={p.you ? "you" : ""}>
                  <b>{p.callsign}</b> {p.size}× {p.aircraft} · {p.task} · T/O {p.takeoff.label.slice(3)} {p.you && "(you)"}
                </li>
              ))}
            </ul>
            <h3>Comms</h3>
            <p className="mono">
              Package {b.comms.package}
              <br />
              Flight {b.comms.flight}
              {b.radio_callsign && b.radio_callsign !== b.callsign && (
                <>
                  <br />
                  Radio callsign {b.radio_callsign}
                </>
              )}
            </p>
          </div>
        </section>
      </article>
    </div>
    ,
    document.body,
  );
}
