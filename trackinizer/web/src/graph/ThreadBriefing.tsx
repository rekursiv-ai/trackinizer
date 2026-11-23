import { type CSSProperties, useState } from "react";
import { usd } from "../detail/fields";
import { dateTime, useMinuteClock } from "../detail/time";
import { formatRoute } from "../router/route";
import { shortAge } from "../lists/Row";
import { micros } from "../live/rows";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { StatusGlyph } from "../ui/glyphs";
import type { Graph, GraphNode } from "../api/graph";
import { type Palette, panelKind } from "./encode";
import { RecordProse, RecordTail } from "./GraphRecord";
import {
  type GraphView,
  membersOf,
  type NextExperiment,
  type Turn,
  turnsOf,
  loadOf,
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
  palette,
  onBack,
  onSelectNode,
}: {
  group: RootGroup;
  graph: Graph;
  palette: Palette;
  onBack?: () => void;
  onSelectNode: (node: GraphNode) => void;
}) {
  const view: GraphView = viewOf(graph);
  const now = useMinuteClock();
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (text: string, which: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // A clipboard the browser will not hand out (an insecure context): the buttons still read.
    }
    setCopied(which);
    setTimeout(() => setCopied(null), 1600);
  };
  const members = membersOf(group, view);
  const root = group.root;
  const verdicts = verdictsOf(members);
  const claims = members.filter((node) => node.kind === "Belief").length;
  const experiments = members.filter((node) => node.kind === "Experiment").length;
  const sources = members.filter((node) => node.kind === "Paper" || node.kind === "WebResult" || node.kind === "WebSearch").length;
  const turns = turnsOf(group, view);
  const relied = reliedOn(members, view);
  const next = nextExperiments(group, view);
  // The two flags the field's briefing ends on, asked of this thread alone: a
  // verdict its own evidence has overtaken, and a claim still standing under
  // heavy contrary proof. Across the whole graph they say how much is unsettled;
  // asked of one thread they say which of ITS claims to look at, which is the
  // question a reader has already narrowed to by opening this.
  const attention = members
    .filter((node) => node.kind === "Belief" && node.judgement !== "disproven")
    .map((belief) => ({ belief, against: loadOf(belief, view).against }))
    .filter((row) => row.against >= 1)
    .sort((a, b) => b.against - a.against || micros(b.belief.created) - micros(a.belief.created))
    .slice(0, 4);
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
            <span className="briefing-ref-kind" style={{ color: panelKind(palette, root.kind) }}>
              <KindIcon kind={root.kind} size={13} />
              {root.kind}#{root.seq}
            </span>
            <StatusGlyph status={root.status} size={13} />
          </div>
          <h2 className="briefing-title">{root.title || "(untitled)"}</h2>
          <p className="briefing-sub muted num">
            {members.length.toLocaleString("en-US")} records · {shortAge(group.newest, now)}
          </p>
          <Verdicts verdicts={verdicts} claims={claims} experiments={experiments} sources={sources} />
          <div className="briefing-actions">
            <button
              type="button"
              className="btn ghost mono"
              onClick={() => void copy(writeUp(group, members, verdicts, turns, view, now), "copy")}
            >
              {copied === "copy" ? "copied" : "copy"}
            </button>
            {/* The chat is a real place, not a prompt to paste: the root's page is where
                the canvas pairs the thread with a live agent session. A new tab leaves the
                graph framed as it was. */}
            <a
              className="btn ghost mono"
              href={root ? formatRoute({ name: "ref", kind: root.kind, seq: root.seq }) : "#"}
              target="_blank"
              rel="noreferrer"
              title="Chat about this thread with the agent, on its record's page"
            >
              chat
            </a>
          </div>
        </div>
      ) : (
        <div className="briefing-hero">
          <h2 className="briefing-title">Unrooted</h2>
          <p className="briefing-sub muted num">{members.length.toLocaleString("en-US")} records under no root</p>
        </div>
      )}
      {/* The root's own summary, under the thread's heading: the row's prose,
          which a person or an agent writes here, and not a sentence the panel
          writes for it. `.record` scopes it to the panel's rules for a record's
          own text, without that panel's box. */}
      {root ? (
        <div className="record record-brief">
          <RecordProse node={root} />
        </div>
      ) : null}
      {turns.length > 0 ? (
        <section className="briefing-sec">
          <h5>turning points</h5>
          <ol className="briefing-turns">
            {turns.map((turn, index) => (
              <TurnRow key={index} turn={turn} view={view} palette={palette} now={now} onSelect={onSelectNode} />
            ))}
          </ol>
        </section>
      ) : null}
      {next.length > 0 ? (
        <section className="briefing-sec">
          <h5>best next experiments</h5>
          {next.map((entry) => (
            <NextRow key={entry.belief.id} entry={entry} palette={palette} onSelect={onSelectNode} />
          ))}
        </section>
      ) : null}
      {attention.length > 0 ? (
        <section className="briefing-sec">
          <h5>
            needs attention<span className="briefing-sec-side">against</span>
          </h5>
          {attention.map(({ belief, against }) => (
            <button key={belief.id} type="button" className="briefing-row" onClick={() => onSelectNode(belief)}>
              {/* Two counts of contrary proof is the field briefing's own bar for a
                  claim holding against the odds; one is a verdict gone stale. */}
              <span className={`proto-ring ${against >= 2 ? "is-odd" : "is-against"}`} aria-hidden="true" />
              <span className="briefing-row-ref mono" style={{ color: panelKind(palette, belief.kind) }}>
                {belief.kind}#{belief.seq}
              </span>
              <span className="briefing-row-title">{belief.title || "(untitled)"}</span>
              <span className="briefing-row-side num">{against}</span>
            </button>
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
              <span className="briefing-row-ref mono" style={{ color: panelKind(palette, belief.kind) }}>
                {belief.kind}#{belief.seq}
              </span>
              <span className="briefing-row-title">{belief.title || "(untitled)"}</span>
              <span className="briefing-row-side num">{evidence}</span>
            </button>
          ))}
        </section>
      ) : null}
      {/* And the root as a record: its parents and children, with the rail's
          "+ add" on each side, the changes on it, and every field it has. A
          thread's briefing answers what the thread concluded; this answers where
          the question itself sits, and it is the same read every other record
          gets, not a second panel stacked on this one. */}
      {root ? (
        <div className="record record-brief">
          <RecordTail node={root} graph={graph} palette={palette} onSelectNode={onSelectNode} />
        </div>
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
  palette,
  now,
  onSelect,
}: {
  turn: Turn;
  view: GraphView;
  palette: Palette;
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
          <NodePill key={node.id} node={node} palette={palette} onSelect={onSelect} />
        ))}
        {named[1] ? <span className="briefing-turn-title">{named[1].title}</span> : null}
      </span>
    </li>
  );
}

