import {
  categoryColors,
  type LensMode,
  type LensOverride,
  type Palette,
} from "./encode";
import type { DrawLink, DrawNode } from "./model";

/** One entry in the dynamic `Key` for the active lens. */
export type LensKeyItem = {
  readonly key: string;
  readonly label: string;
  readonly color: string;
  readonly ring?: string;
  readonly count?: number;
};

/** Output of applying a `GraphLens` to the loaded graph. */
export type LensResult = {
  readonly overrides: Map<string, LensOverride> | null;
  readonly keyItems: LensKeyItem[];
  readonly crossBoundary: Set<DrawLink> | null;
  readonly linkAlpha: Map<DrawLink, number> | null;
};

/** Strategy contract for a graph visual encoding lens. */
export interface GraphLens {
  readonly id: LensMode;
  readonly label: string;
  readonly short: string;
  readonly hint: string;
  compute(
    nodes: readonly DrawNode[],
    links: readonly DrawLink[],
    palette: Palette,
  ): LensResult;
}

const EMPTY_RESULT: LensResult = {
  overrides: null,
  keyItems: [],
  crossBoundary: null,
  linkAlpha: null,
};

/** Default inquiry-kind coloring; `nodeLook` handles it with no overrides. */
const KindLens: GraphLens = {
  id: "kind",
  label: "Kind",
  short: "Inquiry type",
  hint: "Color by inquiry kind",
  compute: () => EMPTY_RESULT,
};

const UNPROVEN_COLOR = "#eab308";

/** Factual lens showing Belief `judgement` states (`proven`, `unproven`, `disproven`). */
const EpistemicLens: GraphLens = {
  id: "epistemic",
  label: "Epistemic",
  short: "Belief verdicts",
  hint: "Highlight proven, unproven, and disproven Beliefs",
  compute(nodes, _links, palette) {
    const overrides = new Map<string, LensOverride>();
    for (const node of nodes) {
      if (node.kind === "Belief") {
        if (node.judgement === "proven") {
          overrides.set(node.id, {
            fill: palette.support,
            ring: palette.support,
            alpha: 1,
          });
        } else if (node.judgement === "unproven") {
          overrides.set(node.id, {
            fill: UNPROVEN_COLOR,
            ring: UNPROVEN_COLOR,
            alpha: 0.95,
          });
        } else if (node.judgement === "disproven") {
          overrides.set(node.id, {
            fill: palette.against,
            fillAlpha: 0,
            ring: palette.against,
            alpha: 1,
          });
        } else {
          overrides.set(node.id, {
            fill: palette.fallback,
            ring: palette.fallback,
            alpha: 0.2,
          });
        }
      } else {
        overrides.set(node.id, {
          fill: palette.fallback,
          ring: palette.fallback,
          alpha: 0.2,
        });
      }
    }
    const keyItems: LensKeyItem[] = [
      {
        key: "proven",
        label: "Proven",
        color: palette.support,
        ring: palette.support,
      },
      {
        key: "unproven",
        label: "Unproven",
        color: UNPROVEN_COLOR,
        ring: UNPROVEN_COLOR,
      },
      {
        key: "disproven",
        label: "Disproven",
        color: "transparent",
        ring: palette.against,
      },
      {
        key: "other",
        label: "Other / unjudged",
        color: palette.fallback,
        ring: palette.fallback,
      },
    ];
    return { overrides, keyItems, crossBoundary: null, linkAlpha: null };
  },
};

const BLUE_SHADES = ["#10233b", "#1a3c66", "#255799", "#3474d4", "#4d90f0"] as const;
const GREEN_SHADES = ["#112b20", "#245c43", "#388e65", "#5ec48e", "#c2fce0"] as const;

