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
  /** A citation with a positive valence; the app's evidence colour. */
  readonly support: string;
  /** A citation with a negative valence. */
  readonly against: string;
  /** Every edge outside the focus's: hovered or selected, it dims the rest. */
  readonly faint: string;
  /** The ring around the focus and the selection: the text colour. */
  readonly halo: string;
  /** The ring around a search match: the app's link accent. */
  readonly match: string;
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
};

/** Where a node stands in what the view lights and finds. */
export type Standing = {
  /** Outside the focus, or a hovered or selected node's neighbourhood. */
  readonly dimmed?: boolean;
  /** Two or more hops from the focus. */
  readonly far?: boolean;
  /** `strong` for the focus and the selection, `match` for a search match. */
  readonly halo?: "strong" | "match" | null;
  /** Text to draw under it. */
  readonly label?: string | null;
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
 * the text colour, and a search match in the accent. A `label` shows under it,
 * at most 40 characters.
 */
export function nodeLook(
  shape: NodeShape,
  palette: Palette,
  { dimmed = false, far = false, halo = null, label = null }: Standing = {},
): NodeLook {
  const fill = palette.kinds[shape.kind] ?? palette.fallback;
  const retired = shape.status === "abandoned" || shape.status === "invalid";
  const belief = shape.kind === "Belief";
  const judgement = belief ? shape.judgement : undefined;
  const fade = far ? FAR_FADE : 1;
  const alpha = (dimmed ? DIMMED_ALPHA : retired ? RETIRED_ALPHA : 1) * fade;
  const fillAlpha = dimmed || retired ? alpha : (0.35 + 0.65 * (belief ? (shape.confidence ?? 1) : 1)) * fade;
  const radius = Math.min(14, 4 + 2.2 * Math.sqrt(shape.degree));
  return {
    radius: shape.root ? Math.max(radius, 12) + 4 : radius,
    fill,
    fillAlpha: judgement === "disproven" ? 0 : fillAlpha,
    wash: judgement === "undecidable" ? palette.background : null,
    washAlpha: fillAlpha * 0.4,
    ring: judgement === "disproven" ? fill : (palette.status[shape.status] ?? palette.fallback),
    ringAlpha: alpha,
    dot: judgement === "proven" && !retired && !dimmed ? (palette.status.complete ?? palette.fallback) : null,
    halo: halo === "strong" ? palette.halo : halo === "match" ? palette.match : null,
    label: label === null ? null : { text: label.length > LABEL_CHARS ? `${label.slice(0, LABEL_CHARS - 1)}…` : label, color: palette.halo, outline: palette.background },
  };
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
 * app's `--l-evidence` and `--l-against`, edges outside a focus `--g-faint`,
 * and the halos the app's `--text` and `--accent-hover`.
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
    background: token("--bg"),
    support: token("--l-evidence"),
    against: token("--l-against"),
    faint: token("--g-faint"),
    halo: token("--text"),
    match: token("--accent-hover"),
  };
}

const RETIRED_ALPHA = 0.25;
/** Under the retired look's alpha, so a dimmed node never reads as retired work (the design's 0.14). */
const DIMMED_ALPHA = 0.14;
/** How much of its look a node two or more hops from the focus keeps. */
const FAR_FADE = 0.7;
const LABEL_CHARS = 40;
const FAVORS_DASH = [4, 3];
