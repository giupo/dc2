import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Package } from "./api/client";
import { BriefingCard, useBriefing } from "./components/BriefingCard";
import { Details } from "./components/Details";
import { Header } from "./components/Header";
import { MapView } from "./components/MapView";
import { PickHint, PlannerContext, RouteBar, usePlannerState } from "./components/Planner";
import { AirDefensePanel, AtoPanel, EventsPanel, ForcesPanel, FrontPanel, IntelPanel, LogisticsPanel, SquadronsPanel, type PanelProps } from "./components/Panels";
import { factionColor, type Units } from "./lib/format";
import { DEFAULT_LAYERS, type Layers, type Selection } from "./lib/selection";
import { useCampaign } from "./lib/useCampaign";

const TABS = [
  { id: "front", label: "Front", Panel: FrontPanel },
  { id: "forces", label: "Forces", Panel: ForcesPanel },
  { id: "logistics", label: "Logistics", Panel: LogisticsPanel },
  { id: "ato", label: "ATO", Panel: AtoPanel },
  { id: "squadrons", label: "Squadrons", Panel: SquadronsPanel },
  { id: "ad", label: "AD", Panel: AirDefensePanel },
  { id: "intel", label: "Intel", Panel: IntelPanel },
  { id: "events", label: "Events", Panel: EventsPanel },
] as const;

const LAYER_LABELS: Record<keyof Layers, string> = {
  territory: "Territory",
  links: "Roads",
  fronts: "Front & battles",
  fires: "Artillery fire",
  divisions: "Brigades",
  logistics: "Convoys",
  air: "Air",
  airDefense: "Air defence",
  structures: "Structures",
  labels: "Names",
};

/** localStorage key of the open/closed map layer menu. */
const LAYERS_OPEN_KEY = "dynacamp.layersOpen";

/** A remembered per-browser setting (null if none or storage is unavailable). */
function remembered(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Remember a per-browser setting (silently skipped if storage is unavailable). */
function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private window or blocked storage: not remembered */
  }
}