/** Shared base for continuous single-hue spectrum lenses (`Depth` and `Recency`). */
class SpectrumLens implements GraphLens {
  readonly id: LensMode;
  readonly label: string;
  readonly short: string;
  readonly hint: string;
  readonly #shades: readonly string[];
  readonly #minAlpha: number;
  readonly #rank: (
    nodes: readonly DrawNode[],
    links: readonly DrawLink[],
  ) => { ratios: Map<string, number>; labels: [string, string, string] };

  constructor(spec: {
    id: LensMode;
    label: string;
    short: string;
    hint: string;
    shades: readonly string[];
    minAlpha: number;
    rank: (
      nodes: readonly DrawNode[],
      links: readonly DrawLink[],
    ) => { ratios: Map<string, number>; labels: [string, string, string] };
  }) {
    this.id = spec.id;
    this.label = spec.label;
    this.short = spec.short;
    this.hint = spec.hint;
    this.#shades = spec.shades;
    this.#minAlpha = spec.minAlpha;
    this.#rank = spec.rank;
  }

  compute(nodes: readonly DrawNode[], links: readonly DrawLink[]): LensResult {
    const { ratios, labels } = this.#rank(nodes, links);
    const overrides = new Map<string, LensOverride>();
    for (const node of nodes) {
      const ratio = ratios.get(node.id);
      if (ratio === undefined) {
        overrides.set(node.id, {
          fill: this.#shades[0]!,
          ring: this.#shades[0]!,
          alpha: 0.12,
        });
        continue;
      }
      const shade = shadeAt(this.#shades, ratio);
      overrides.set(node.id, {
        fill: shade,
        ring: shade,
        alpha: this.#minAlpha + (1 - this.#minAlpha) * ratio,
      });
    }
    const linkAlpha = new Map<DrawLink, number>();
    for (const link of links) {
      const r = Math.max(ratios.get(link.from) ?? 0, ratios.get(link.to) ?? 0);
      linkAlpha.set(link, 0.1 + 0.9 * r);
    }
    const keyItems: LensKeyItem[] = [
      { key: "lo", label: labels[0], color: shadeAt(this.#shades, 0) },
      { key: "mid", label: labels[1], color: shadeAt(this.#shades, 0.5) },
      { key: "hi", label: labels[2], color: shadeAt(this.#shades, 1) },
    ];
    return { overrides, keyItems, crossBoundary: null, linkAlpha };
  }
}

/** Shared base for categorical lenses (`Account`, `Owner`, `Topic / Tag`). */
class CategoryLens implements GraphLens {
  readonly id: LensMode;
  readonly label: string;
  readonly short: string;
  readonly hint: string;
  readonly #missingLabel: string;
  readonly #highlightCross: boolean;
  readonly #pick: (
    nodes: readonly DrawNode[],
  ) => Map<string, string>;

  constructor(spec: {
    id: LensMode;
    label: string;
    short: string;
    hint: string;
    missingLabel: string;
    highlightCross?: boolean;
    pick: (nodes: readonly DrawNode[]) => Map<string, string>;
  }) {
    this.id = spec.id;
    this.label = spec.label;
    this.short = spec.short;
    this.hint = spec.hint;
    this.#missingLabel = spec.missingLabel;
    this.#highlightCross = spec.highlightCross ?? false;
    this.#pick = spec.pick;
  }

  compute(
    nodes: readonly DrawNode[],
    links: readonly DrawLink[],
    palette: Palette,
  ): LensResult {
    const valueOf = this.#pick(nodes);
    const counts = new Map<string, number>();
    let missing = 0;
    for (const node of nodes) {
      const val = valueOf.get(node.id);
      if (val) counts.set(val, (counts.get(val) ?? 0) + 1);
      else missing += 1;
    }
    const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const wheel = categoryColors(palette);
    const colorOf = new Map(
      ranked.map(([name], i) => [name, wheel[i % wheel.length]!]),
    );
    const overrides = new Map<string, LensOverride>();
    for (const node of nodes) {
      const val = valueOf.get(node.id);
      if (val) {
        overrides.set(node.id, { fill: colorOf.get(val)!, alpha: 1 });
      } else {
        overrides.set(node.id, { fill: palette.fallback, alpha: 0.18 });
      }
    }
    let crossBoundary: Set<DrawLink> | null = null;
    if (this.#highlightCross) {
      crossBoundary = new Set<DrawLink>();
      for (const link of links) {
        const a = valueOf.get(link.from);
        const b = valueOf.get(link.to);
        if (a && b && a !== b) crossBoundary.add(link);
      }
    }
    const keyItems: LensKeyItem[] = [
      ...ranked.slice(0, 8).map(([name, count]) => ({
        key: name,
        label: name,
        color: colorOf.get(name)!,
        count,
      })),
      ...(missing > 0
        ? [
            {
              key: "_missing",
              label: this.#missingLabel,
              color: palette.fallback,
              count: missing,
            },
          ]
        : []),
    ];
    return { overrides, keyItems, crossBoundary, linkAlpha: null };
  }
}

const MAX_DEPTH = 9; // 10 levels: 0..9

const DepthLens = new SpectrumLens({
  id: "depth",
  label: "Depth",
  short: "Hops from root",
  hint: "Brighter with distance from root",
  shades: BLUE_SHADES,
  minAlpha: 0.2,
  rank(nodes, links) {
    const roots = new Set(nodes.filter((n) => n.root).map((n) => n.id));
    const hasNonRootParent = new Set<string>();
    for (const link of links) {
      if (
        (link.kind === "narrows" ||
          link.kind === "produced_by" ||
          link.kind === "requires") &&
        !roots.has(link.to)
      ) {
        hasNonRootParent.add(link.from);
      }
    }
    const children = new Map<string, string[]>();
    const addEdge = (parent: string, child: string) => {
      const list = children.get(parent);
      if (list) list.push(child);
      else children.set(parent, [child]);
    };
    for (const link of links) {
      if (
        link.kind === "narrows" ||
        link.kind === "produced_by" ||
        link.kind === "requires"
      ) {
        if (roots.has(link.to) && hasNonRootParent.has(link.from)) continue;
        addEdge(link.to, link.from);
      } else if (link.kind === "proves" || link.kind === "favors") {
        addEdge(link.from, link.to);
      }
    }
    const depth = new Map<string, number>();
    for (const id of roots) depth.set(id, 0);
    for (let pass = 0; pass < MAX_DEPTH; pass++) {
      let changed = false;
      for (const [at, d] of depth) {
        if (d >= MAX_DEPTH) continue;
        for (const child of children.get(at) ?? []) {
          const next = Math.min(MAX_DEPTH, d + 1);
          if ((depth.get(child) ?? -1) < next) {
            depth.set(child, next);
            changed = true;
          }
        }
      }
      if (!changed) break;
    }
    const maxDepth = Math.max(1, ...depth.values());
    const ratios = new Map<string, number>();
    for (const [id, d] of depth) ratios.set(id, d / maxDepth);
    return {
      ratios,
      labels: ["Depth 0 (shallowest)", "Mid depth", `Depth ${maxDepth} (deepest)`],
    };
  },
});

const RecencyLens = new SpectrumLens({
  id: "recency",
  label: "Recency",
  short: "Creation time",
  hint: "Earliest to latest creation time",
  shades: GREEN_SHADES,
  minAlpha: 0.16,
  rank(nodes) {
    const dated = nodes
      .map((node) => ({ id: node.id, t: Date.parse(node.created) }))
      .filter((entry) => Number.isFinite(entry.t))
      .sort((a, b) => a.t - b.t);
    const ratios = new Map<string, number>();
    const denom = Math.max(1, dated.length - 1);
    for (const [index, entry] of dated.entries()) {
      ratios.set(entry.id, index / denom);
    }
    return {
      ratios,
      labels: ["Oldest (dark/dim)", "Middle", "Latest (bright mint)"],
    };
  },
});

function fieldPicker(pick: (node: DrawNode) => string | undefined) {
  return (nodes: readonly DrawNode[]) => {
    const out = new Map<string, string>();
    for (const node of nodes) {
      const val = pick(node);
      if (val) out.set(node.id, val);
    }
    return out;
  };
}

const AccountLens = new CategoryLens({
  id: "account",
  label: "Account",
  short: "By author",
  hint: "Color by author account & bridges",
  missingLabel: "No account",
  highlightCross: true,
  pick: fieldPicker((n) => n.account),
});

export const LENSES: readonly GraphLens[] = [
  KindLens,
  EpistemicLens,
  DepthLens,
  RecencyLens,
  AccountLens,
];

export const LENS_MODES: ReadonlySet<string> = new Set(LENSES.map((l) => l.id));

const LENS_BY_ID = new Map<LensMode, GraphLens>(LENSES.map((l) => [l.id, l]));

export function computeLens(
  mode: LensMode,
  nodes: readonly DrawNode[],
  links: readonly DrawLink[],
  palette: Palette,
): LensResult {
  return (LENS_BY_ID.get(mode) ?? KindLens).compute(nodes, links, palette);
}

/** Continuously interpolate across hex color stops for `ratio` in `[0, 1]`. */
function shadeAt(shades: readonly string[], ratio: number): string {
  const clamped = Math.min(1, Math.max(0, ratio));
  const scaled = clamped * (shades.length - 1);
  const lo = Math.floor(scaled);
  const hi = Math.min(shades.length - 1, lo + 1);
  return lerpHex(shades[lo]!, shades[hi]!, scaled - lo);
}

function lerpHex(a: string, b: string, t: number): string {
  const ar = Number.parseInt(a.slice(1, 3), 16);
  const ag = Number.parseInt(a.slice(3, 5), 16);
  const ab = Number.parseInt(a.slice(5, 7), 16);
  const br = Number.parseInt(b.slice(1, 3), 16);
  const bg = Number.parseInt(b.slice(3, 5), 16);
  const bb = Number.parseInt(b.slice(5, 7), 16);
  const r = Math.round(ar + (br - ar) * t);
  const g = Math.round(ag + (bg - ag) * t);
  const bl = Math.round(ab + (bb - ab) * t);
  return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${bl.toString(16).padStart(2, "0")}`;
}
