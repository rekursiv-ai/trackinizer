// The graph's controls, built from the lists' parts: the toolbar's menus
// (`Menu`, `.btn.ghost`), the filter chips (`.fchips`), the segmented toggles
// (`.view-seg`), and the key, zoom buttons and tooltip over the canvas.
import { useState } from "react";
import { ALL_NODES, type GraphNode } from "../api/graph";
import type { Meta } from "../app/boot";
import { sentence } from "../detail/relationGroups";
import { Menu, type MenuOption } from "../lists/Menu";
import { HOPS, type Hops } from "../router/route";
import { capitalize, StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { EmptyState } from "../ui/view";
import { type Palette, panelKind, panelTint, panelValence } from "./encode";
import type { DrawNode } from "./model";
import { hopsName } from "./search";

/** The kinds, statuses and owners the Filter hides. */
export type Hidden = {
  readonly kinds: readonly string[];
  readonly statuses: readonly string[];
  readonly owners: readonly string[];
};
type Field = keyof Hidden;

/**
 * The Filter menu, as a list's: pick Kind, Status or Owner, then tick what
 * shows. Each value has its count among the nodes loaded; all show at first.
 * Owner lists the people: a session's owner is the agent that ran it, since
 * the server keys live sessions by owner, so the people are every other
 * record's owner -- and ticking one off leaves that person's lines of work.
 */
export function FilterMenu({
  meta,
  nodes,
  hidden,
  onToggle,
}: {
  meta: Meta;
  nodes: readonly GraphNode[];
  hidden: Hidden;
  onToggle: (field: Field, value: string) => void;
}) {
  const [field, setField] = useState<Field | null>(null);
  const tally = (pick: (node: GraphNode) => string, value: string) => String(nodes.filter((node) => pick(node) === value).length);
  const people = [...new Set(nodes.flatMap((node) => (node.kind !== "AgentSession" && node.owner !== undefined ? [node.owner] : [])))].sort();
  const options: MenuOption[] =
    field === "kinds"
      ? meta.kinds.map((kind) => ({
          value: kind,
          label: kindLook(kind).plural,
          icon: <KindIcon kind={kind} size={14} />,
          hint: tally((node) => node.kind, kind),
          checked: !hidden.kinds.includes(kind),
        }))
      : field === "statuses"
        ? (meta.enums.status ?? []).map((status) => ({
            value: status,
            label: capitalize(status),
            icon: <StatusGlyph status={status} />,
            hint: tally((node) => node.status, status),
            checked: !hidden.statuses.includes(status),
          }))
        : field === "owners"
          ? people.map((owner) => ({
              value: owner,
              label: owner,
              hint: tally((node) => node.owner ?? "", owner),
              checked: !hidden.owners.includes(owner),
            }))
          : FIELDS.map(([value, label]) => ({ value, label, icon: <Icon name="filter" size={14} /> }));
  const count = hidden.kinds.length + hidden.statuses.length + hidden.owners.length;
  return (
    <Menu
      label={field === "kinds" ? "Show kinds…" : field === "statuses" ? "Show statuses…" : field === "owners" ? "Show owners…" : "Filter by…"}
      trigger={
        <button type="button" className="btn ghost">
          <Icon name="filter" size={14} />
          Filter
          {count > 0 ? <span className="nav-badge">{count}</span> : null}
        </button>
      }
      options={options}
      multi
      onPick={(value) => {
        if (field) onToggle(field, value);
        else setField(FIELDS.find(([name]) => name === value)?.[0] ?? null);
      }}
      onBack={field ? () => setField(null) : undefined}
      onClose={() => setField(null)}
    />
  );
}

/**
 * The chips under the tools, as a list's: what the Filter hides, each removable,
 * and Clear. A chip names what shows instead ("Kind is Papers") when, of the
 * values `present` in the nodes loaded, fewer show than it hides.
 */
export function FilterChips({
  hidden,
  present,
  onRemove,
  onClear,
}: {
  hidden: Hidden;
  present: Hidden;
  onRemove: (field: Field) => void;
  onClear: () => void;
}) {
  const chips = FIELDS.filter(([field]) => hidden[field].length > 0);
  if (chips.length === 0) return null;
  const name = (field: Field, value: string) => (field === "kinds" ? kindLook(value).plural : field === "owners" ? value : capitalize(value));
  const phrase = (field: Field) => {
    const shown = present[field].filter((value) => !hidden[field].includes(value));
    const named = (values: readonly string[]) => values.map((value) => name(field, value)).join(", ");
    return shown.length > 0 && shown.length < hidden[field].length ? `is ${named(shown)}` : `is not ${named(hidden[field])}`;
  };
  return (
    <div className="fchips">
      {chips.map(([field, label]) => (
        <span key={field} className="fchip">
          <span className="k">{label}</span> {phrase(field)}
          <button type="button" aria-label={`Remove the ${label} filter`} onClick={() => onRemove(field)}>
            <Icon name="x" size={12} />
          </button>
        </span>
      ))}
      <button type="button" className="btn ghost" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}

/**
 * The node limit: 100, 1k, 5k or All, or any count typed into the menu's box,
 * as 2500, 2,500 or 2.5k. A typed count offers only the option of that count, a
 * preset or its own; matched as text, 50 would offer 5k first.
 */
export function LimitMenu({ limit, onLimit }: { limit: number; onLimit: (limit: number) => void }) {
  const presets = LIMITS.map(([value, name]) => ({ value: String(value), label: `${name} nodes`, checked: value === limit }));
  const typed = LIMITS.some(([value]) => value === limit) ? [] : [{ value: String(limit), label: `${limitName(limit)} nodes`, checked: true }];
  return (
    <Menu
      label="Nodes to show…"
      trigger={
        <button type="button" className="btn ghost" title="The newest inquiries to draw, each with the older ones it links to">
          <Icon name="layers" size={14} />
          Nodes: {limitName(limit)}
        </button>
      }
      options={[...presets, ...typed]}
      match={(option, text) => {
        const count = parseCount(text);
        return count === null ? option.label.toLowerCase().includes(text.toLowerCase()) : option.value === String(count);
      }}
      fixed={(text) => {
        const count = parseCount(text);
        return count === null || count === limit || LIMITS.some(([value]) => value === count)
          ? []
          : [{ value: String(count), label: `${limitName(count)} nodes` }];
      }}
      onPick={(value) => onLimit(Number(value))}
    />
  );
}

/** How the limit reads: `1k`, `2,500`, `All`. */
function limitName(limit: number): string {
  return LIMITS.find(([value]) => value === limit)?.[1] ?? limit.toLocaleString("en-US");
}

/** The count `text` names, as 2500, 2,500 or 2.5k, or null for anything else or one past All. */
function parseCount(text: string): number | null {
  const found = /^(\d[\d,]*(?:\.\d+)?)\s*(k?)$/i.exec(text.trim());
  if (!found) return null;
  const count = Number(found[1]!.replaceAll(",", "")) * (found[2] ? 1000 : 1);
  return Number.isInteger(count) && count >= 1 && count <= ALL_NODES ? count : null;
}

/** Replay and its speeds: a pick starts it at that speed, or, while it runs, changes the speed; Stop ends it. */
export function ReplayMenu({
  running,
  speed,
  onSpeed,
  onStop,
}: {
  running: boolean;
  speed: number;
  onSpeed: (speed: number) => void;
  onStop: () => void;
}) {
  return (
    <Menu
      label="Replay at…"
      trigger={
        <button type="button" className="btn ghost" title="Grow the graph again in the order it was made">
          <Icon name="clock" size={14} />
          {running ? `Replaying at ${speed}x` : "Replay"}
        </button>
      }
      options={SPEEDS.map((option) => ({ value: String(option), label: `${option}x`, checked: running && option === speed }))}
      fixed={() => (running ? [{ value: STOP, label: "Stop" }] : [])}
      onPick={(value) => (value === STOP ? onStop() : onSpeed(Number(value)))}
    />
  );
}

/**
 * The focus row, under the tools while a focus is held: the focused node, Hops
 * 1, 2, 3 or All, each with how many nodes it reaches, Dim the rest or Only
 * these, and Through, the edge kinds the walk follows; then what is lit.
 */
export function FocusRow({
  name,
  node,
  hops,
  counts,
  lit,
  only,
  skipEdges,
  meta,
  onHops,
  onOnly,
  onSkip,
  onClear,
}: {
  /** The focus's `Kind#seq`, or its id. */
  name: string;
  /** The focused node, or null when the graph has not loaded it. */
  node: GraphNode | null;
  hops: Hops;
  counts: { readonly [hops in Hops]: number };
  lit: { readonly nodes: number; readonly edges: number };
  only: boolean;
  skipEdges: readonly string[];
  meta: Meta;
  onHops: (hops: Hops) => void;
  onOnly: (only: boolean) => void;
  onSkip: (edgeKind: string) => void;
  onClear: () => void;
}) {
  const edgeKinds = Object.keys(meta.edges);
  const edgeName = (kind: string) => sentence(meta.edges[kind]?.forward ?? kind);
  const walked = edgeKinds.filter((kind) => !skipEdges.includes(kind));
  const skipped = edgeKinds.filter((kind) => skipEdges.includes(kind));
  const through =
    skipped.length === 0
      ? "all edges"
      : walked.length === 0
        ? "no edges"
        : skipped.length <= walked.length
          ? `all but ${skipped.map(edgeName).join(", ")}`
          : walked.map(edgeName).join(", ");
  return (
    <div className="fchips graph-focus" role="group" aria-label="Focus">
      <span className="muted">Focus</span>
      <span className="fchip">
        {node ? <KindIcon kind={node.kind} size={14} /> : null}
        {node ? <StatusGlyph status={node.status} size={12} /> : null}
        <span className="graph-focus-name">{node ? `${name} · ${node.title || "(untitled)"}` : `${name} is not among the nodes loaded`}</span>
        <button type="button" aria-label="Clear the focus" onClick={onClear}>
          <Icon name="x" size={12} />
        </button>
      </span>
      {node ? (
        <>
          <span className="muted">Hops</span>
          <div className="view-seg" role="group" aria-label="Hops">
            {HOPS.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={option === hops}
                aria-label={`${option === "all" ? "All hops" : hopsName(option)}, ${counts[option]} nodes`}
                onClick={() => onHops(option)}
              >
                {option === "all" ? "All" : option}
                <span className="graph-seg-count num">{counts[option].toLocaleString("en-US")}</span>
              </button>
            ))}
          </div>
          <div className="view-seg" role="group" aria-label="Outside the focus">
            <button type="button" aria-pressed={!only} onClick={() => onOnly(false)}>
              Dim the rest
            </button>
            <button type="button" aria-pressed={only} onClick={() => onOnly(true)}>
              Only these
            </button>
          </div>
          <Menu
            label="Walk through…"
            trigger={
              <button type="button" className="btn ghost">
                <Icon name="swap" size={14} />
                Through: {through}
              </button>
            }
            options={edgeKinds.map((kind) => ({ value: kind, label: edgeName(kind), checked: !skipEdges.includes(kind) }))}
            multi
            onPick={onSkip}
          />
          <span className="spacer" />
          <span className="muted num">
            {lit.nodes.toLocaleString("en-US")} nodes · {lit.edges.toLocaleString("en-US")} edges
          </span>
        </>
      ) : null}
    </div>
  );
}

