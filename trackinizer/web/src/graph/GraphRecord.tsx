import { useQuery } from "@tanstack/react-query";
import { type CSSProperties, type ReactNode, useState } from "react";
import type { Graph, GraphEdge, GraphNode } from "../api/graph";
import type { Change, Detail, DetailRow, Peer } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { type Field, kindFields, usd } from "../detail/fields";
import { detailQueries } from "../detail/queries";
import { isRailEdge, railPeers, relationGroups, type RailPeer, type RelationGroup } from "../detail/relationGroups";
import { relativeTime, useMinuteClock } from "../detail/time";
import { type ActivityItem, activityItems, describeChange, type Mention } from "../detail/timeline";
import { PropertyEditor } from "../editors/PropertyEditor";
import { TextEditor, TitleEditor } from "../editors/TextEditor";
import { Markdown } from "../markdown/Markdown";
import { RelationFlows, useRelationActions } from "../relations/flows";
import { formatRoute } from "../router/route";
import { Avatar } from "../ui/glyphs";
import { KindIcon } from "../ui/kinds";
import { type Palette, panelKind } from "./encode";
import {
  chainOf,
  groupsOf,
  impactOf,
  ledgerOf,
  loadOf,
  nextExperiments,
  viewOf,
  type GraphView,
} from "./briefing";
import "./record.css";

/** How many rows a section stands open with. */
const SHOWN = 6;
/** How many votes a ledger stands open with, as many as the design's did. */
const LEDGER = 12;
/** How many of the records leaning on this one the panel names. */
const LEANERS = 8;
/** The changes the panel tells, of the last 50 the server sends. */
const TOLD = 3;

/**
 * The record, as the design prototype's panel told it: its ref and verdict, what
 * it says, how sure it is and why, the votes that settled it and what would wobble
 * were it wrong, where it came from, what it cost, who changed it last, and every
 * field it has.
 *
 * It is the graph's one read of a selected record. What the canvas knows — the
 * graph the view already holds gives the chain, the ledger and the impact — draws
 * at once; `/api/web/get` adds what only the row carries: its description, its
 * owner's change, its costs and its fields, a moment later.
 */