export default function App() {
  // The coalition whose units are friendly ("" until the campaign is known: then its first faction's).
  const [viewpoint, setViewpoint] = useState("");
  // Fog of war: see the campaign as the viewpoint faction's sensors do (off = umpire view).
  const [fog, setFog] = useState(true);
  const [factions, setFactions] = useState<{ id: string; coalition: string }[]>([]);
  const fogFaction = fog ? (factions.find((f) => f.coalition === viewpoint)?.id ?? null) : null;
  const data = useCampaign(fogFaction);
  // Display units: the user's choice (remembered), else the campaign's default.
  const [unitsChoice, setUnitsChoice] = useState<Units | null>(() => (localStorage.getItem("dynacamp.units") as Units | null) ?? null);
  const units: Units = unitsChoice ?? data.campaign?.units ?? "imperial";
  const chooseUnits = (u: Units) => {
    localStorage.setItem("dynacamp.units", u);
    setUnitsChoice(u);
  };
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("front");
  const [layers, setLayers] = useState<Layers>(DEFAULT_LAYERS);
  const [selected, setSelected] = useState<Selection>(null);
  const [focus, setFocus] = useState<{ lngLat: [number, number]; seq: number }>();
  const known = data.campaign?.factions;
  useEffect(() => {
    if (known && known.length !== factions.length) {
      setFactions(known.map((f) => ({ id: f.id, coalition: f.coalition })));
      setViewpoint((v) => v || (known[0]?.coalition ?? ""));
    }
  }, [known, factions.length]);
  // One "View as" button per coalition, named after its first faction.
  const coalitions = (known ?? []).filter((f, i, all) => all.findIndex((o) => o.coalition === f.coalition) === i);
  const [briefingOpen, setBriefingOpen] = useState(false);
  const selectedFlight = selected?.kind === "flight" ? selected.id : null;
  const briefing = useBriefing(selectedFlight, units);

  // Selecting from a list also moves the map; selecting on the map does not.
  const select = useCallback((s: Selection, at?: number[] | null) => {
    setSelected(s);
    if (at) setFocus((f) => ({ lngLat: [at[1], at[0]], seq: (f?.seq ?? 0) + 1 }));
  }, []);

  // ATO orders are given as the viewpoint coalition's faction.
  const planner = usePlannerState(factions.find((f) => f.coalition === viewpoint)?.id ?? null);

  // Packages over (gone from the ATO) that an event was clicked about, in the current view.
  const [archive, setArchive] = useState<Package[]>([]);
  useEffect(() => setArchive([]), [fogFaction]);
  const openArchived = useCallback((s: { kind: "flight" | "package"; id: string }) => {
    const load = s.kind === "package" ? api.package(s.id, fogFaction) : api.flightPackage(s.id, fogFaction);
    load.then((p) => {
      setArchive((a) => [p, ...a.filter((x) => x.id !== p.id)].slice(0, 50));
      const f = s.kind === "flight" ? p.flights.find((x) => x.id === s.id) : undefined;
      select(s, f?.ll ?? p.ll);
    }).catch(() => undefined); // an enemy's, or dropped from the archive: nothing to show
  }, [fogFaction, select]);

  const panelProps: PanelProps = { data, selected, select, viewpoint, units, archive, openArchived };
  // On a phone (narrow screen, see styles.css) the panels are a sheet under the
  // map, opened by tapping a tab and closed by tapping it again. The map layers
  // fold behind a button everywhere (closed by default, remembered).
  const [sheetOpen, setSheetOpen] = useState(false);
  const [layersOpen, setLayersOpen] = useState(() => remembered(LAYERS_OPEN_KEY) === "1");
  const toggleLayers = () =>
    setLayersOpen((o) => {
      remember(LAYERS_OPEN_KEY, o ? "0" : "1");
      return !o;
    });
  const pickTab = (id: (typeof TABS)[number]["id"]) => {
    if (id === tab) setSheetOpen((o) => !o);
    else {
      setTab(id);
      setSheetOpen(true);
    }
  };
  const Active = TABS.find((t) => t.id === tab)!.Panel;
  // The tab strip scrolls sideways: the mouse wheel scrolls it, and the active tab is kept in view.
  const tabStrip = useRef<HTMLElement>(null);
  useEffect(() => {
    tabStrip.current?.querySelector<HTMLElement>("button.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [tab]);
  const wheelTabs = (e: React.WheelEvent) => {
    const el = tabStrip.current;
    if (!el || el.scrollWidth <= el.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    el.scrollLeft += e.deltaY;
  };

  return (
    <PlannerContext.Provider value={planner}>
    <div className="app">
      <Header data={data}>
        <div className="view-controls">
          <div className="viewpoint" title="Whose campaign you look at: friendly NATO symbols (rectangle), the ATO, the squadrons, the bullseye, the orders you give">
            <span>View as</span>
            {coalitions.map((f) => (
              <button key={f.coalition} className={viewpoint === f.coalition ? "on" : ""} onClick={() => setViewpoint(f.coalition)} style={{ color: factionColor(data.campaign, f.id) }} title={f.coalition}>
                {f.name}
              </button>
            ))}
          </div>
        </div>
      </Header>
      <main className="main">
        <section className="map-wrap">
          <MapView data={data} layers={layers} selected={selected} onSelect={(s) => setSelected(s)} focus={focus} viewpoint={viewpoint} briefing={briefing} units={units} />
          <div className={`layers ${layersOpen ? "open" : ""}`}>
            <button className="layers-toggle" onClick={toggleLayers} aria-expanded={layersOpen} title="Show or hide map layers">
              {layersOpen ? "✕ Layers" : "☰ Layers"}
            </button>
            <div className="layers-body">
            {(Object.keys(LAYER_LABELS) as (keyof Layers)[]).map((k) => (
              <label key={k}>
                <input type="checkbox" checked={layers[k]} onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })} />
                {LAYER_LABELS[k]}
              </label>
            ))}
            <label className="fog" title="Fog of war: show only what the viewpoint coalition's sensors (aircraft, AWACS, radars, recon) have found. Off: umpire view, everything.">
              <input type="checkbox" checked={fog} onChange={(e) => setFog(e.target.checked)} />
              Fog of war
            </label>
            <div className="viewpoint" title="Units of distances, altitudes and speeds (also in the briefings)">
              <span>Units</span>
              <button className={units === "metric" ? "on" : ""} onClick={() => chooseUnits("metric")}>SI</button>
              <button className={units === "imperial" ? "on" : ""} onClick={() => chooseUnits("imperial")}>kts/ft/NM</button>
            </div>
            </div>
          </div>
          <RouteBar units={units} />
          <PickHint />
          <Details {...panelProps} close={() => setSelected(null)} openBriefing={() => setBriefingOpen(true)} />
          {briefingOpen && briefing && <BriefingCard b={briefing} close={() => setBriefingOpen(false)} />}
          {!data.connected && !data.campaign && (
            <div className="overlay">
              Connecting to the campaign engine…
              <small>Start it with: uv run dynacamp serve scenarios/&lt;scenario&gt;.toml</small>
            </div>
          )}
        </section>
        <aside className={`sidebar ${sheetOpen ? "open" : ""}`}>
          <nav className="tabs" ref={tabStrip} onWheel={wheelTabs}>
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "on" : ""} onClick={() => pickTab(t.id)}>
                {t.label}
                {t.id === "front" && !!data.fronts?.battles.length && <span className="count">{data.fronts.battles.length}</span>}
                {t.id === "ato" && !!data.ato?.packages.length && <span className="count">{data.ato.packages.length}</span>}
                {t.id === "intel" && !!data.intel?.contacts.length && <span className="count">{data.intel.contacts.length}</span>}
              </button>
            ))}
          </nav>
          <div className="sidebar-body">
            <Active {...panelProps} />
          </div>
        </aside>
      </main>
    </div>
    </PlannerContext.Provider>
  );
}
