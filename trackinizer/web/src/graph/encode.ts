// How the graph draws each inquiry and edge: v1's encoding (`server/assets/graph.html`)
// on the app's theme tokens, worked out once when data or the theme changes, so the
// canvas's per-frame accessors only read fields.

/** The colours the graph draws with, from the theme's tokens (`graph.css`). */
export type Palette = {
  /** Each inquiry kind's fill. */
  readonly kinds: { readonly [kind: string]: string };
  /** Each edge kind's stroke, for edges with no valence. */
  readonly edges: { readonly [edgeKind: string]: string };
  /** Each status's ring. */
  readonly status: { readonly [status: string]: string };
  /** For a kind, edge kind or status with no colour of its own. */
  readonly fallback: string;
  /** The view's background: an undecidable Belief's half-tone washes it over the fill. */
  readonly background: string;
  /** A citation with a positive valence; the graph's `--g-vpos`. */
  readonly support: string;
  /** A citation with a negative valence; the graph's `--g-vneg`. */
  readonly against: string;
  /** Every edge outside the focus's: hovered or selected, it dims the rest. */
  readonly faint: string;
  /** The ring around the focus and the selection: the text colour. */
  readonly halo: string;
  /** The ring around a search match: the app's link accent. */
  readonly match: string;
  /** Every kind but Belief, drawn as one tint while the view is zoomed out. */
  readonly duoRecord: string;
  /** A Belief while the view is zoomed out: the one hue that carries from far off. */
  readonly duoBelief: string;
  /** The ring around what an agent pointed at: the app's amber. */
  readonly highlight: string;
};

/** How one node is drawn: a disc of `fill`, maybe washed, ringed, maybe dotted. */
export type NodeLook = {
  readonly radius: number;
  readonly fill: string;
  /** 0 draws no fill: a disproven Belief is hollow. */
  readonly fillAlpha: number;
  /** A second fill over the first: an undecidable Belief's half-tone. */
  readonly wash: string | null;
  readonly washAlpha: number;
  readonly ring: string;
  readonly ringAlpha: number;
  /** A proven Belief's inner dot. */
  readonly dot: string | null;
  /** A ring just outside the disc: the focus, the selection or a search match. */
  readonly halo: string | null;
  /** Text under the disc, outlined in the background so edges never cross it: a root's title while grouped. */
  readonly label: { readonly text: string; readonly color: string; readonly outline: string } | null;
  /**
   * The fill to draw at a distance, which `fillAt` eases into `fill` as the view
   * is zoomed in. It equals `fill` where the kind's own hue should show at every
   * zoom: inside a focus, where the colour is the answer to what was asked.
   */
  readonly duo: string;
};

/** Where a node stands in what the view lights and finds. */
export type Standing = {
  /** Outside the focus, or a hovered or selected node's neighbourhood. */
  readonly dimmed?: boolean;
  /** Two or more hops from the focus. */
  readonly far?: boolean;
  /** `strong` for the focus and the selection, `highlight` for what an agent pointed at, `match` for a search match. */
  readonly halo?: "strong" | "highlight" | "match" | null;
  /** Text to draw under it. */
  readonly label?: string | null;
  /** Inside a lit focus: draw the kind's own hue at every zoom, not the distant tint. */
  readonly near?: boolean;
  /** How much of itself it draws, 0 to 1: how far it has come up in a replay. */
  readonly appear?: number;
};

/** How one edge is drawn; its arrow takes its colour. */
export type LinkLook = { readonly color: string; readonly width: number; readonly dash: number[] | null };

/** What a node's look depends on. */
export type NodeShape = {
  readonly kind: string;
  readonly status: string;
  readonly judgement?: string;
  readonly confidence?: number;
  /** Distinct inquiries it shares an edge with. */
  readonly degree: number;
  /** Weighted PageRank, normalised to the graph's busiest node: what lifts a hub's disc. */
  readonly authority: number;
  /** An Issue that narrows nothing: the top of a tree. */
  readonly root: boolean;
};

/**
 * How the node `shape` is drawn.
 *
 * Size is prominence: the radius grows with the square root of the degree, 4 to
 * 14, and a root Issue is the largest. Retired work (abandoned, invalid)
 * recedes. A Belief's confidence washes its fill out, and its judgement marks
 * the disc: hollow when disproven, half-toned when undecidable, dotted when
 * proven.
 *
 * Where it stands changes it too. A node `dimmed` outside a focus recedes
 * further than retired work, so that the two never look alike, and one `far`
 * from the focus fades a little. A `halo` rings the focus or the selection in
 * the text colour, what an agent pointed at in amber, and a search match in the
 * accent. A `label` shows under it,
 * at most 40 characters.
 */
