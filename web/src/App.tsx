import { useCallback, useState } from "react";
import { Details } from "./components/Details";
import { Header } from "./components/Header";
import { MapView } from "./components/MapView";
import { AirDefensePanel, AtoPanel, EventsPanel, ForcesPanel, FrontPanel, LogisticsPanel, type PanelProps } from "./components/Panels";
import { factionColor } from "./lib/format";
import { DEFAULT_LAYERS, type Layers, type Selection } from "./lib/selection";
import { useCampaign } from "./lib/useCampaign";

const TABS = [
  { id: "front", label: "Front", Panel: FrontPanel },
  { id: "forces", label: "Forces", Panel: ForcesPanel },
  { id: "logistics", label: "Logistics", Panel: LogisticsPanel },
  { id: "ato", label: "ATO", Panel: AtoPanel },
  { id: "ad", label: "AD", Panel: AirDefensePanel },
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
  const data = useCampaign();
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("front");
  const [layers, setLayers] = useState<Layers>(DEFAULT_LAYERS);
  const [selected, setSelected] = useState<Selection>(null);
  const [focus, setFocus] = useState<{ lngLat: [number, number]; seq: number }>();
  const [viewpoint, setViewpoint] = useState("blue");

  // Selecting from a list also moves the map; selecting on the map does not.
  const select = useCallback((s: Selection, at?: number[] | null) => {
    setSelected(s);
    if (at) setFocus((f) => ({ lngLat: [at[1], at[0]], seq: (f?.seq ?? 0) + 1 }));
  }, []);

  const panelProps: PanelProps = { data, selected, select, viewpoint };
  const Active = TABS.find((t) => t.id === tab)!.Panel;

  return (
    <div className="app">
      <Header data={data} />
      <main className="main">
        <section className="map-wrap">
          <MapView data={data} layers={layers} selected={selected} onSelect={(s) => setSelected(s)} focus={focus} viewpoint={viewpoint} />
          <div className="layers">
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
          </div>
          <Details {...panelProps} close={() => setSelected(null)} />
          {!data.connected && !data.campaign && (
            <div className="overlay">
              Connecting to the campaign engine…
              <small>Start it with: uv run dynacamp serve scenarios/&lt;scenario&gt;.toml</small>
            </div>
          )}
        </section>
        <aside className="sidebar">
          <nav className="tabs">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "on" : ""} onClick={() => setTab(t.id)}>
                {t.label}
                {t.id === "front" && !!data.fronts?.battles.length && <span className="count">{data.fronts.battles.length}</span>}
                {t.id === "ato" && !!data.ato?.packages.length && <span className="count">{data.ato.packages.length}</span>}
              </button>
            ))}
          </nav>
          <div className="sidebar-body">
            <Active {...panelProps} />
          </div>
        </aside>
      </main>
    </div>
  );
}
