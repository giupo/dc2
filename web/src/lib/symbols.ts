// NATO APP-6 / MIL-STD-2525C symbology via milsymbol.
//
// SIDC (letter based, 2525C): S + affiliation (F friend, H hostile) + battle
// dimension (G ground, A air) + status (P present, A anticipated = dashed frame)
// + function id (6) + modifier + echelon. Affiliation depends on the viewer:
// the "viewpoint" coalition is friendly. In a fog-of-war view, stale contacts are
// drawn with the dashed frame and unidentified radar tracks as "unknown" (U).
import ms from "milsymbol";

export type Affiliation = "F" | "H" | "N" | "U";
/** P = present; A = anticipated/suspected (dashed frame): a stale contact. */
export type Status = "P" | "A";

export function affiliation(coalition: string, viewpoint: string): Affiliation {
  if (!coalition) return "U";
  return coalition === viewpoint ? "F" : "H";
}

// campaign template -> (function id, echelon)
const UNIT_FN: Record<string, [string, string]> = {
  tank_brigade: ["UCA---", "H"], // armour, brigade
  mech_brigade: ["UCIZ--", "H"], // mechanised infantry, brigade
  infantry_brigade: ["UCIM--", "H"], // motorised infantry, brigade
  artillery_group: ["UCF---", "G"], // field artillery, regiment/group
};

// campaign task -> air function id
const AIR_FN: Record<string, string> = {
  CAP: "MFF---", // fighter
  ESCORT: "MFF---",
  SEAD: "MFA---", // attack/strike
  CAS: "MFA---",
  BAI: "MFA---",
  STRIKE: "MFB---", // bomber
  AWACS: "MFRW--", // airborne early warning
  TANKER: "MFK---", // tanker
  TRANSPORT: "MFC---", // cargo airlift
};

export function unitSidc(template: string, aff: Affiliation, status: Status = "P"): string {
  const [fn, echelon] = UNIT_FN[template] ?? ["UCI---", "H"];
  return `S${aff}G${status}${fn}-${echelon}`;
}

/** Air symbol; ``task`` null = an unidentified track (generic aircraft). */
export function airSidc(task: string | null, helo: boolean, aff: Affiliation, status: Status = "P"): string {
  const fn = helo ? (task === "TRANSPORT" ? "MHC---" : task ? "MHA---" : "MH----") : task ? (AIR_FN[task] ?? "MF----") : "M-----";
  return `S${aff}A${status}${fn}--`;
}

export const convoySidc = (aff: Affiliation, status: Status = "P") => `S${aff}G${status}USST----`; // transportation

// air defence role -> function id (2525C): missile AD long / medium, short range AD, radar
const SAM_FN: Record<string, string> = { long: "UCDML-", medium: "UCDMM-", short: "UCDS--", ewr: "ESR---" };
export function samSidc(role: string, aff: Affiliation, status: Status = "P"): string {
  return `S${aff}G${status}${SAM_FN[role] ?? "UCD---"}--`;
}

/** Symbol status of an entity as its viewer knows it. */
export function knownStatus(intel: { stale: boolean } | null | undefined): Status {
  return intel?.stale ? "A" : "P";
}
export const airbaseSidc = (aff: Affiliation) => `S${aff}GPIBA---H-`; // installation: airport / air base

export interface Rendered {
  svg: string;
  width: number;
  height: number;
  anchor: [number, number]; // the symbol's reference point inside the SVG
}

const cache = new Map<string, Rendered>();

// Readable on a dark map: light text with a dark halo, dark outline around frames.
const DARK_MAP_STYLE = {
  infoColor: "#e5e7eb",
  infoOutlineColor: "#05070b",
  infoOutlineWidth: 3,
  infoSize: 48,
  outlineColor: "#05070b",
  outlineWidth: 2,
};

/** Render (and cache) a symbol. ``options`` are milsymbol options (size, uniqueDesignation, direction...). */
export function symbol(sidc: string, options: Record<string, unknown> = {}): Rendered {
  const key = sidc + JSON.stringify(options);
  let r = cache.get(key);
  if (!r) {
    const s = new ms.Symbol(sidc, { size: 22, ...DARK_MAP_STYLE, ...options });
    const size = s.getSize();
    const anchor = s.getAnchor();
    r = { svg: s.asSVG(), width: size.width, height: size.height, anchor: [anchor.x, anchor.y] };
    if (cache.size > 2000) cache.clear();
    cache.set(key, r);
  }
  return r;
}

/** First token of a unit name, e.g. "1st Guards Tank Bde" -> "1st". Used as unique designation. */
export function designation(name: string): string {
  return name.split(/\s+/)[0] ?? name;
}