export function GraphRecord({
  node,
  graph,
  palette,
  onSelectNode,
  onHoverPeer,
}: {
  node: GraphNode;
  graph: Graph;
  palette: Palette;
  onSelectNode: (node: GraphNode) => void;
  onHoverPeer?: (node: GraphNode | null) => void;
}) {
  const meta = useMeta();
  const read = useQuery(detailQueries.detail(node.id));
  const now = useMinuteClock();
  const view: GraphView = viewOf(graph);
  // A `Kind#seq` in prose wears its kind's hue one step out of the black ground,
  // as the design's panels do; `--c` carries it to a pill's border and wash.
  const ink = (kind: string) => panelKind(palette, kind);
  const belief = node.kind === "Belief";
  const detail = read.data;
  const row = detail?.self;
  const created = relativeTime(node.created, now);
  const changed = row === undefined ? null : relativeTime(row.modified, now);
  const fields = row === undefined ? [] : kindFields(row, meta.fieldOwners);
  // The panel's editable blocks, on the app's own editors: its chips, its title,
  // the summary — the row's `description`, which every kind has a route for — and
  // the rest of its prose. Each is plain until the row is read a moment later.
  const status = fields.find((field) => field.name === "status");
  const judgement = fields.find((field) => field.name === "judgement");
  const verdict = node.judgement ?? (judgement ? "unproven" : undefined);
  const owner = fields.find((field) => field.name === "owner");
  const ownedBy = typeof owner?.value === "string" ? owner.value : node.owner;
  // A record the canvas gives no owner waits for its row before it says
  // "unassigned", so the line never reads one thing and then its opposite.
  const who = ownedBy ?? (detail ? "unassigned" : undefined);
  const description = fields.find((field) => field.name === "description");
  const prose = fields.filter((field) => field.look.place === "text" && field.name !== "description" && field.value !== undefined);
  const edit = (field: Field | undefined, chip: ReactNode) =>
    detail && field ? <PropertyEditor detail={detail} field={field}>{chip}</PropertyEditor> : chip;
  const chain = chainOf(node, view);
  const load = belief ? loadOf(node, view) : null;
  const votes = belief ? byWeight(ledgerOf(node, view)) : [];
  const mix = belief ? byRole(votes.map(({ node: other }) => other)) : null;
  const leans = impactOf(node, view, 1_000);
  const direct = leans.filter(({ hops }) => hops === 1).length;
  const indirect = leans.length - direct;
  // What this record cites; a claim's own votes are the ledger above, which reads
  // the edges the other way.
  const groups = groupsOf(node, view);
  const casts = byWeight(groups.filter((group) => group.label === "proves" || group.label === "favors").flatMap((group) => group.rows));
  // A belief with no trail of its own is still reached: the design ran the chain
  // through the proof that carries it, and closed it on the belief.
  const prover = belief && chain.length === 0 ? votes.find(({ edge }) => edge.edge_kind === "proves") : undefined;
  const steps = chain.length > 0 ? chain : prover ? [...chainOf(prover.node, view), node] : [];
  // The claim alone as a one-member thread: what would settle it — re-run the
  // strongest experiment citing it, or design one.
  const next = belief
    ? nextExperiments({ root: null, key: node.id, members: [node.id], newest: node.created, kinds: new Map() }, view)
    : [];
  const cost = fields.filter((field) => field.look.format === "usd" && field.value !== undefined);
  // A record that spent nothing says so once, rather than in two empty cards.
  const spent = cost.filter((field) => Number(field.value) > 0);

  return (
    <div className="record">
      <div className="rec-h">
        <p className="rec-ref">
          <span className="rec-kref" style={{ color: ink(node.kind) }}>
            {node.kind}#{node.seq}
          </span>
          {edit(status, <span className="rec-chip">{node.status}</span>)}
          {belief && verdict !== undefined
            ? edit(judgement, <span className={"rec-chip is-" + verdict}>{verdict}</span>)
            : null}
        </p>
        {detail ? <TitleEditor detail={detail} /> : <h3 className="rec-title">{node.title || "(untitled)"}</h3>}
        <p className="rec-sub">
          {who !== undefined ? <>{edit(owner, <span className="rec-owner">{who}</span>)}{" · "}</> : null}
          {created}
          {changed !== null && changed !== created ? (
            <>
              {" · "}
              {/* “changed yesterday” is one thought; a break after “changed” orphans the date. */}
              <span className="rec-nowrap">changed {changed}</span>
            </>
          ) : null}
        </p>
      </div>

      {/* The summary: the row's own prose, which every kind can carry and a human
          or an agent can write here, in place. It stands open whether or not it
          has been filled, so the column is the same on every record. */}
      {detail && description ? (
        <Section name="summary">
          <TextEditor detail={detail} field={description}>
            <div className="rec-text" data-field="description">
              {typeof description.value === "string" ? (
                <Markdown source={description.value} kinds={meta.kinds} />
              ) : (
                <p className="rec-empty">Add a summary — click to write it.</p>
              )}
            </div>
          </TextEditor>
        </Section>
      ) : null}
      {detail
        ? prose.map((field) => (
            <Section key={field.name} name={field.look.label.toLowerCase()}>
              <TextEditor detail={detail} field={field}>
                <div className="rec-text" data-field={field.name}>
                  <Markdown source={String(field.value)} kinds={meta.kinds} />
                </div>
              </TextEditor>
            </Section>
          ))
        : null}
      {read.isError ? (
        <p className="rec-note">
          Could not read the record: {read.error.message}{" "}
          <button type="button" className="rec-act" onClick={() => void read.refetch()}>
            retry
          </button>
        </p>
      ) : null}

      {belief && node.confidence !== undefined ? (
        <Section name="confidence">
          <div className="rec-conf">
            <span className="rec-cbar" aria-hidden="true">
              <b style={fillOf(node.confidence)} />
              <i />
            </span>
            <span className="num">{node.confidence.toFixed(2)}</span>
          </div>
          <p className="rec-note">{weightOf(load, leans.length)}</p>
        </Section>
      ) : null}

      {belief && mix ? (
        <Section name="evidence mix">
          <p className="rec-mix">
            {ROLE_LABELS.map(([role, label, plural]) =>
              mix[role] > 0 ? (
                <span key={role}>
                  <i className={`is-${role}`} />
                  {mix[role]} {mix[role] === 1 ? label : plural}
                </span>
              ) : null,
            )}
          </p>
          <p className="rec-mix sub">
            <span>{load?.for ?? 0} load-bearing for</span>
            <span className={load !== null && load.against > 0 ? "is-against" : undefined}>{load?.against ?? 0} load-bearing against</span>
          </p>
          {/* The design's two cautions on a claim's evidence, read off the same counts. */}
          {node.judgement !== "disproven" && (load?.for ?? 0) <= 1 ? (
            <p className="rec-note is-warn">
              Rests on {(load?.for ?? 0) === 1 ? "a single" : "no"} load-bearing citation in favor
              {mix.exp > 0 ? "" : ", and no experiment"}. Worth strengthening before more work builds on it.
            </p>
          ) : node.judgement === "proven" && mix.exp === 0 ? (
            <p className="rec-note">Proven from papers and web sources only; no experiment of ours backs it yet.</p>
          ) : null}
        </Section>
      ) : null}

      <Section name="if this were wrong" count={leans.length}>
        {leans.length === 0 ? (
          <p className="rec-note">Nothing builds on this yet.</p>
        ) : (
          <>
            <p className="rec-note">
              <em>{direct}</em> {direct === 1 ? "belief cites" : "beliefs cite"} it directly
              {load && load.for > 0 ? ` (${load.for} as load-bearing proof)` : ""}
              {indirect > 0 ? (
                <>
                  , and <em>{indirect}</em> more lean on those
                </>
              ) : null}
              . Their owners would get a dependency alert.
            </p>
            {leans.slice(0, LEANERS).map(({ belief: other, hops }) => (
              <button key={other.id} type="button" className="rec-row" onClick={() => onSelectNode(other)}>
                <span className={`rec-dot is-${other.judgement ?? "unproven"}`} />
                <span className="rec-kref" style={{ color: ink(other.kind) }}>
                  {other.kind}#{other.seq}
                </span>
                <span className="rec-row-title">{other.title || "(untitled)"}</span>
                <span className="rec-row-side">{hops === 1 ? "direct" : `${hops - 1} step${hops === 2 ? "" : "s"} away`}</span>
              </button>
            ))}
            {leans.length > LEANERS ? <p className="rec-more">+{leans.length - LEANERS} more</p> : null}
          </>
        )}
      </Section>

      <Section name="cost">
        {spent.length > 0 ? (
          <div className="rec-spend">
            {spent.map((field) => (
              <div key={field.name}>
                <b className="num">{usd(Number(field.value))}</b>
                <small>{field.look.label}</small>
              </div>
            ))}
          </div>
        ) : (
          <p className="rec-note">No cost recorded.</p>
        )}
      </Section>

      {steps.length > 1 ? (
        <Section name="how it got here">
          {steps.map((step, index) => {
            // A borrowed trail ends on this belief, reached by the proof that carries it.
            const tail = prover !== undefined && index === steps.length - 1 ? prover : null;
            const against = tail !== null && (tail.edge.valence ?? 1) < 0;
            return (
              <div key={step.id} className="rec-step">
                {index > 0 ? (
                  <span className="rec-verb" style={tail ? { color: against ? "var(--p-vneg)" : "var(--p-vpos)" } : undefined}>
                    {tail ? (against ? "argues against" : "proves") : "produced"}
                  </span>
                ) : null}
                <Pill node={step} ink={ink} onSelect={onSelectNode} />
                <span className="rec-step-title">{step.title || "(untitled)"}</span>
              </div>
            );
          })}
        </Section>
      ) : null}

      {votes.length > 0 ? (
        <Section name="proved_by / favored_by" count={votes.length}>
          <Ledger rows={votes} ink={ink} onSelect={onSelectNode} onHoverPeer={onHoverPeer} />
        </Section>
      ) : null}

      {casts.length > 0 ? (
        <Section name="proves / favors" count={casts.length}>
          <Ledger rows={casts} ink={ink} onSelect={onSelectNode} onHoverPeer={onHoverPeer} most={SHOWN} />
        </Section>
      ) : null}

      {/* Its lineage, as the detail's rail has it: the records above it and the
          ones below, then the rest, each neighbour named once with every verb
          that joins it, and the rail's own "+ add" on each side. */}
      {detail ? (
        <RelationFlows detail={detail}>
          <Lineage detail={detail} view={view} ink={ink} onSelect={onSelectNode} onHoverPeer={onHoverPeer} />
        </RelationFlows>
      ) : null}

      {next[0] ? (
        <Section name="best next experiment">
          <p className="rec-prose">
            {next[0].template ? (
              <>
                {next[0].rerun ? "Re-run" : "Adapt"} <Pill node={next[0].template} ink={ink} onSelect={onSelectNode} /> to settle{" "}
                <Pill node={next[0].belief} ink={ink} onSelect={onSelectNode} />
              </>
            ) : (
              <>
                Design a first experiment to settle <Pill node={next[0].belief} ink={ink} onSelect={onSelectNode} />
              </>
            )}
          </p>
        </Section>
      ) : null}

      {read.data ? <Activity items={activityItems(read.data.changes)} detail={read.data} fields={fields} now={now} ink={ink} /> : null}
      {row !== undefined ? <AllFields row={row} fields={fields} /> : null}
    </div>
  );
}

