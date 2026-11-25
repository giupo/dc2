import { useEffect, useRef, useState } from "react";
import { AuthError, api, token, type Auth, type SaveStatus } from "../api/client";
import { factionColor } from "../lib/format";

// Campaign seconds per real second; labels as in flight sims' time acceleration.
const SPEEDS = [1, 10, 60, 300, 900, 3600];

/** Padlock icon, open or closed. */
function Padlock({ open }: { open: boolean }) {
  return (
    <svg width="10" height="11" viewBox="0 0 10 11" aria-hidden="true">
      <path d={open ? "M3 5 V3 a2 2 0 0 1 4 -0.5" : "M3 5 V3 a2 2 0 0 1 4 0 V5"} fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="1.5" y="5" width="7" height="5.5" rx="1" fill="currentColor" />
    </svg>
  );
}

/** Asks for the API token (commands need it) or forgets the stored one. */
function TokenGate({ auth, onAuth, asking, setAsking }: { auth: Auth; onAuth: (a: Auth) => void; asking: boolean; setAsking: (v: boolean) => void }) {
  const [value, setValue] = useState("");
  const [wrong, setWrong] = useState(false);
  if (!auth.required) return null;
  if (auth.authenticated)
    return (
      <button className="lock open" title="Commands unlocked. Click to forget the API token" onClick={() => { token.clear(); onAuth({ ...auth, authenticated: false }); }}>
        <Padlock open />
      </button>
    );
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    token.set(value);
    api.auth().then((a) => {
      setWrong(!a.authenticated);
      if (!a.authenticated) token.clear();
      else { setValue(""); setAsking(false); }
      onAuth(a);
    }).catch(() => setWrong(true));
  };
  return (
    <>
      <button className={`lock ${asking ? "on" : ""}`} title="Commands are locked: enter the API token printed by `dynacamp serve`" onClick={() => setAsking(!asking)}>
        <Padlock open={false} />
      </button>
      {asking && (
        <form className="token-form" onSubmit={submit}>
          <input type="password" autoComplete="off" autoFocus placeholder="API token" value={value} className={wrong ? "wrong" : ""} onChange={(e) => { setValue(e.target.value); setWrong(false); }} aria-label="API token" />
          <button type="submit">OK</button>
        </form>
      )}
    </>
  );
}

const TRIGGER: Record<string, string> = { auto: "autosave", api: "saved from the UI/API", exit: "saved on shutdown" };

/** Tooltip of the save button: what clicking does and the latest save. */
function saveTitle(save: SaveStatus | null | undefined, locked: boolean): string {
  if (!save?.enabled) return "Saving is disabled: start the server with --save FILE";
  const lines = [locked ? "Locked: enter the API token to save" : `Save the campaign now to ${save.file}`];
  if (save.last) {
    const at = new Date(save.last.saved_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    lines.push(`Last save: ${save.last.time.label} at ${at} (${TRIGGER[save.last.trigger] ?? save.last.trigger})`);
  } else lines.push("Not saved yet in this server run");
  lines.push(save.autosave_minutes ? `Autosave every ${save.autosave_minutes} min` : "No autosave: only on request and on shutdown");
  return lines.join("\n");
}

/** Save now (POST /api/save); briefly shows a tick, or "!" with the error. */
function SaveButton({ save, locked, onAuthError }: { save: SaveStatus | null | undefined; locked: boolean; onAuthError: () => void }) {
  const [state, setState] = useState<"idle" | "saving" | "done" | "error">("idle");
  const [error, setError] = useState<string>();
  const [latest, setLatest] = useState<SaveStatus>();
  // The campaign view refreshes every few seconds; a fresher answer to our own POST wins meanwhile.
  const status = latest && (latest.last?.saved_at ?? 0) > (save?.last?.saved_at ?? 0) ? latest : save;
  const click = () => {
    setState("saving");
    api.save().then((s) => {
      setLatest(s);
      setError(undefined);
      setState("done");
      setTimeout(() => setState((x) => (x === "done" ? "idle" : x)), 2000);
    }).catch((e: Error) => {
      if (e instanceof AuthError) onAuthError();
      setError(e.message);
      setState("error");
    });
  };
  const title = state === "error" && error ? `Save failed: ${error}` : saveTitle(status, locked);
  return (
    <button className={`save ${state}`} disabled={!save?.enabled || locked || state === "saving"} onClick={click} title={title} aria-label="Save campaign">
      {state === "done" ? (
        <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden="true"><path d="M1.5 6 L4.5 9 L9.5 2" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg>
      ) : (
        <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden="true">
          <path d="M1 1 H8.5 L10 2.5 V10 H1 Z" fill="none" stroke="currentColor" strokeWidth="1.2" />
          <rect x="3" y="1.5" width="4.5" height="3" fill="currentColor" />
          <rect x="2.8" y="6.5" width="5.4" height="2.8" fill="currentColor" opacity="0.6" />
        </svg>
      )}
      {state === "error" && <span className="err">!</span>}
    </button>
  );
}

/** Pause / play and fast-forward, save, API token. Time controls are disabled
 * while a game is live (time runs 1:1); commands until the API token is known. */
function TimeControls({ data }: { data: CampaignData }) {
  const c = data.control;
  const [error, setError] = useState<string>();
  const [auth, setAuth] = useState<Auth>();
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    api.auth().then(setAuth).catch(() => undefined);
  }, [data.connected]);
  // Space toggles pause, like the play button (latest state through a ref: the
  // listener is installed once).
  const toggle = useRef<() => void>(undefined);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))) return;
      e.preventDefault(); // no page scroll, no click on the focused button
      toggle.current?.();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
  if (!c) {
    toggle.current = undefined;
    return null;
  }
  const locked = !!auth && auth.required && !auth.authenticated;
  const authFailed = () => {
    token.clear();
    setAuth({ required: true, authenticated: false });
    setAsking(true);
  };
  const change = (x: { paused?: boolean; compression?: number }) =>
    api.control(x).then(() => setError(undefined)).catch((e: Error) => {
      if (e instanceof AuthError) authFailed();
      setError(e.message);
    });
  const live = data.live;
  const title = live ? "A game is live: time runs 1:1" : locked ? "Locked: enter the API token" : undefined;
  const disabled = live || locked;
  toggle.current = disabled ? undefined : () => change({ paused: !c.paused });
  return (
    <div className="time-controls" title={error ?? title}>
      <button className={`play ${c.paused ? "" : "on"}`} disabled={disabled} onClick={() => change({ paused: !c.paused })} title={c.paused ? "Resume (Space)" : "Pause (Space)"}>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          {c.paused ? <path d="M2 1 L9 5 L2 9 Z" fill="currentColor" /> : <path d="M2 1 H4 V9 H2 Z M6 1 H8 V9 H6 Z" fill="currentColor" />}
        </svg>
      </button>
      {SPEEDS.map((s) => (
        <button key={s}
	  className={!c.paused && c.compression === s ? "on" : ""}
	  disabled={disabled}
	  onClick={() => change({ paused: false, compression: s })}
	  title={`${s} s of campaign per second`}>
          {s}×
        </button>
      ))}
      <SaveButton save={data.campaign?.save} locked={locked} onAuthError={authFailed} />
      {auth && <TokenGate auth={auth} onAuth={(a) => { setAuth(a); setError(undefined); }} asking={asking} setAsking={setAsking} />}
      {error && !locked && <span className="err">!</span>}
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
