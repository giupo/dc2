import { useCallback, useEffect, useState } from "react";
import { api, type Package } from "./api/client";
import { BriefingCard, useBriefing } from "./components/BriefingCard";
import { Details } from "./components/Details";
import { Header } from "./components/Header";
import { MapView } from "./components/MapView";
import { PickHint, PlannerContext, RouteBar, usePlannerState } from "./components/Planner";
import { AirDefensePanel, AtoPanel, EventsPanel, ForcesPanel, FrontPanel, IntelPanel, LogisticsPanel, type PanelProps } from "./components/Panels";
import { factionColor, type Units } from "./lib/format";
import { DEFAULT_LAYERS, type Layers, type Selection } from "./lib/selection";
import { useCampaign } from "./lib/useCampaign";

const TABS = [
  { id: "front", label: "Front", Panel: FrontPanel },
  { id: "forces", label: "Forces", Panel: ForcesPanel },
  { id: "logistics", label: "Logistics", Panel: LogisticsPanel },
  { id: "ato", label: "ATO", Panel: AtoPanel },
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
  labels: "Names",
};

export default function App() {
  const [viewpoint, setViewpoint] = useState("blue");
  // Fog of war: see the campaign as the viewpoint faction's sensors do (off = umpire view).
  const [fog, setFog] = useState(true);
  const [factions, setFactions] = useState<{ id: string; side: string }[]>([]);
  const fogFaction = fog ? (factions.find((f) => f.side === viewpoint)?.id ?? null) : null;
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
    if (known && known.length !== factions.length) setFactions(known.map((f) => ({ id: f.id, side: f.side })));
  }, [known, factions.length]);
  const [briefingOpen, setBriefingOpen] = useState(false);
  const selectedFlight = selected?.kind === "flight" ? selected.id : null;
  const briefing = useBriefing(selectedFlight, units);

  // Selecting from a list also moves the map; selecting on the map does not.
  const select = useCallback((s: Selection, at?: number[] | null) => {
    setSelected(s);
    if (at) setFocus((f) => ({ lngLat: [at[1], at[0]], seq: (f?.seq ?? 0) + 1 }));
  }, []);

  // ATO orders are given as the viewpoint side's faction.
  const planner = usePlannerState(factions.find((f) => f.side === viewpoint)?.id ?? null);

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
  // map, opened by tapping a tab and closed by tapping it again; the layer list
  // folds behind a button. On a desktop both are always open.
  const [sheetOpen, setSheetOpen] = useState(false);
  const [layersOpen, setLayersOpen] = useState(false);
  const pickTab = (id: (typeof TABS)[number]["id"]) => {
    if (id === tab) setSheetOpen((o) => !o);
    else {
      setTab(id);
      setSheetOpen(true);
    }
  };
  const Active = TABS.find((t) => t.id === tab)!.Panel;

  return (
    <PlannerContext.Provider value={planner}>
    <div className="app">
      <Header data={data} />
      <main className="main">
        <section className="map-wrap">
          <MapView data={data} layers={layers} selected={selected} onSelect={(s) => setSelected(s)} focus={focus} viewpoint={viewpoint} briefing={briefing} units={units} />
          <div className={`layers ${layersOpen ? "open" : ""}`}>
            <button className="layers-toggle" onClick={() => setLayersOpen((o) => !o)} aria-expanded={layersOpen}>
              {layersOpen ? "✕ Layers" : "☰ Layers"}
            </button>
            <div className="layers-body">
            {(Object.keys(LAYER_LABELS) as (keyof Layers)[]).map((k) => (
              <label key={k}>
                <input type="checkbox" checked={layers[k]} onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })} />
                {LAYER_LABELS[k]}
              </label>
            ))}
            <div className="viewpoint" title="NATO symbols: the viewpoint side is friendly (rectangle), the enemy hostile (diamond)">
              <span>View as</span>
              {(data.campaign?.factions ?? []).map((f) => (
                <button key={f.side} className={viewpoint === f.side ? "on" : ""} onClick={() => setViewpoint(f.side)} style={{ color: factionColor(data.campaign, f.id) }}>
                  {f.side}
                </button>
              ))}
            </div>
            <label className="fog" title="Fog of war: show only what the viewpoint side's sensors (aircraft, AWACS, radars, recon) have found. Off: umpire view, everything.">
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
          <nav className="tabs">
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