/** One section: its heading — a plain sentence, in the sans — over its rows. */
function Section({ name, count, action, children }: { name: string; count?: number; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="rec-sec">
      <h5>
        {name}
        {count !== undefined && count > 0 ? <span>{count}</span> : null}
        {action}
      </h5>
      {children}
    </section>
  );
}

/**
 * "+ add" on a lineage section, as the detail's rail has it: the same relation
 * picker, opened from the heading and answering to the same write mode.
 */
function AddRelation({ title }: { title: string }) {
  const mode = useWriteMode();
  const adding = useRelationActions().find((action) => action.id === "relation.add");
  if (!adding) return null;
  return (
    <button
      type="button"
      className="rec-add"
      aria-label={title}
      disabled={mode === "disabled"}
      onClick={(event) => adding.run(event.currentTarget)}
    >
      + add
    </button>
  );
}

/** A claim's ledger: its heaviest votes first, the rest behind `+N more`. */
function Ledger({
  rows,
  ink,
  onSelect,
  onHoverPeer,
  most = LEDGER,
}: {
  rows: readonly { node: GraphNode; edge: GraphEdge }[];
  ink: (kind: string) => string;
  onSelect: (node: GraphNode) => void;
  onHoverPeer?: (node: GraphNode | null) => void;
  most?: number;
}) {
  const [open, setOpen] = useState(false);
  const shown = open ? rows : rows.slice(0, most);
  return (
    <>
      {shown.map(({ node: other, edge }, index) => (
        <Vote
          // The same evidence can cite a claim twice through two records; React needs one key per row.
          key={`${other.id}-${index}`}
          other={other}
          edge={edge}
          ink={ink}
          onSelect={onSelect}
          onHoverPeer={onHoverPeer}
        />
      ))}
      {!open && rows.length > most ? (
        <button type="button" className="rec-more" onClick={() => setOpen(true)}>
          +{rows.length - most} more
        </button>
      ) : null}
    </>
  );
}

