import { KindIcon } from "../ui/kinds";
import type { Graph, GraphNode } from "../api/graph";
import { chainOf, evidenceOf, impactOf, loadOf, nextExperiments, type GraphView, viewOf } from "./briefing";
import "./briefing.css";

/**
 * What the graph knows about the selected node, above Peek's detail: the chain
 * of `produced_by` it came down, its evidence mix and what leans on it were it
 * wrong, and, for an open claim, the experiment best made next. The same reads
 * the design prototype's record view gave, on the app's own data.
 */
export function NodeInsights({ node, graph, onSelectNode }: { node: GraphNode; graph: Graph; onSelectNode: (node: GraphNode) => void }) {
  const view: GraphView = viewOf(graph);
  const chain = chainOf(node, view);
  const belief = node.kind === "Belief";
  const load = belief ? loadOf(node, view) : null;
  const evidence = belief ? evidenceOf(node, view) : [];
  const mix = belief ? byRole(evidence.map(({ node: other }) => other)) : null;
  const impact = impactOf(node, view);
  // The claim alone as a one-member thread: the next experiment is what would
  // settle this belief — re-run the strongest experiment citing it, design one.
  const next = belief
    ? nextExperiments({ root: null, key: node.id, members: [node.id], newest: node.created, kinds: new Map() }, view)
    : [];
  return (
    <div className="node-insights">
      {chain.length > 0 ? (
        <section className="briefing-sec">
          <h5>how it got here</h5>
          <ol className="node-chain">
            {chain.map((step, index) => (
              <li key={step.id} className="node-chain-step">
                {index > 0 ? <span className="node-chain-verb mono">produced</span> : null}
                <button type="button" className="briefing-pill mono" onClick={() => onSelectNode(step)}>
                  <KindIcon kind={step.kind} size={11} />
                  {step.kind}#{step.seq}
                </button>
                <span className="node-chain-title">{step.title || "(untitled)"}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
      {belief && mix ? (
        <section className="briefing-sec">
          <h5>evidence mix</h5>
          <p className="node-mix">
            {ROLE_LABELS.map(([role, label, pluralLabel]) =>
              mix[role] > 0 ? (
                <span key={role} className="node-mix-item">
                  <i className={`node-mix-dot is-${role}`} />
                  {mix[role]} {mix[role] === 1 ? label : pluralLabel}
                </span>
              ) : null,
            )}
          </p>
          <p className="node-mix-sub mono">
            <span>{load?.for ?? 0} load-bearing for</span>
            <span className={load && load.against > 0 ? "is-against" : undefined}>{load?.against ?? 0} load-bearing against</span>
          </p>
        </section>
      ) : null}
      {impact.length > 0 ? (
        <section className="briefing-sec">
          <h5>
            if this were wrong<span className="briefing-sec-side num">{impactOf(node, view, 1_000).length}</span>
          </h5>
          {impact.map(({ belief: other, hops }) => (
            <button key={other.id} type="button" className="briefing-row" onClick={() => onSelectNode(other)}>
              <BeliefDot belief={other} />
              <span className="briefing-row-ref mono">{other.kind}#{other.seq}</span>
              <span className="briefing-row-title">{other.title || "(untitled)"}</span>
              <span className="briefing-row-side mono">{hops === 1 ? "direct" : `${hops - 1} step${hops === 2 ? "" : "s"} away`}</span>
            </button>
          ))}
        </section>
      ) : null}
      {next[0] ? (
        <section className="briefing-sec">
          <h5>best next experiment</h5>
          <p className="node-next">
            {next[0].template ? (
              <>
                {next[0].rerun ? "Re-run" : "Adapt"} <NodePill node={next[0].template} onSelect={onSelectNode} /> to settle{" "}
                <NodePill node={next[0].belief} onSelect={onSelectNode} />
              </>
            ) : (
              <>
                Design a first experiment to settle <NodePill node={next[0].belief} onSelect={onSelectNode} />
              </>
            )}
          </p>
        </section>
      ) : null}
    </div>
  );
}

/** An evidence row's roles, the way the design grouped them. */
const ROLE_LABELS = [
  ["exp", "experiment", "experiments"],
  ["source", "paper or web result", "papers or web results"],
  ["belief", "other belief", "other beliefs"],
  ["work", "work record", "work records"],
] as const;

/** `nodes` counted by the role its kind plays: work, source, experiment or belief. */
function byRole(nodes: readonly GraphNode[]): { [role: string]: number } {
  const mix: { [role: string]: number } = { exp: 0, source: 0, belief: 0, work: 0 };
  for (const node of nodes) {
    const role = ROLE_OF[node.kind] ?? "work";
    mix[role] += 1;
  }
  return mix;
}

const ROLE_OF: { [kind: string]: string } = {
  Experiment: "exp",
  CodeChange: "exp",
  Artifact: "exp",
  Paper: "source",
  WebResult: "source",
  WebSearch: "source",
  Belief: "belief",
  Issue: "work",
  AgentSession: "work",
};

/** A record's pill: its kind's glyph and `Kind#seq`, a click selecting it. */
function NodePill({ node, onSelect }: { node: GraphNode; onSelect: (node: GraphNode) => void }) {
  return (
    <button type="button" className="briefing-pill mono" onClick={() => onSelect(node)}>
      <KindIcon kind={node.kind} size={11} />
      {node.kind}#{node.seq}
    </button>
  );
}

/** A belief's verdict dot: filled when proven, hollow when disproven, dimmed when open. */
function BeliefDot({ belief }: { belief: GraphNode }) {
  return <span className={`briefing-dot ${belief.judgement === "disproven" ? "is-hollow" : belief.judgement === "proven" ? "is-proven" : "is-open"}`} />;
}
