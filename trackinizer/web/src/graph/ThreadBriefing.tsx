import { dateTime, useMinuteClock } from "../detail/time";
import { shortAge } from "../lists/Row";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { StatusGlyph } from "../ui/glyphs";
import type { Graph, GraphNode } from "../api/graph";
import {
  type GraphView,
  membersOf,
  type NextExperiment,
  type Turn,
  turnsOf,
  verdictsOf,
  viewOf,
  reliedOn,
  nextExperiments,
} from "./briefing";
import type { RootGroup } from "./roots";
import "./briefing.css";

/**
 * A cluster's briefing: the thread's root question, how its claims stand, the
 * story of how they got there, the beliefs most relied on, and the experiments
 * best made next. A node picked here opens Peek on it.
 *
 * It draws above the root's own detail in Peek, so that picking a root — in the
 * roots list, by its island's name, or on the canvas — reads its thread where
 * every other record is read, and the roots list stays where it was. `onBack`
 * heads it with a way out, for a caller that shows it in place of something else.
 */
export function ThreadBriefing({
  group,
  graph,
  onBack,
  onSelectNode,
}: {
  group: RootGroup;
  graph: Graph;
  onBack?: () => void;
  onSelectNode: (node: GraphNode) => void;
}) {
  const view: GraphView = viewOf(graph);
  const now = useMinuteClock();
  const members = membersOf(group, view);
  const root = group.root;
  const verdicts = verdictsOf(members);
  const claims = members.filter((node) => node.kind === "Belief").length;
  const experiments = members.filter((node) => node.kind === "Experiment").length;
  const sources = members.filter((node) => node.kind === "Paper" || node.kind === "WebResult" || node.kind === "WebSearch").length;
  const turns = turnsOf(group, view);
  const relied = reliedOn(members, view);
  const next = nextExperiments(group, view);
  const held = relied.filter(({ belief }) => belief.judgement === "proven");
  const broken = relied.filter(({ belief }) => belief.judgement === "disproven");
  return (
    <aside className="briefing" aria-label="Cluster briefing">
      {onBack ? (
        <div className="briefing-head">
          <button type="button" className="icon-btn" aria-label="Back to the roots list" onClick={onBack}>
            <Icon name="arrowL" size={14} />
          </button>
          <span className="briefing-crumb muted">briefing</span>
        </div>
      ) : null}
      {root ? (
        <div className="briefing-hero">
          <div className="briefing-ref mono">
            <KindIcon kind={root.kind} size={13} />
            {root.kind}#{root.seq}
            <StatusGlyph status={root.status} size={13} />
          </div>
          <h2 className="briefing-title">{root.title || "(untitled)"}</h2>
          <p className="briefing-sub muted num">
            {members.length.toLocaleString("en-US")} records · {shortAge(group.newest, now)}
          </p>
          <Verdicts verdicts={verdicts} claims={claims} experiments={experiments} sources={sources} />
        </div>
      ) : (
        <div className="briefing-hero">
          <h2 className="briefing-title">Unrooted</h2>
          <p className="briefing-sub muted num">{members.length.toLocaleString("en-US")} records under no root</p>
        </div>
      )}
      {root && (held.length > 0 || broken.length > 0) ? (
        <section className="briefing-sec">
          <h5>
            summary
          </h5>
          <p className="briefing-prose">
            This thread set out to {lower(root.title.replace(/[.?]$/, ""))}.{" "}
            {held[0] ? (
              <>
                So far the evidence shows <NodePill node={held[0].belief} onSelect={onSelectNode} /> {held[0].belief.title};{" "}
              </>
            ) : (
              "Nothing is proven yet. "
            )}
            {broken[0] ? (
              <>
                The evidence rules out <NodePill node={broken[0].belief} onSelect={onSelectNode} /> {broken[0].belief.title}.{" "}
              </>
            ) : (
              "Nothing has been ruled out yet. "
            )}
            Of {claims} claim{claims === 1 ? "" : "s"}, {verdicts.proven} proven, {verdicts.disproven} disproven and{" "}
            {verdicts.unproven} still open, from {experiments} experiment{experiments === 1 ? "" : "s"} and {sources} source
            {sources === 1 ? "" : "s"}.
          </p>
        </section>
      ) : null}
      {turns.length > 0 ? (
        <section className="briefing-sec">
          <h5>turning points</h5>
          <ol className="briefing-turns">
            {turns.map((turn, index) => (
              <TurnRow key={index} turn={turn} view={view} now={now} onSelect={onSelectNode} />
            ))}
          </ol>
        </section>
      ) : null}
      {next.length > 0 ? (
        <section className="briefing-sec">
          <h5>best next experiments</h5>
          {next.map((entry) => (
            <NextRow key={entry.belief.id} entry={entry} onSelect={onSelectNode} />
          ))}
        </section>
      ) : null}
      {relied.length > 0 ? (
        <section className="briefing-sec">
          <h5>
            most relied-on beliefs<span className="briefing-sec-side">evidence</span>
          </h5>
          {relied.map(({ belief, evidence }) => (
            <button key={belief.id} type="button" className="briefing-row" onClick={() => onSelectNode(belief)}>
              <BeliefDot belief={belief} />
              <span className="briefing-row-ref mono">{belief.kind}#{belief.seq}</span>
              <span className="briefing-row-title">{belief.title || "(untitled)"}</span>
              <span className="briefing-row-side num">{evidence}</span>
            </button>
          ))}
        </section>
      ) : null}
    </aside>
  );
}

