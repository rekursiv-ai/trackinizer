// The graph's own chrome, as the design prototype draws it: a briefing panel at
// the left that reads the whole graph, a timeline across the foot that replays
// it, and a legend that folds to a pill at the right. The canvas between them is
// the prototype's; these sit over it and never take its width.

import { animate, motion } from "framer-motion";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import type { Graph, GraphNode } from "../api/graph";
import { type GraphView, type NextExperiment, evidenceOf, loadOf, nextExperiments, viewOf } from "./briefing";
import { type Palette, panelKind, panelValence } from "./encode";
import type { RootGroup } from "./roots";

/** The windows the briefing can read the graph over. */
export type Window = 7 | 30 | 0;

/** What the briefing panel says about the whole graph. */
export type HomeBriefing = {
  readonly asOf: Date;
  /** New records in the window, and the verdicts its own votes moved. */
  readonly fresh: number;
  readonly broke: number;
  readonly brokeNow: number;
  readonly gained: number;
  readonly gainedNow: number;
  /** What arrived in the window, by what it is: the bars. */
  readonly cameIn: readonly { readonly label: string; readonly tint: string; readonly count: number }[];
  /** The claims the window settled, newest first, with the record that settled each. */
  readonly learned: readonly { readonly cite: GraphNode; readonly verdict: string; readonly belief: GraphNode }[];
  readonly outOfDate: readonly Flagged[];
  readonly againstOdds: readonly Flagged[];
  readonly next: readonly NextExperiment[];
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

/** Read `graph` the way the prototype's briefing does, over the last `days` (0 for all of it). */
export function homeBriefing(graph: Graph, days: Window): HomeBriefing {
  const view = viewOf(graph);
  const asOf = graph.nodes.reduce((latest, node) => (node.created > latest ? node.created : latest), "");
  const asOfDate = asOf === "" ? new Date() : new Date(asOf);
  const from = days === 0 ? "" : new Date(asOfDate.getTime() - days * DAY_MS).toISOString();
  const inWindow = (node: GraphNode) => node.created >= from;
  const fresh = graph.nodes.filter(inWindow).length;
  // What moved is read off the votes that arrived, as the design did it: a belief
  // took counter-evidence when a record the window holds proved against it. Its
  // verdict now — disproven, proven — is the parenthetical, so a window that
  // settled nothing says so rather than repeating the whole store's tally.
  let [broke, brokeNow, gained, gainedNow] = [0, 0, 0, 0];
  for (const node of graph.nodes) {
    if (node.kind !== "Belief") continue;
    const cast = evidenceOf(node, view).filter(({ node: cite, edge }) => edge.edge_kind === "proves" && inWindow(cite));
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
    count: graph.nodes.filter((node) => kinds.includes(node.kind) && inWindow(node)).length,
  }));
  // What the window settled: a claim only counts as learned when the proof that
  // settled it arrived in the window, newest proof first, as the design ordered it.
  const learned = graph.nodes
    .filter((node) => node.kind === "Belief" && (node.judgement === "proven" || node.judgement === "disproven"))
    .flatMap((belief) => {
      const strongest = evidenceOf(belief, view)
        .filter(({ node: cite, edge }) => edge.edge_kind === "proves" && inWindow(cite))
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
  };
}

const DAY_MS = 86_400_000;

/** The briefing panel: the whole graph, read over a window. `palette` tints each
 *  ref with its kind's own hue, as the design's panels do. A row of `needs
 *  attention` picked calls `onSignal` with the beliefs it counts; `signal` names
 *  the one standing open. */
export function Briefing({
  graph,
  palette,
  onPick,
  onSignal,
  signal,
}: {
  graph: Graph;
  palette: Palette;
  onPick: (node: GraphNode) => void;
  onSignal: (key: SignalKey, ids: readonly string[]) => void;
  signal: SignalKey | null;
}) {
  const [days, setDays] = useState<Window>(7);
  const brief = useMemo(() => homeBriefing(graph, days), [graph, days]);
  const peak = Math.max(1, ...brief.cameIn.map((row) => row.count));
  // A ref in prose is small text: the canvas's hue lifted a step, which is how
  // the design colours a `Kind#seq` in its panels.
  const tint = (kind: string) => ({ "--c": panelKind(palette, kind) } as CSSProperties);
  const verbInk = (judgement: string) =>
    ({ "--c": panelValence(judgement === "disproven" ? palette.against : palette.support) } as CSSProperties);
  return (
    <motion.aside
      className="proto-briefing"
      initial={{ x: -16, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ type: "spring", stiffness: 320, damping: 30 }}
    >
      <header>
        <span>briefing</span>
      </header>
      <div className="proto-briefing-body">
        <div className="proto-asof">
          <span>as of {brief.asOf.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span>
          <span className="proto-windows">
            {([7, 30, 0] as Window[]).map((span) => (
              <button key={span} type="button" className={span === days ? "on" : ""} onClick={() => setDays(span)}>
                {span === 0 ? "all" : `${span}d`}
              </button>
            ))}
          </span>
        </div>
        <p className="proto-summary">
          {/* The design's three sentences, each read over the window: what arrived, what
              its own votes settled, and how many verdicts have fallen behind. The middle
              two go quiet when the window holds neither event nor flag. */}
          {brief.fresh === 0 ? (
            <>Nothing new {days === 0 ? "yet" : `in the last ${days} days`}. </>
          ) : days === 0 ? (
            <>
              <b>{brief.fresh}</b> records in total.{" "}
            </>
          ) : (
            <>
              <b>{brief.fresh}</b> new records in the last {days} days.{" "}
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
            </li>
          ))}
        </ul>
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
  onScrub,
  onToggle,
}: {
  bins: readonly number[];
  /** How far through the graph's life the view stands, 0 to 1. */
  position: number;
  playing: boolean;
  date: string;
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
      <span className="proto-date num">{date}</span>
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