export function nodeLook(
  shape: NodeShape,
  palette: Palette,
  { dimmed = false, far = false, halo = null, label = null, near = false, appear = 1 }: Standing = {},
): NodeLook {
  const fill = palette.kinds[shape.kind] ?? palette.fallback;
  const retired = shape.status === "abandoned" || shape.status === "invalid";
  const belief = shape.kind === "Belief";
  const judgement = belief ? shape.judgement : undefined;
  const fade = (far ? FAR_FADE : 1) * appear;
  const alpha = (dimmed ? DIMMED_ALPHA : retired ? RETIRED_ALPHA : 1) * fade;
  const fillAlpha = dimmed || retired ? alpha : (0.35 + 0.65 * (belief ? (shape.confidence ?? 1) : 1)) * fade;
  // The design's sizes: a base by kind (a Belief the biggest of the small, so a
  // claim reads at every zoom) and authority — the weighted PageRank — lifting
  // the few nodes the evidence flows through into a bright core, while the mass
  // stays a tight ball of small dots.
  const radius = (BASE_OF[shape.kind] ?? 2.5) + 6.5 * shape.authority;
  return {
    radius,
    fill,
    fillAlpha: judgement === "disproven" ? 0 : fillAlpha,
    wash: judgement === "undecidable" ? palette.background : null,
    washAlpha: fillAlpha * 0.4,
    ring: judgement === "disproven" ? fill : (palette.status[shape.status] ?? palette.fallback),
    ringAlpha: alpha,
    dot: judgement === "proven" && !retired && !dimmed ? (palette.status.complete ?? palette.fallback) : null,
    halo: halo === "strong" ? palette.halo : halo === "highlight" ? palette.highlight : halo === "match" ? palette.match : null,
    label: label === null ? null : { text: label.length > LABEL_CHARS ? `${label.slice(0, LABEL_CHARS - 1)}…` : label, color: palette.halo, outline: palette.background },
    duo: near ? fill : belief ? palette.duoBelief : palette.duoRecord,
  };
}

/**
 * The fill to draw `look` with at `zoom`: its distant tint eased into its kind's
 * own hue between `DUO_ZOOM` and `KIND_ZOOM`.
 *
 * Far off, a thousand nodes in nine hues read as confetti, and the shape of the
 * work is lost; one tint for records and one for Beliefs leaves each island a
 * single form with its claims picked out. Close up there are few enough nodes on
 * screen to tell nine hues apart, and the kind is what the reader wants.
 *
 * The blend is quantised to `BLEND_STEPS`, so a pan at one zoom asks for the
 * same few colours rather than a new one each frame, and every mix is kept
 * (`mixed`): over a drag, the work is a map lookup per node.
 */
export function fillAt(look: NodeLook, zoom: number): string {
  if (look.duo === look.fill) return look.fill;
  const eased = (zoom - DUO_ZOOM) / (KIND_ZOOM - DUO_ZOOM);
  const step = Math.round(Math.min(1, Math.max(0, eased)) * BLEND_STEPS) / BLEND_STEPS;
  if (step === 0) return look.duo;
  if (step === 1) return look.fill;
  return mix(look.duo, look.fill, step);
}

/** `from` and `to` mixed, `amount` of the way from one to the other; either unreadable gives `to`. */
export function mix(from: string, to: string, amount: number): string {
  const key = `${from}|${to}|${amount}`;
  const kept = mixed.get(key);
  if (kept !== undefined) return kept;
  const [a, b] = [channels(from), channels(to)];
  const blended =
    a === null || b === null
      ? to
      : `#${[0, 1, 2].map((index) => Math.round(a[index]! + (b[index]! - a[index]!) * amount).toString(16).padStart(2, "0")).join("")}`;
  // Bounded by the palette's colours times the blend's steps, so it cannot grow with the graph.
  mixed.set(key, blended);
  return blended;
}

/**
 * A panel's colour for a graph colour: the design's "one step lighter than the
 * graph". A `Kind#seq` in prose is small text, and the canvas's hue at that size
 * reads as muddy; lifted toward white it is the same hue, and legible.
 */
export const panelTint = (colour: string) => mix(colour, "#ffffff", 0.28);

/** A panel's colour for a valence, which the design lifts a little less. */
export const panelValence = (colour: string) => mix(colour, "#ffffff", 0.2);

/** A panel's colour for one inquiry kind. */
export const panelKind = (palette: Palette, kind: string) => panelTint(palette.kinds[kind] ?? palette.fallback);