/** One vote on a claim: its bar out from the middle, the record that cast it, and its title. */
function Vote({
  other,
  edge,
  ink,
  onSelect,
  onHoverPeer,
}: {
  other: GraphNode;
  edge: GraphEdge;
  ink: (kind: string) => string;
  onSelect: (node: GraphNode) => void;
  onHoverPeer?: (node: GraphNode | null) => void;
}) {
  const value = edge.valence ?? 1;
  const against = value < 0;
  return (
    <button
      type="button"
      className="rec-lrow"
      onMouseEnter={() => onHoverPeer?.(other)}
      onMouseLeave={() => onHoverPeer?.(null)}
      onFocus={() => onHoverPeer?.(other)}
      onBlur={() => onHoverPeer?.(null)}
      onClick={() => onSelect(other)}
    >
      <span className={`rec-vb ${against ? "is-against" : "is-for"}`}>
        <i
          className={edge.edge_kind === "favors" ? "is-favor" : undefined}
          style={{ width: `${Math.min(Math.abs(value), 1) * 28}px`, ...(against ? { right: "50%" } : { left: "50%" }) }}
        />
      </span>
      <span className="rec-lt">
        <span className="rec-kref" style={{ color: ink(other.kind) }}>
          {other.kind}#{other.seq}
        </span>
        <span className="rec-lt-title">{other.title || "(untitled)"}</span>
      </span>
    </button>
  );
}

