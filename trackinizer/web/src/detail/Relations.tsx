import { useDeferredValue, useId, useState } from "react";
import type { Detail, Peer } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { useRelationActions } from "../relations/flows";
import { RelationRow } from "../relations/RelationRow";
import { formatRoute } from "../router/route";
import { LabelChip, PriorityGlyph, StateGlyphs } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { isRailEdge, type RelationGroup, relationGroups } from "./relationGroups";

/**
 * The inquiry's other relations: those the rail does not show (citations),
 * grouped by edge kind and direction. With none, it renders nothing.
 *
 * Each group shows its first 100 rows, then "Show all": the largest hub's 731
 * rows painted in 11 ms, so there is no virtualization. A writer adds relations
 * from the header, and annotates or removes each from its row.
 */
export function Relations({ detail }: { detail: Detail }) {
  const { edges } = useMeta();
  const mode = useWriteMode();
  const add = useRelationActions().find((action) => action.id === "relation.add");
  const groups = relationGroups(detail, edges).filter((group) => !isRailEdge(group.edgeKind));
  const id = useId();
  if (!groups.length) return null;
  const count = groups.reduce((total, group) => total + group.peers.length, 0);
  // Each group's share of the first screen's rows, in order: the first groups
  // fill it, and the rest start empty.
  let left = FIRST_SCREEN;
  const firsts = groups.map((group) => {
    const first = Math.min(group.peers.length, left);
    left -= first;
    return first;
  });
  return (
    <section className="sec" aria-labelledby={id}>
      <div className="sec-h">
        <h2 id={id}>
          Other relations <span className="count">{count}</span>
        </h2>
        {add ? (
          <>
            <div className="spacer" />
            <button
              type="button"
              className="btn ghost"
              disabled={mode === "disabled"}
              onClick={(event) => add.run(event.currentTarget)}
            >
              <Icon name="plus" size={13} />
              Add relation <kbd className="hide-sm">R</kbd>
            </button>
          </>
        ) : null}
      </div>
      <div className="rel-list">
        {groups.map((group, index) => (
          <Group key={`${group.edgeKind}:${group.direction}`} detail={detail} group={group} first={firsts[index]!} mode={mode} />
        ))}
      </div>
    </section>
  );
}

/**
 * One group's rows: `first` of them in the first render, then up to 100, or all
 * after "Show all", in a background render React can interrupt
 * (https://react.dev/reference/react/useDeferredValue). Drawing a hub's 121 rows
 * at once took one task of 51 to 62 ms with the CPU slowed 4x, over the plan's
 * 50 ms. The first screen's rows fill the tallest window, so the rows that follow
 * land below the fold and shift nothing in view.
 */
function Group({ detail, group, first, mode }: { detail: Detail; group: RelationGroup; first: number; mode: WriteMode }) {
  const [all, setAll] = useState(false);
  const id = useId();
  const limit = all ? group.peers.length : SHOWN;
  const drawn = useDeferredValue(limit, first);
  const shown = group.peers.slice(0, drawn);
  return (
    <div role="group" aria-labelledby={id}>
      <div className="rel-group-h">
        <Icon name={group.direction === "out" ? "arrowR" : "arrowL"} size={12} />
        <span id={id}>{group.label}</span>
        <span className="count">{group.peers.length}</span>
      </div>
      <ul>
        {shown.map((peer) => (
          <Row key={peer.id} detail={detail} group={group} peer={peer} mode={mode} />
        ))}
      </ul>
      {drawn === limit && shown.length < group.peers.length ? (
        <button type="button" className="rel-more" onClick={() => setAll(true)}>
          Show all {group.peers.length}
        </button>
      ) : null}
    </div>
  );
}

/**
 * One related inquiry, with the edge's annotations.
 *
 * An edge priority belongs to the child under this one parent. On a child of
 * this inquiry it is that child's priority here, so it draws the child's
 * priority glyph (COLD-17). On a parent it is this inquiry's priority under
 * that parent, a tag rather than the parent's glyph. The server sends no
 * neighbour's own priority, so a child without one shows its kind.
 */
function Row({ detail, group, peer, mode }: { detail: Detail; group: RelationGroup; peer: Peer; mode: WriteMode }) {
  const inbound = group.direction === "in";
  const priority = peer.priority ?? null;
  return (
    <RelationRow detail={detail} group={group} peer={peer} mode={mode}>
      <a className="rel-link" href={formatRoute({ name: "ref", kind: peer.kind, seq: peer.seq })}>
        {inbound && priority !== null ? <PriorityGlyph priority={priority} /> : <KindIcon kind={peer.kind} />}
        <span className="rel-ref">{`${peer.kind}#${peer.seq}`}</span>
        <StateGlyphs status={peer.status} judgement={peer.judgement} />
        <span className="rel-title">{peer.title}</span>
        <EdgeMarks peer={peer} inbound={inbound} />
      </a>
    </RelationRow>
  );
}

/**
 * One edge's annotations on a relation row: its valence, this inquiry's priority
 * under a parent, its note and its labels. A child's priority under this inquiry
 * is the row's priority glyph, which the row draws.
 */
export function EdgeMarks({ peer, inbound }: { peer: Peer; inbound: boolean }) {
  return (
    <span className="rel-annot">
      {peer.valence !== undefined ? <Valence value={peer.valence} /> : null}
      {!inbound && peer.priority !== undefined ? (
        <span className="kind-tag" title="This inquiry's priority under it">
          p{peer.priority}
        </span>
      ) : null}
      {peer.note ? (
        <span className="rel-note" title={peer.note}>
          {peer.note}
        </span>
      ) : null}
      {peer.labels?.map((label) => <LabelChip key={label} label={label} />)}
    </span>
  );
}

/** A signed valence: a bar out from the middle, green for, red against. */
function Valence({ value }: { value: number }) {
  const sign = value >= 0 ? "pos" : "neg";
  return (
    <span className="valence-wrap" title={`Valence ${value}`}>
      <span className={`valence ${sign}`} aria-hidden="true">
        <i style={{ width: `${Math.min(1, Math.abs(value)) * 50}%` }} />
      </span>
      <span className={`v-num ${sign}`}>
        {value > 0 ? "+" : ""}
        {value.toFixed(1)}
      </span>
    </span>
  );
}

const SHOWN = 100;
/** Rows in a detail's first render, across its groups: at 38 px each, more than the tallest window holds. */
const FIRST_SCREEN = 40;

type WriteMode = ReturnType<typeof useWriteMode>;