/** One next experiment: what to run, to settle what, and what it would take. */
function NextRow({ entry, palette, onSelect }: { entry: NextExperiment; palette: Palette; onSelect: (node: GraphNode) => void }) {
  const { belief, template, rerun } = entry;
  return (
    <div className="briefing-next">
      <div className="briefing-next-run">
        {template ? (
          <>
            {rerun ? "Re-run" : "Adapt"} <NodePill node={template} palette={palette} onSelect={onSelect} />{" "}
            <span className="briefing-next-template" title={template.title}>
              {trunc(template.title, 26)}
            </span>
            {rerun ? " with a new seed" : " for this claim"}
          </>
        ) : (
          "Design a first experiment"
        )}
      </div>
      <div className="briefing-next-settle" title={belief.title}>
        to settle <NodePill node={belief} palette={palette} onSelect={onSelect} /> {trunc(belief.title, 48)}
      </div>
      <div className="briefing-next-meta mono">
        {/* A belief with no confidence recorded has none to show: printing 0.50
            states a score nobody set. */}
        {typeof belief.confidence === "number" ? <span>conf {belief.confidence.toFixed(2)}</span> : <span>no confidence set</span>}
        <span>{entry.evidence} evidence</span>
        {entry.leaning > 0 ? <span>{entry.leaning} lean on it</span> : null}
        <span>{entry.price.priced ? `~${usd(entry.price.usd)} per test` : "no test priced yet"}</span>
      </div>
    </div>
  );
}

/** A record's pill: its kind's glyph and `Kind#seq`, a click selecting it. Its
 *  border, wash and ink are the kind's own hue, lifted a step for prose. */
function NodePill({ node, palette, onSelect }: { node: GraphNode; palette: Palette; onSelect: (node: GraphNode) => void }) {
  return (
    <button type="button" className="briefing-pill mono" style={tint(palette, node.kind)} onClick={() => onSelect(node)}>
      <KindIcon kind={node.kind} size={11} />
      {node.kind}#{node.seq}
    </button>
  );
}

/** A pill's `--c`: the kind's canvas hue, one step lighter, as the design's panels wear it. */
const tint = (palette: Palette, kind: string) => ({ "--c": panelKind(palette, kind) }) as CSSProperties;

/** A belief's verdict dot: filled when proven, hollow when disproven, dimmed when open. */
function BeliefDot({ belief }: { belief: GraphNode }) {
  return <span className={`briefing-dot ${belief.judgement === "disproven" ? "is-hollow" : belief.judgement === "proven" ? "is-proven" : "is-open"}`} />;
}

/** A title shortened to one line's worth, as the design's lists do; the row's
 *  `title` still carries the whole of it. */
function trunc(text: string, most: number): string {
  return text.length > most ? `${text.slice(0, most - 1).trimEnd()}…` : text;
}

/** The thread as plain prose, which the `copy` button puts on the clipboard. */
function writeUp(
  group: RootGroup,
  members: readonly GraphNode[],
  verdicts: { readonly proven: number; readonly unproven: number; readonly disproven: number },
  turns: readonly Turn[],
  view: GraphView,
  now: number,
): string {
  const lines = [
    `# ${group.root?.title || "Unrooted"}`,
    "",
    `${members.length.toLocaleString("en-US")} records as of ${dateTime(new Date(now).toISOString())}.`,
    `${verdicts.proven} proven · ${verdicts.unproven} unproven · ${verdicts.disproven} disproven.`,
    "",
    "Turning points:",
  ];
  for (const turn of turns) {
    const named = turn.refs.map((id) => view.byId.get(id)).filter((node) => node !== undefined);
    lines.push(`- ${dateTime(turn.created)} — ${turn.say} ${named.map((node) => `${node.kind}#${node.seq} ${node.title}`).join(", ")}`);
  }
  return lines.join("\n");
}