/** One neighbour's row: the record at the other end of an edge, and every verb that joins it here. */
type PeerRow = { readonly peer: Peer; readonly names: readonly string[] };

/** One side of the rail, as rows: each neighbour once, under all the verbs that join it. */
function railRows(peers: readonly RailPeer[]): PeerRow[] {
  return peers.map(({ peer, edges }) => ({ peer, names: [...new Set(edges.map(({ group }) => group.name))] }));
}

/**
 * The rail's groups that are neither lineage nor a claim's votes — a citation, a
 * supersession: one entry per edge kind, both directions in it, the section named
 * by the verb read outward from here.
 */
function othersOf(groups: readonly RelationGroup[]): { edgeKind: string; label: string; rows: PeerRow[] }[] {
  const byKind = new Map<string, { edgeKind: string; label: string; rows: PeerRow[] }>();
  for (const group of groups) {
    if (isRailEdge(group.edgeKind) || group.edgeKind === "proves" || group.edgeKind === "favors") continue;
    const known = byKind.get(group.edgeKind);
    const rows = group.peers.map((peer) => ({ peer, names: [group.name] }));
    if (known) known.rows.push(...rows);
    else byKind.set(group.edgeKind, { edgeKind: group.edgeKind, label: group.direction === "out" ? group.name : group.label.toLowerCase(), rows });
  }
  return [...byKind.values()];
}

/**
 * The panel's lineage, read the way the detail's rail reads it: the records above
 * this one, the records below it, then every other relation by its kind. Each
 * neighbour is named once, under all the verbs that join it. A side stands open
 * even when it holds nothing, because that is where "+ add" puts the first one.
 */
function Lineage({
  detail,
  view,
  ink,
  onSelect,
  onHoverPeer,
}: {
  detail: Detail;
  view: GraphView;
  ink: (kind: string) => string;
  onSelect: (node: GraphNode) => void;
  onHoverPeer?: (node: GraphNode | null) => void;
}) {
  const meta = useMeta();
  const rail = relationGroups(detail, meta.edges);
  const peers = (rows: readonly PeerRow[]) => (
    <Peers rows={rows} view={view} ink={ink} onSelect={onSelect} onHoverPeer={onHoverPeer} />
  );
  return (
    <>
      {[
        { name: "parents", add: "Add parent", rows: railRows(railPeers(rail, "out")) },
        { name: "children", add: "Add child", rows: railRows(railPeers(rail, "in")) },
      ].map(({ name, add, rows }) => (
        <Section key={name} name={name} count={rows.length} action={<AddRelation title={add} />}>
          {rows.length > 0 ? peers(rows) : <p className="rec-empty">None yet.</p>}
        </Section>
      ))}
      {othersOf(rail).map((group) => (
        <Section key={group.edgeKind} name={group.label} count={group.rows.length}>
          {peers(group.rows)}
        </Section>
      ))}
    </>
  );
}

