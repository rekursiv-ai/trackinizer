// The graph's own chrome, as the design prototype draws it: a briefing panel at
// the left that reads the whole graph, a timeline across the foot that replays
// it, and a legend that folds to a pill at the right. The canvas between them is
// the prototype's; these sit over it and never take its width.

import { animate, motion } from "framer-motion";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import type { Graph, GraphNode } from "../api/graph";
import { usd } from "../detail/fields";
import { type Panel, type PanelSpec } from "../ui/panel";
import {
  type GraphView,
  type NextExperiment,
  type Spend,
  costOf,
  evidenceOf,
  loadOf,
  nextExperiments,
  spendOf,
  viewOf,
} from "./briefing";
import { type Palette, panelKind, panelValence } from "./encode";
import { UNROOTED, type RootGroup, type Roots } from "./roots";

/** The briefing's own panel: the graph route keeps it open, as the design's `#side` starts. */
export const BRIEFING: PanelSpec = { id: "graph.briefing", name: "the briefing", side: "left" };

/** The arrow the design draws in its panel's header: one to hide it. */
const HIDE_GLYPH = (
  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
    <path
      d="M8 2L4 6l4 4M2 1.5v9"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

/** The pill that takes the panel's corner while it is hidden, the design's `#sShow`. */
export function BriefingShow({ panel }: { panel: Panel }) {
  return (
    <button
      type="button"
      className="proto-show"
      aria-label="Show the briefing"
      aria-controls="proto-briefing"
      onClick={() => panel.setCollapsed(false)}
    >
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M2 3h8M2 6h8M2 9h5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
      briefing
    </button>
  );
}

/** What the briefing panel says about the whole graph. */
export type HomeBriefing = {
  readonly asOf: Date;
  /** Records the field holds, and the verdicts their own votes moved. */
  readonly fresh: number;
  readonly broke: number;
  readonly brokeNow: number;
  readonly gained: number;
  readonly gainedNow: number;
  /** What the field holds, by what it is: the bars. */
  readonly cameIn: readonly { readonly label: string; readonly tint: string; readonly count: number }[];
  /** The claims the field has settled, newest proof first. */
  readonly learned: readonly { readonly cite: GraphNode; readonly verdict: string; readonly belief: GraphNode }[];
  readonly outOfDate: readonly Flagged[];
  readonly againstOdds: readonly Flagged[];
  readonly next: readonly NextExperiment[];
  readonly spend: Spend;
  /** The line of work that spent most, named by its root; null when nothing has spent. */
  readonly topSpend: { readonly title: string; readonly usd: number; readonly share: number } | null;
};

/** A belief the briefing flags, and how much load-bearing evidence stands against it. */
export type Flagged = { readonly belief: GraphNode; readonly against: number };

/** The two flags, which the panel offers as one open list at a time. */
export type SignalKey = "outOfDate" | "againstOdds";

/** Each flag as the panel says it: its key, the ring it wears, its count's words
 *  one and many, its why. */
const FLAGS: readonly [SignalKey, string, string, string, string][] = [
  ["outOfDate", "is-against", "verdict out of date", "verdicts out of date", "Counter-evidence landed, verdict unchanged"],
  ["againstOdds", "is-odd", "holds against the odds", "holding against the odds", "Standing despite heavy contrary context"],
];

/** The bars' tints, the prototype's. */
const CAME_IN: readonly { readonly label: string; readonly tint: string; readonly kinds: readonly string[] }[] = [
  { label: "Work", tint: "#6e8efb", kinds: ["Issue", "CodeChange", "AgentSession"] },
  { label: "Sources", tint: "#67d4e8", kinds: ["Paper", "WebSearch", "WebResult"] },
  { label: "Experiments", tint: "#b69cff", kinds: ["Experiment"] },
  { label: "Beliefs", tint: "#4cc9a0", kinds: ["Belief"] },
];

/** Read `graph` the way the prototype's briefing does, over all of it.
 *  `roots`, when the caller groups the canvas, names the thread the field spent most on. */
export function homeBriefing(graph: Graph, roots: Roots | null = null): HomeBriefing {
  const view = viewOf(graph);
  const asOf = graph.nodes.reduce((latest, node) => (node.created > latest ? node.created : latest), "");
  const asOfDate = asOf === "" ? new Date() : new Date(asOf);
  const fresh = graph.nodes.length;
  // What moved is read off the votes the field holds, as the design did it: a
  // belief took counter-evidence when a record proved against it. Its verdict
  // now — disproven, proven — is the parenthetical, so a graph that settled
  // nothing says so rather than repeating a tally from elsewhere.
  let [broke, brokeNow, gained, gainedNow] = [0, 0, 0, 0];
  for (const node of graph.nodes) {
    if (node.kind !== "Belief") continue;
    const cast = evidenceOf(node, view).filter(({ edge }) => edge.edge_kind === "proves");
    if (cast.some(({ edge }) => (edge.valence ?? 1) < 0)) {
      broke += 1;
      if (node.judgement === "disproven") brokeNow += 1;
    }
    if (cast.some(({ edge }) => (edge.valence ?? 1) >= 0)) {
      gained += 1;
      if (node.judgement === "proven") gainedNow += 1;
    }
  }
  const cameIn = CAME_IN.map(({ label, tint, kinds }) => ({
    label,
    tint,
    count: graph.nodes.filter((node) => kinds.includes(node.kind)).length,
  }));
  // What the field settled: a claim only counts as learned when a proof stands
  // behind its verdict, newest proof first, as the design ordered it.
  const learned = graph.nodes
    .filter((node) => node.kind === "Belief" && (node.judgement === "proven" || node.judgement === "disproven"))
    .flatMap((belief) => {
      const strongest = evidenceOf(belief, view)
        .filter(({ edge }) => edge.edge_kind === "proves")
        .sort((a, b) => Math.abs(b.edge.valence ?? 0) - Math.abs(a.edge.valence ?? 0))[0];
      return strongest ? [{ cite: strongest.node, verdict: belief.judgement!, belief }] : [];
    })
    .sort((a, b) => b.cite.created.localeCompare(a.cite.created))
    .slice(0, 4);
  // The two flags the design's briefing ends on: a verdict its own evidence has
  // overtaken, and a claim that keeps standing under contrary proof.
  const flagged = (heaviest: number) =>
    graph.nodes
      .filter((node) => node.kind === "Belief" && node.judgement !== "disproven")
      .map((belief) => ({ belief, against: loadOf(belief, view).against }))
      .filter((row) => row.against >= heaviest)
      .sort((a, b) => b.against - a.against || b.belief.created.localeCompare(a.belief.created));
  const all: RootGroup = { key: "*", root: null, members: graph.nodes.map((node) => node.id), newest: asOf, kinds: new Map() };
  const spend = spendOf(graph.nodes, view);
  // The heaviest thread: the design named the one root that spent most, and said
  // it as a share of the whole bill. Under no roots there is no thread to name,
  // so the note stays quiet rather than crediting the graph at large.
  const byThread = new Map<string, number>();
  for (const node of graph.nodes) {
    const home = roots?.home.get(node.id);
    if (!home || home === UNROOTED) continue;
    byThread.set(home, (byThread.get(home) ?? 0) + costOf(node));
  }
  const [heaviest, spent] = [...byThread].sort(([, a], [, b]) => b - a)[0] ?? [];
  const crown = heaviest === undefined ? undefined : view.byId.get(heaviest);
  const topSpend = crown && spent && spend.total > 0 ? { title: crown.title || "(untitled)", usd: spent, share: spent / spend.total } : null;
  return {
    asOf: asOfDate,
    fresh,
    broke,
    brokeNow,
    gained,
    gainedNow,
    cameIn,
    learned,
    outOfDate: flagged(1),
    againstOdds: flagged(2),
    next: nextExperiments(all, view, 3),
    spend,
    topSpend,
  };
}

/** The briefing panel: the whole graph the field draws. `palette` tints each
 *  ref with its kind's own hue, as the design's panels do. A row of `needs
 *  attention` picked calls `onSignal` with the beliefs it counts; `signal` names
 *  the one standing open. `roots` is the canvas's grouping, which lets `spend`
 *  name the thread it fell on. Hidden, it slides past the viewport's edge and
 *  leaves `BriefingShow` in its corner; the canvas takes the width back. */
export function Briefing({
  graph,
  palette,
  panel,
  roots,
  onPick,
  onSignal,
  signal,
}: {
  graph: Graph;
  palette: Palette;
  panel: Panel;
  roots?: Roots | null;
  onPick: (node: GraphNode) => void;
  onSignal: (key: SignalKey, ids: readonly string[]) => void;
  signal: SignalKey | null;
}) {
  const brief = useMemo(() => homeBriefing(graph, roots ?? null), [graph, roots]);
  const peak = Math.max(1, ...brief.cameIn.map((row) => row.count));
  // A ref in prose is small text: the canvas's hue lifted a step, which is how
  // the design colours a `Kind#seq` in its panels.
  const tint = (kind: string) => ({ "--c": panelKind(palette, kind) } as CSSProperties);
  const verbInk = (judgement: string) =>
    ({ "--c": panelValence(judgement === "disproven" ? palette.against : palette.support) } as CSSProperties);
  return (
    <motion.aside
      id="proto-briefing"
      className={panel.collapsed ? "proto-briefing is-hidden" : "proto-briefing"}
      inert={panel.collapsed}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.24, ease: "easeOut" }}
    >
      <header>
        <span>briefing</span>
        <button
          type="button"
          className="proto-hide"
          aria-label="Hide panel"
          aria-expanded="true"
          aria-controls="proto-briefing"
          onClick={() => panel.setCollapsed(true)}
        >
          {HIDE_GLYPH}
        </button>
      </header>
      <div className="proto-briefing-body">
        <div className="proto-asof">
          <span>as of {brief.asOf.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span>
        </div>
        <p className="proto-summary">
          {/* The design's three sentences, each read over the field: what it holds, what
              its own votes settled, and how many verdicts have fallen behind. The middle
              two go quiet when the graph holds neither event nor flag. */}
          {brief.fresh === 0 ? (
            <>Nothing recorded yet. </>
          ) : (
            <>
              <b>{brief.fresh.toLocaleString("en-US")}</b> records in view.{" "}
            </>
          )}
          {brief.broke > 0 || brief.gained > 0 ? (
            <>
              <b>{brief.broke}</b> belief{brief.broke === 1 ? "" : "s"} took counter-evidence
              {brief.brokeNow > 0 ? ` (${brief.brokeNow} now disproven)` : ""} and <b>{brief.gained}</b> gained support
              {brief.gainedNow > 0 ? ` (${brief.gainedNow} now proven)` : ""}.{" "}
            </>
          ) : null}
          {brief.outOfDate.length > 0 ? (
            <>
              <b>{brief.outOfDate.length}</b> still need{brief.outOfDate.length === 1 ? "s" : ""} a verdict update.
            </>
          ) : null}
        </p>
        <h3>came in</h3>
        <ul className="proto-camein">
          {brief.cameIn.map((row) => (
            <li key={row.label}>
              <i style={{ background: row.tint }} />
              <span className="proto-camein-label">{row.label}</span>
              <span className="proto-bar">
                <motion.i
                  style={{ background: row.tint }}
                  initial={{ scaleX: 0 }}
                  animate={{ scaleX: row.count / peak }}
                  transition={{ duration: 0.5, ease: "easeOut" }}
                />
              </span>
              <span className="proto-delta num">+{row.count}</span>
            </li>
          ))}
        </ul>
        <h3>learned</h3>
        <ul className="proto-learned">
          {brief.learned.map(({ cite, verdict, belief }) => (
            <li key={belief.id}>
              <button type="button" className="proto-chip" style={tint(cite.kind)} onClick={() => onPick(cite)}>
                {cite.kind}#{cite.seq}
              </button>
              <span className={`proto-verdict ${verdict}`} style={verbInk(verdict)}>
                {verdict}
              </span>
              <button type="button" className="proto-chip" style={tint(belief.kind)} onClick={() => onPick(belief)}>
                {belief.kind}#{belief.seq}
              </button>
              <div className="proto-learned-line">{belief.title || "(untitled)"}</div>
            </li>
          ))}
        </ul>
        <h3>needs attention</h3>
        {/* Each row is a question the canvas can answer: picked, it lights the beliefs
            it counts and fits to them, then names them here. */}
        <ul className="proto-attention">
          {FLAGS.map(([key, tone, one, many, why]) => {
            const rows = brief[key];
            const on = signal === key;
            return (
              <li key={key}>
                <button
                  type="button"
                  className={`proto-att${on ? " is-on" : ""}`}
                  disabled={rows.length === 0}
                  onClick={() => onSignal(key, rows.map(({ belief }) => belief.id))}
                >
                  <i className={`proto-ring ${tone}`} />
                  <span>
                    <b>
                      {rows.length} {rows.length === 1 ? one : many}
                    </b>
                    <span>{why}</span>
                  </span>
                  <em className="proto-att-hint num">{rows.length === 0 ? "none" : on ? "shown" : "show on the graph"}</em>
                </button>
                {on ? (
                  <ul className="proto-flagged">
                    {rows.map(({ belief, against }) => (
                      <li key={belief.id}>
                        <button type="button" className="proto-flag" onClick={() => onPick(belief)}>
                          <span className="proto-chip" style={tint(belief.kind)}>
                            {belief.kind}#{belief.seq}
                          </span>
                          <span className="proto-flag-title">{belief.title || "(untitled)"}</span>
                          <span className="proto-flag-side num">{against} against</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
        <h3>
          best next experiments <em>value per $</em>
        </h3>
        <p className="proto-next-why">Ranked by how undecided a claim is, what rides on it, and what one test costs. Not random: the same data always gives the same list.</p>
        <ul className="proto-next">
          {brief.next.map((row) => (
            <li key={row.belief.id}>
              <button type="button" onClick={() => onPick(row.belief)}>
                {row.rerun ? "Re-run" : "Adapt"}{" "}
                {row.template ? (
                  <span className="proto-chip inline" style={tint(row.template.kind)}>
                    {row.template.kind}#{row.template.seq}
                  </span>
                ) : (
                  "a fresh test"
                )}{" "}
                for{" "}
                <span className="proto-chip inline" style={tint(row.belief.kind)}>
                  {row.belief.kind}#{row.belief.seq}
                </span>
              </button>
              <span className="proto-next-line">{row.belief.title || "(untitled)"}</span>
              {/* The row's receipts, as the design's third line: what the claim is worth
                  settling and what one test in the thread costs to settle it. */}
              <span className="proto-next-meta num">
                <span>conf {(row.belief.confidence ?? 0.5).toFixed(2)}</span>
                <span>{row.evidence} evidence</span>
                {row.leaning > 0 ? <span>{row.leaning} lean on it</span> : null}
                <span>{row.price.priced ? `~${usd(row.price.usd)} per test` : "no test priced yet"}</span>
              </span>
            </li>
          ))}
        </ul>
        <h3>spend</h3>
        <div className="proto-spend">
          <div>
            <b className="num">{usd(brief.spend.total)}</b>
            <small>total</small>
          </div>
          <div>
            <b className="num">{usd(brief.spend.resource)}</b>
            <small>compute</small>
          </div>
          <div>
            <b className="num">{usd(brief.spend.agent)}</b>
            <small>agents</small>
          </div>
          <div>
            <b className="num">{brief.spend.perSettled === null ? "–" : usd(brief.spend.perSettled)}</b>
            <small>per verdict settled</small>
          </div>
        </div>
        {brief.topSpend ? (
          <p className="proto-spend-note">
            Most spent on <em>{brief.topSpend.title}</em> ({usd(brief.topSpend.usd)},{" "}
            {Math.round(brief.topSpend.share * 100)}%).
          </p>
        ) : null}
      </div>
    </motion.aside>
  );
}

/** The timeline's bars: `bins` counts of nodes by when they were made. */
export function binsOf(graph: Graph, bins = 72): number[] {
  if (graph.nodes.length === 0) return new Array(bins).fill(0);
  const times = graph.nodes.map((node) => Date.parse(node.created));
  const [first, last] = [Math.min(...times), Math.max(...times)];
  const span = Math.max(1, last - first);
  const out = new Array(bins).fill(0);
  for (const time of times) out[Math.min(bins - 1, Math.floor(((time - first) / span) * bins))] += 1;
  return out;
}

/**
 * The foot of the graph: play, the sparkline of when the work was made, a scrub
 * dot along it and the date the view stands at. Scrubbing draws the graph as it
 * was at that moment; playing grows it from there.
 */
export function Timeline({
  bins,
  position,
  playing,
  date,
  grown,
  onScrub,
  onToggle,
}: {
  bins: readonly number[];
  /** How far through the graph's life the view stands, 0 to 1. */
  position: number;
  playing: boolean;
  date: string;
  /** How many of the graph's records the stand has reached, or null at its end. */
  grown: number | null;
  onScrub: (fraction: number) => void;
  onToggle: () => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const peak = Math.max(1, ...bins);
  const scrubTo = (clientX: number) => {
    const box = track.current?.getBoundingClientRect();
    if (!box) return;
    onScrub(Math.min(1, Math.max(0, (clientX - box.left) / box.width)));
  };
  return (
    <div className="proto-timeline">
      <button type="button" className="proto-play" onClick={onToggle} aria-label={playing ? "Pause the replay" : "Replay the graph's growth"}>
        <span className="proto-play-glyph">{playing ? "❚❚" : "▶"}</span>
      </button>
      <div
        className="proto-track"
        ref={track}
        role="slider"
        aria-label="Replay position"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(position * 100)}
        tabIndex={0}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          scrubTo(event.clientX);
        }}
        onPointerMove={(event) => {
          if (event.buttons === 1) scrubTo(event.clientX);
        }}
      >
        {bins.map((count, index) => (
          <i key={index} style={{ height: `${(count / peak) * 100}%`, opacity: index / bins.length <= position ? 0.85 : 0.3 }} />
        ))}
        <motion.span
          className="proto-scrub"
          animate={{ left: `${position * 100}%` }}
          transition={{ type: "spring", stiffness: 400, damping: 34 }}
        />
      </div>
      <span className="proto-date num">
        {date}
        {grown === null ? null : `, ${grown.toLocaleString()} nodes`}
      </span>
    </div>
  );
}

/** A number eased toward `target` on screen, so counts and bars move rather than jump. */
export function useEased(target: number, ms = 400): number {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const controls = animate(from.current, target, {
      duration: ms / 1000,
      onUpdate: (value) => setShown(value),
      onComplete: () => {
        from.current = target;
      },
    });
    return () => controls.stop();
  }, [target, ms]);
  return shown;
}

/** The graph's life, as a `GraphView`, for panels that read neighbours. */
export { viewOf, type GraphView };