/** The thread's verdict bar, and what its three spans and the counts beside them read. */
function Verdicts({
  verdicts,
  claims,
  experiments,
  sources,
}: {
  verdicts: { readonly proven: number; readonly unproven: number; readonly disproven: number };
  claims: number;
  experiments: number;
  sources: number;
}) {
  const total = Math.max(1, verdicts.proven + verdicts.unproven + verdicts.disproven);
  return (
    <div className="briefing-verdicts">
      <div
        className="briefing-vbar"
        role="img"
        aria-label={`${verdicts.proven} proven, ${verdicts.unproven} unproven, ${verdicts.disproven} disproven`}
      >
        <span className="is-proven" style={{ width: `${(verdicts.proven / total) * 100}%` }} />
        <span className="is-unproven" style={{ width: `${(verdicts.unproven / total) * 100}%` }} />
        <span className="is-disproven" style={{ width: `${(verdicts.disproven / total) * 100}%` }} />
      </div>
      <div className="briefing-vkey mono">
        <span>
          <i className="is-proven" /> {verdicts.proven} proven
        </span>
        <span>
          <i className="is-unproven" /> {verdicts.unproven} unproven
        </span>
        <span>
          <i className="is-disproven" /> {verdicts.disproven} disproven
        </span>
        <span className="briefing-vkey-side">
          {experiments} experiment{experiments === 1 ? "" : "s"} · {sources} source{sources === 1 ? "" : "s"} · {claims} claim
          {claims === 1 ? "" : "s"}
        </span>
      </div>
    </div>
  );
}

/** One turning point: its date, what happened, the records it named. */
function TurnRow({
  turn,
  view,
  now,
  onSelect,
}: {
  turn: Turn;
  view: GraphView;
  now: number;
  onSelect: (node: GraphNode) => void;
}) {
  const named = turn.refs.map((id) => view.byId.get(id)).filter((node) => node !== undefined);
  return (
    <li className="briefing-turn">
      <span className="briefing-turn-date mono" title={dateTime(turn.created)}>
        {shortAge(turn.created, now)}
      </span>
      <span className="briefing-turn-what">
        {turn.say === "opened" ? "opened" : turn.say === "broke" ? "broke" : "proved"}
        {named.map((node) => (
          <NodePill key={node.id} node={node} onSelect={onSelect} />
        ))}
        {named[1] ? <span className="briefing-turn-title">{named[1].title}</span> : null}
      </span>
    </li>
  );
}

/** One next experiment: what to run, to settle what, and what it would take. */
function NextRow({ entry, onSelect }: { entry: NextExperiment; onSelect: (node: GraphNode) => void }) {
  const { belief, template, rerun } = entry;
  return (
    <div className="briefing-next">
      <div className="briefing-next-run">
        {template ? (
          <>
            {rerun ? "Re-run" : "Adapt"} <NodePill node={template} onSelect={onSelect} />{" "}
            <span className="briefing-next-template">{template.title}</span>
            {rerun ? " with a new seed" : " for this claim"}
          </>
        ) : (
          "Design a first experiment"
        )}
      </div>
      <div className="briefing-next-settle">
        to settle <NodePill node={belief} onSelect={onSelect} /> {belief.title}
      </div>
      <div className="briefing-next-meta mono">
        <span>conf {(belief.confidence ?? 0.5).toFixed(2)}</span>
        <span>{entry.evidence} evidence</span>
        {entry.leaning > 0 ? <span>{entry.leaning} lean on it</span> : null}
      </div>
    </div>
  );
}

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

function lower(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}
