export type SelectionKind = "division" | "flight" | "package" | "convoy" | "province" | "battle" | "airbase" | "depot" | "sam";

export type Selection = { kind: SelectionKind; id: string } | null;

export interface Layers {
  territory: boolean;
  links: boolean;
  fronts: boolean;
  fires: boolean;
  divisions: boolean;
  logistics: boolean;
  air: boolean;
  airDefense: boolean;
  structures: boolean;
  labels: boolean;
}

export const DEFAULT_LAYERS: Layers = {
  territory: true,
  links: true,
  fronts: true,
  fires: true,
  divisions: true,
  logistics: true,
  air: true,
  airDefense: true,
  structures: true,
  labels: true,
};
