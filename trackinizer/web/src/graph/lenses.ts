import type { LensMode, LensOverride, Palette } from "./encode";
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
    const other: LensOverride = {
      fill: palette.fallback,
      alpha: 0.2,
    };
    const verdicts: readonly {
      readonly key: string;
      readonly label: string;
      readonly override: LensOverride;
    }[] = [
      {
        key: "proven",
        label: "Proven",
        override: { fill: palette.support, alpha: 1 },
      },
      {
        key: "unproven",
        label: "Unproven",
        override: { fill: UNPROVEN_COLOR, alpha: 0.95 },
      },
      {
        key: "disproven",
        label: "Disproven",
        override: { fill: palette.against, fillAlpha: 0, alpha: 1 },
      },
      { key: "other", label: "Other / unjudged", override: other },
    ];
    const byJudgement = new Map(verdicts.map((v) => [v.key, v.override]));
    const overrides = new Map<string, LensOverride>();
    for (const node of nodes) {
      const key = node.kind === "Belief" ? (node.judgement ?? "other") : "other";
      overrides.set(node.id, byJudgement.get(key) ?? other);
    }
    const keyItems: LensKeyItem[] = verdicts.map(({ key, label, override }) => ({
      key,
      label,
      color: override.fillAlpha === 0 ? "transparent" : override.fill,
      ring: override.fillAlpha === 0 ? override.fill : undefined,
    }));
    return { overrides, keyItems };
  },
};

/** Factory for continuous single-hue spectrum lenses (`Depth` and `Recency`). */
function spectrumLens(spec: {
  id: LensMode;
  label: string;
  short: string;
  hint: string;
  stops: readonly [string, string];
  minAlpha: number;
  rank: (
    nodes: readonly DrawNode[],
    links: readonly DrawLink[],
  ) => { ratios: Map<string, number>; labels: [string, string, string] };
}): GraphLens {
  const [dark, bright] = spec.stops;
  return {
    id: spec.id,
    label: spec.label,
    short: spec.short,
    hint: spec.hint,
    compute(nodes, links) {
      const { ratios, labels } = spec.rank(nodes, links);
      const overrides = new Map<string, LensOverride>();
      for (const node of nodes) {
        const ratio = ratios.get(node.id);
        if (ratio === undefined) {
          overrides.set(node.id, { fill: dark, alpha: 0.12 });
          continue;
        }
        const color = lerpHex(dark, bright, ratio);
        overrides.set(node.id, {
          fill: color,
          alpha: spec.minAlpha + (1 - spec.minAlpha) * ratio,
        });
      }
      const keyItems: LensKeyItem[] = [
        { key: "lo", label: labels[0], color: dark },
        { key: "mid", label: labels[1], color: lerpHex(dark, bright, 0.5) },
        { key: "hi", label: labels[2], color: bright },
      ];
      return { overrides, keyItems };
    },
  };
}

const MAX_DEPTH = 9; // 10 levels: 0..9

const DepthLens = spectrumLens({
  id: "depth",
  label: "Depth",
  short: "Hops from root",
  hint: "Brighter with distance from root",
  stops: ["#10233b", "#4d90f0"],
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

const RecencyLens = spectrumLens({
  id: "recency",
  label: "Recency",
  short: "Creation time",
  hint: "Earliest to latest creation time",
  stops: ["#112b20", "#8cf0be"],
  minAlpha: 0.16,
  rank(nodes) {
    const dated = nodes
      .map((node) => ({ id: node.id, t: Date.parse(node.created) }))
      .filter((entry) => Number.isFinite(entry.t))
      .sort((a, b) => a.t - b.t);
    const ratios = new Map<string, number>();
    const denom = Math.max(1, dated.length - 1);
    for (const [index, entry] of dated.entries()) ratios.set(entry.id, index / denom);
    return {
      ratios,
      labels: ["Oldest (dark/dim)", "Middle", "Latest (bright mint)"],
    };
  },
});

const AccountLens: GraphLens = {
  id: "account",
  label: "Account",
  short: "By author",
  hint: "Color by author account",
  compute(nodes, _links, palette) {
    const counts = new Map<string, number>();
    let missing = 0;
    for (const node of nodes) {
      if (node.account) counts.set(node.account, (counts.get(node.account) ?? 0) + 1);
      else missing += 1;
    }
    const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const wheel = categoryColors(palette);
    const colorOf = new Map(ranked.map(([name], i) => [name, wheel[i % wheel.length]!]));
    const overrides = new Map<string, LensOverride>();
    for (const node of nodes) {
      if (node.account) {
        overrides.set(node.id, { fill: colorOf.get(node.account)!, alpha: 1 });
      } else {
        overrides.set(node.id, { fill: palette.fallback, alpha: 0.18 });
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
        ? [{ key: "_missing", label: "No account", color: palette.fallback, count: missing }]
        : []),
    ];
    return { overrides, keyItems };
  },
};

/** Single source of truth for all graph lenses, in menu order. */
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

function categoryColors(palette: Palette): string[] {
  const hues = Object.values(palette.kinds).filter(
    (color) => color && color !== palette.fallback,
  );
  return hues.length > 0 ? hues : [palette.fallback];
}

function lerpHex(a: string, b: string, ratio: number): string {
  const t = Math.min(1, Math.max(0, ratio));
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