/**
 * The key over the canvas, in the design's four sections. `beliefs` reads a
 * Belief's three verdicts; `nodes, full color when zoomed in` lists each kind
 * loaded as a button, as a chart legend's entries are: a hidden kind clicked
 * shows it too, and a shown one shows only it, unless it is the only one shown,
 * which shows every kind — each sets the kinds Filter hides, and a hidden kind
 * has no count. `between clusters` names the two hairline hues, and `links`
 * lists every edge kind by its stroke with how many are loaded, as toggle
 * buttons: one clicked hides that kind's wires, as `Through` does.
 */
export function Key({
  id,
  meta,
  palette,
  kinds,
  nodes,
  edges,
  hiddenKinds,
  onHiddenKinds,
  hiddenEdges,
  onHiddenEdges,
}: {
  id: string;
  meta: Meta;
  palette: Palette;
  /** The kinds of the nodes loaded, in the server's order. */
  kinds: readonly string[];
  /** The nodes drawn. */
  nodes: readonly GraphNode[];
  /** The edges drawn. */
  edges: readonly { readonly kind: string; readonly valence?: number }[];
  hiddenKinds: readonly string[];
  onHiddenKinds: (hiddenKinds: readonly string[]) => void;
  hiddenEdges: readonly string[];
  onHiddenEdges: (hiddenEdges: readonly string[]) => void;
}) {
  const counts = tally(nodes.map((node) => node.kind));
  const edgeCounts = tally(edges.map((edge) => edge.kind));
  // The panel's colours: one step lighter than the graph's, as the design's are.
  const shown = kinds.filter((kind) => !hiddenKinds.includes(kind));
  const pick = (kind: string): { readonly title: string; readonly hidden: readonly string[] } => {
    const { plural } = kindLook(kind);
    if (hiddenKinds.includes(kind)) return { title: `Show ${plural} too`, hidden: hiddenKinds.filter((other) => other !== kind) };
    if (shown.length === 1) return { title: "Show every kind", hidden: [] };
    return { title: `Show only ${plural}`, hidden: meta.kinds.filter((other) => other !== kind) };
  };
  const belief = palette.kinds.Belief ?? palette.fallback;
  const linkRows = Object.keys(meta.edges)
    .filter((kind) => edgeCounts.has(kind))
    .map((kind) => {
      const cited = kind === "proves" || kind === "favors";
      return {
        kind,
        name: sentence(meta.edges[kind]!.forward),
        color: cited ? panelValence(palette.support) : panelTint(palette.edges[kind] ?? palette.fallback),
        dashed: kind === "favors",
        count: (edgeCounts.get(kind) ?? 0) as number,
      };
    });
  return (
    <aside id={id} className="graph-key" aria-label="Key">
      <div>
        <h2 id={`${id}-beliefs`}>beliefs</h2>
        <ul aria-labelledby={`${id}-beliefs`}>
          <li><span className="graph-mark"><span className="graph-dot" style={{ background: belief }} /></span>proven</li>
          <li><span className="graph-mark"><span className="graph-dot" style={{ background: belief, opacity: 0.55 }} /></span>unproven</li>
          <li><span className="graph-mark"><span className="graph-ring" style={{ borderColor: belief }} /></span>disproven</li>
        </ul>
      </div>
      <div>
        <h2 id={`${id}-kinds`}>nodes, full color when zoomed in</h2>
        <ul aria-labelledby={`${id}-kinds`}>
          {kinds.map((kind) => {
            const on = shown.includes(kind);
            const next = pick(kind);
            return (
              <li key={kind}>
                <button type="button" aria-pressed={on} title={next.title} onClick={() => onHiddenKinds(next.hidden)}>
                  <span className="graph-mark">
                    <span className="graph-dot" style={{ background: panelKind(palette, kind) }} />
                  </span>
                  {kindLook(kind).plural}
                  {on ? <span className="graph-tally num">{counts.get(kind)}</span> : null}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
      <div>
        <h2 id={`${id}-between`}>between clusters</h2>
        <ul aria-labelledby={`${id}-between`}>
          <li><span className="graph-mark"><span className="graph-stroke" style={{ borderTopColor: panelValence(palette.support) }} /></span>supporting evidence</li>
          <li><span className="graph-mark"><span className="graph-stroke" style={{ borderTopColor: panelValence(palette.against) }} /></span>counter-evidence</li>
        </ul>
      </div>
      <div>
        <h2 id={`${id}-links`}>links</h2>
        <ul aria-labelledby={`${id}-links`}>
          {linkRows.map((row) => {
            const off = hiddenEdges.includes(row.kind);
            return (
              <li key={row.kind}>
                <button
                  type="button"
                  aria-pressed={!off}
                  title={off ? `Show ${row.name.toLowerCase()} links` : `Hide ${row.name.toLowerCase()} links`}
                  onClick={() => onHiddenEdges(hiddenEdges.includes(row.kind) ? hiddenEdges.filter((other) => other !== row.kind) : [...hiddenEdges, row.kind])}
                >
                  <span className="graph-mark">
                    <span className="graph-stroke" style={{ borderTopColor: row.color, borderTopStyle: row.dashed ? "dashed" : "solid" }} />
                  </span>
                  {row.name}
                  <span className="graph-tally num">{off ? null : row.count}</span>
                </button>
              </li>
            );
          })}
          <li><span className="graph-mark"><span className="graph-stroke" style={{ borderTopColor: panelValence(palette.against) }} /></span>argues against</li>
        </ul>
      </div>
    </aside>
  );
}

/**
 * What a hovered node is, by it, as text, since a canvas has none: its kind
 * and status as the app draws them, its ref and, under a focus, its hops from
 * it (`far`); its title; and `hint`, what a click does where it is drawn.
 */
export function Tooltip({ node, x, y, far, hint }: { node: DrawNode; x: number; y: number; far: number | undefined; hint: string }) {
  return (
    <div className="graph-tip" role="tooltip" style={{ left: x + TIP_OFFSET_PX, top: y + TIP_OFFSET_PX }}>
      <div className="graph-tip-ref">
        <KindIcon kind={node.kind} size={12} />
        <StatusGlyph status={node.status} size={12} />
        {node.kind}#{node.seq}
        {far ? ` · ${hopsName(far)}` : null}
      </div>
      <div>{node.title || "(untitled)"}</div>
      <div className="graph-tip-hint">{hint}</div>
    </div>
  );
}

/** Zoom in and out by `ZOOM_STEP`, and frame every node shown, at the canvas's foot. */
export function ZoomButtons({ onZoom, onFit }: { onZoom: (factor: number) => void; onFit: () => void }) {
  return (
    <div className="graph-zoom" role="group" aria-label="Zoom">
      <button type="button" className="icon-btn" aria-label="Zoom in" onClick={() => onZoom(ZOOM_STEP)}>
        <Icon name="plus" size={15} />
      </button>
      <button type="button" className="icon-btn" aria-label="Zoom out" onClick={() => onZoom(1 / ZOOM_STEP)}>
        −
      </button>
      <button type="button" className="icon-btn" aria-label="Fit" title="Frame every node shown (F)" onClick={onFit}>
        <Icon name="maximize" size={14} />
      </button>
    </div>
  );
}

/** Over the canvas, in place of a graph too big to draw unasked: draw it, or read the newest `ASK_ABOVE` instead. */
export function AskToDraw({ count, onDraw, onFewer }: { count: number; onDraw: () => void; onFewer: () => void }) {
  return (
    <div className="graph-ask">
      <EmptyState icon={<Icon name="graph" size={24} />} title={`Draw ${count.toLocaleString("en-US")} nodes?`}>
        <p>
          Laying out more than {ASK_ABOVE.toLocaleString("en-US")} nodes keeps this tab busy until it settles, longer the more
          there are.
        </p>
        <span className="w-actions">
          <button type="button" className="btn primary" onClick={onDraw}>
            Draw them
          </button>
          <button type="button" className="btn" onClick={onFewer}>
            Show {limitName(ASK_ABOVE)} instead
          </button>
        </span>
      </EmptyState>
    </div>
  );
}

/**
 * The most nodes the view draws unasked. At 5,000 synthetic nodes the layout
 * drew 33 to 35 fps with no long task; at 7,500 every frame was a long task of
 * 50 to 69 ms, at 10,000 of 89 to 91 ms, for the 9 to 12 s the layout ran
 * (Chromium on the dev Mac, 2026-10-02).
 */
export const ASK_ABOVE = 5_000;

/** How many times each value comes. */
function tally(values: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return counts;
}

const FIELDS = [
  ["kinds", "Kind"],
  ["statuses", "Status"],
  ["owners", "Owner"],
] as const satisfies readonly (readonly [Field, string])[];
/** The node limits offered, by name: the three the design draws, and any count typed in. */
const LIMITS = [
  [1000, "1k"],
  [2500, "2.5k"],
  [5000, "5k"],
] as const;
/** v1's Replay speeds. */
const SPEEDS = [0.25, 0.5, 1, 2, 4, 10];
/** Stop's value among the speeds; no speed is spelled so. */
const STOP = "stop";
const ZOOM_STEP = 1.5;
const TIP_OFFSET_PX = 12;
