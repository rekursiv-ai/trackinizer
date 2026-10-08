import { type CSSProperties, useState } from "react";
import { dateTime, useMinuteClock } from "../detail/time";
import { formatRoute } from "../router/route";
import { shortAge } from "../lists/Row";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { StatusGlyph } from "../ui/glyphs";
import type { Graph, GraphNode } from "../api/graph";
import { type Palette, panelKind } from "./encode";
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
      {root && (held.length > 0 || broken.length > 0) ? (
        <section className="briefing-sec">
          <h5>
            summary
          </h5>
          <p className="briefing-prose">
            This thread set out to {lower(root.title.replace(/[.?]$/, ""))}.{" "}
            {held[0] ? (
              <>
                So far the evidence shows <NodePill node={held[0].belief} palette={palette} onSelect={onSelectNode} /> {held[0].belief.title};{" "}
              </>
            ) : (
              "Nothing is proven yet. "
            )}
            {broken[0] ? (
              <>
                The evidence rules out <NodePill node={broken[0].belief} palette={palette} onSelect={onSelectNode} /> {broken[0].belief.title}.{" "}
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
        <span>conf {(belief.confidence ?? 0.5).toFixed(2)}</span>
        <span>{entry.evidence} evidence</span>
        {entry.leaning > 0 ? <span>{entry.leaning} lean on it</span> : null}
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

function lower(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
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