/** `colour`'s red, green and blue, or null when it is neither `#rgb`, `#rrggbb` nor `rgb(…)`. */
function channels(colour: string): [number, number, number] | null {
  const text = colour.trim();
  if (text.startsWith("#")) {
    const digits = text.slice(1);
    const wide = digits.length === 6 ? digits : digits.length === 3 ? [...digits].map((digit) => digit + digit).join("") : null;
    if (wide === null) return null;
    const value = Number.parseInt(wide, 16);
    return Number.isNaN(value) ? null : [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  }
  const parts = /^rgba?\(([^)]+)\)$/.exec(text);
  if (!parts) return null;
  const numbers = parts[1]!.split(/[,/\s]+/).filter(Boolean).map(Number);
  return numbers.length >= 3 && numbers.slice(0, 3).every((number) => Number.isFinite(number))
    ? [numbers[0]!, numbers[1]!, numbers[2]!]
    : null;
}

/**
 * How an edge of `kind` is drawn, given the degree of its busier end.
 *
 * A citation with a valence draws in the support or against colour, as wide as
 * its weight; any other edge in its kind's quiet hue, heavier the busier its
 * ends, so a hub's edges read as the backbone. `favors`, the citation that does
 * not vote, is dashed. An edge `dimmed` outside a focus draws faint.
 */
export function linkLook(
  link: { readonly kind: string; readonly valence?: number },
  endDegree: number,
  palette: Palette,
  dimmed = false,
): LinkLook {
  const dash = link.kind === "favors" ? FAVORS_DASH : null;
  if (link.valence !== undefined) {
    const color = dimmed ? palette.faint : link.valence < 0 ? palette.against : palette.support;
    return { color, width: 0.75 + 3.5 * Math.abs(link.valence), dash };
  }
  const color = dimmed ? palette.faint : (palette.edges[link.kind] ?? palette.fallback);
  return { color, width: Math.min(5, 0.75 + 0.6 * Math.sqrt(endDegree)), dash };
}

/**
 * The palette the theme's tokens under `element` give, for the server's
 * `kinds`, `edgeKinds` and `statuses`: `--g-kind-<Kind>`, `--g-edge-<kind>` and
 * `--g-status-<status>`, each `--g-fallback` when unset; valence takes the
 * graph's `--g-vpos` and `--g-vneg` (the app's list colours behind them), edges
 * outside a focus `--g-faint`, and the halos the app's `--text`, `--amber` and
 * `--accent-hover`.
 */
export function readPalette(
  element: Element,
  { kinds, edgeKinds, statuses }: { kinds: readonly string[]; edgeKinds: readonly string[]; statuses: readonly string[] },
): Palette {
  const style = getComputedStyle(element);
  const token = (name: string) => style.getPropertyValue(name).trim();
  const fallback = token("--g-fallback");
  const each = (names: readonly string[], prefix: string) =>
    Object.fromEntries(names.map((name) => [name, token(`${prefix}${name}`) || fallback]));
  return {
    kinds: each(kinds, "--g-kind-"),
    edges: each(edgeKinds, "--g-edge-"),
    status: each(statuses, "--g-status-"),
    fallback,
    background: token("--g-bg") || token("--bg"),
    support: token("--g-vpos") || token("--l-evidence"),
    against: token("--g-vneg") || token("--l-against"),
    faint: token("--g-faint"),
    halo: token("--text"),
    match: token("--accent-hover"),
    duoRecord: token("--g-duo-record") || fallback,
    duoBelief: token("--g-duo-belief") || fallback,
    highlight: token("--amber"),
  };
}

const RETIRED_ALPHA = 0.25;
/** The design's base radius, by kind, before a hub's degree lifts it. */
const BASE_OF: { [kind: string]: number } = {
  Belief: 3.8,
  Issue: 2.9,
  Experiment: 3.3,
  Paper: 2.3,
  CodeChange: 2.3,
  WebSearch: 2.2,
  WebResult: 1.9,
  AgentSession: 3.4,
  Artifact: 2.5,
};
/** Under the retired look's alpha, so a dimmed node never reads as retired work (the design's 0.14). */
const DIMMED_ALPHA = 0.14;
/** How much of its look a node two or more hops from the focus keeps. */
const FAR_FADE = 0.7;
const LABEL_CHARS = 40;
const FAVORS_DASH = [4, 3];
/** At or below this zoom the graph is two tints; above `KIND_ZOOM` it is the kind hues, and between, a blend. */
export const DUO_ZOOM = 0.55;
const KIND_ZOOM = 1.3;
/** How many blends the ease takes, so panning asks for a handful of colours and not one a frame. */
const BLEND_STEPS = 4;
const mixed = new Map<string, string>();