/** A section's neighbours: a few of them, and the rest behind the fold. The heading
    names the relation, so a row carries its dot, its ref, its title and its verb. */
function Peers({
  rows,
  view,
  ink,
  onSelect,
  onHoverPeer,
}: {
  rows: readonly PeerRow[];
  view: GraphView;
  ink: (kind: string) => string;
  onSelect: (node: GraphNode) => void;
  onHoverPeer?: (node: GraphNode | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const shown = open ? rows : rows.slice(0, SHOWN);
  return (
    <>
      {shown.map(({ peer, names }) => {
        const body = (
          <>
            <span className="rec-ndot" style={{ background: ink(peer.kind) }} />
            <span className="rec-kref" style={{ color: ink(peer.kind) }}>
              {peer.kind}#{peer.seq}
            </span>
            <span className="rec-row-title">{peer.title || "(untitled)"}</span>
            <span className="rec-row-side">{names.join(" · ")}</span>
          </>
        );
        const drawn = view.byId.get(peer.id);
        // A neighbour the canvas holds opens beside it; one past the view's limit
        // goes to its page, as the detail's rail does.
        return drawn ? (
          <button
            key={peer.id}
            type="button"
            className="rec-row"
            onMouseEnter={() => onHoverPeer?.(drawn)}
            onMouseLeave={() => onHoverPeer?.(null)}
            onClick={() => onSelect(drawn)}
          >
            {body}
          </button>
        ) : (
          <a key={peer.id} className="rec-row" href={formatRoute({ name: "ref", kind: peer.kind, seq: peer.seq })}>
            {body}
          </a>
        );
      })}
      {!open && rows.length > SHOWN ? (
        <button type="button" className="rec-more" onClick={() => setOpen(true)}>
          +{rows.length - SHOWN} more
        </button>
      ) : null}
    </>
  );
}

/** The changes the server names, newest first, three of them. */
function Activity({
  items,
  detail,
  fields,
  now,
  ink,
}: {
  items: readonly ActivityItem[];
  detail: Detail;
  fields: readonly Field[];
  now: number;
  ink: (kind: string) => string;
}) {
  const meta = useMeta();
  if (items.length === 0) return null;
  return (
    <Section name="activity" count={detail.changes.length}>
      {items
        .slice(-TOLD)
        .reverse()
        .map((item, index) => {
          const changes: readonly Change[] = item.type === "change" ? [item.change] : item.changes;
          const last = changes[changes.length - 1]!;
          return (
            <div key={`${last.id}-${index}`} className="rec-ahd">
              <Avatar actor={last.actor} size={18} />
              <span className="rec-a">
                <b>{last.actor}</b>{" "}
                {changes.length > 1
                  ? `${changes.length} upstream changes`
                  : describeChange(changes[0]!, { detail, topology: meta.edges, fields }).map((part, at) =>
                      typeof part === "string" ? part : <Mentioned key={at} mention={part} ink={ink} />,
                    )}
              </span>
              <span className="rec-when">{relativeTime(last.created, now)}</span>
            </div>
          );
        })}
      {items.length > TOLD ? <p className="rec-more-note">{items.length - TOLD} earlier</p> : null}
    </Section>
  );
}

/** An inquiry a change names, as the panel writes it: its ref, in its kind's hue. */
function Mentioned({ mention, ink }: { mention: Mention; ink: (kind: string) => string }) {
  const { peer } = mention;
  return <span style={{ color: ink(peer?.kind ?? "") }}>{refOf(peer) ?? mention.id.slice(0, 8)}</span>;
}

/** Every field the row has that the panel has not already shown, folded shut. */
function AllFields({ row, fields }: { row: DetailRow; fields: readonly Field[] }) {
  const shown = fields.filter((field) => field.value !== undefined && field.look.place !== "title" && field.name !== "description");
  return (
    <details className="rec-raw">
      <summary>all fields</summary>
      <dl className="rec-rawg">
        <dt>id</dt>
        <dd>{row.id}</dd>
        {shown.map((field) => {
          const text = textOf(field);
          return (
            <div key={field.name} className="rec-rawpair">
              <dt>{field.look.label}</dt>
              <dd title={text}>{text}</dd>
            </div>
          );
        })}
      </dl>
    </details>
  );
}

/** A field's value, as one line of the field dump. */
function textOf(field: Field): string {
  const { value, look } = field;
  if (look.format === "usd") return Number(value) === 0 ? "none recorded" : usd(Number(value));
  if (look.format === "fraction") return Number(value).toFixed(2);
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** A record's pill: its kind's glyph and `Kind#seq`, a click selecting it. */
function Pill({ node, ink, onSelect }: { node: GraphNode; ink: (kind: string) => string; onSelect: (node: GraphNode) => void }) {
  return (
    <button type="button" className="rec-pill" style={{ "--c": ink(node.kind) } as CSSProperties} onClick={() => onSelect(node)}>
      <KindIcon kind={node.kind} size={11} />
      {node.kind}#{node.seq}
    </button>
  );
}

/** A neighbour's `Kind#seq`, or nothing when the read does not carry one. */
function refOf(peer: Peer | undefined): string | undefined {
  return peer === undefined ? undefined : `${peer.kind}#${peer.seq}`;
}

/** The design's ledger order: a proof before a lean, the heavier of each first. */
function byWeight(rows: readonly { node: GraphNode; edge: GraphEdge }[]): { node: GraphNode; edge: GraphEdge }[] {
  return [...rows].sort(
    (a, b) =>
      Number(b.edge.edge_kind === "proves") - Number(a.edge.edge_kind === "proves") ||
      Math.abs(b.edge.valence ?? 1) - Math.abs(a.edge.valence ?? 1),
  );
}

/** What leans on a claim, and which way its votes go: the line under its confidence. */
function weightOf(load: { against: number; for: number } | null, impact: number): string {
  const against = load?.against ?? 0;
  const forIt = load?.for ?? 0;
  const leaning = impact === 0 ? "nothing builds on it" : `${impact} record${impact === 1 ? "" : "s"} build on it`;
  if (against > forIt) return `${against} against outweigh ${forIt} in favor, and ${leaning}.`;
  if (forIt > 0) return `${forIt} in favor, ${against} against, and ${leaning}.`;
  return `No load-bearing citation yet, and ${leaning}.`;
}

/** The confidence bar's fill: from the middle toward its own side, as the design drew it. */
function fillOf(confidence: number): CSSProperties {
  const c = Math.min(1, Math.max(0, confidence));
  return {
    left: `${Math.min(c, 0.5) * 100}%`,
    width: `${Math.abs(c - 0.5) * 100}%`,
    background: c >= 0.5 ? "var(--p-vpos)" : "var(--p-vneg)",
  };
}

/** What a claim's evidence is made of, the way the design grouped it. */
const ROLE_LABELS = [
  ["exp", "experiment", "experiments"],
  ["source", "paper or web result", "papers or web results"],
  ["belief", "other belief", "other beliefs"],
  ["work", "work record", "work records"],
] as const;

/** `nodes` counted by the role its kind plays: work, source, experiment or belief. */
function byRole(nodes: readonly GraphNode[]): { [role: string]: number } {
  const mix: { [role: string]: number } = { exp: 0, source: 0, belief: 0, work: 0 };
  for (const node of nodes) mix[ROLE_OF[node.kind] ?? "work"] += 1;
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
