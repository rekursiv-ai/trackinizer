import { useDeferredValue, useId, useState } from "react";
import type { Detail } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { useRelationActions } from "../relations/flows";
import { RelationRow } from "../relations/RelationRow";
import { formatRoute } from "../router/route";
import { PriorityGlyph, StateGlyphs } from "../ui/glyphs";
import { type RailPeer, railPeers, relationGroups } from "./relationGroups";
import { EdgeMarks } from "./Relations";

/**
 * The rail beside the description, as the old UI's Before and After panes had
 * it: the inquiry's parents, then its children, by its lineage
 * edges. Each peer shows once, with its status, title and every edge joining
 * it, read from this inquiry; a writer annotates or removes each edge from its
 * line, and adds from each section's "+ add".
 */
export function Rail({ detail }: { detail: Detail }) {
  const { edges } = useMeta();
  const groups = relationGroups(detail, edges);
  return (
    <>
      <Section detail={detail} title="Parents" add="Add parent" peers={railPeers(groups, "out")} />
      <Section detail={detail} title="Children" add="Add child" peers={railPeers(groups, "in")} />
    </>
  );
}

/**
 * One side of the rail: its first ten peers, then all on "Show all", drawn in a
 * background render React can interrupt, as a relation group's are: the largest
 * hub has 731 relations.
 */
function Section({ detail, title, add, peers }: { detail: Detail; title: string; add: string; peers: readonly RailPeer[] }) {
  const mode = useWriteMode();
  const adding = useRelationActions().find((action) => action.id === "relation.add");
  const [all, setAll] = useState(false);
  const drawn = useDeferredValue(all ? peers.length : SHOWN);
  const id = useId();
  return (
    <section className="rail-sec" aria-labelledby={id}>
      <div className="rail-h">
        <h2 id={id}>{title}</h2>
        <span className="count">{peers.length}</span>
        {adding ? (
          // The picker offers every relation, both ways: it takes no direction.
          <button
            type="button"
            className="rail-add"
            aria-label={add}
            disabled={mode === "disabled"}
            onClick={(event) => adding.run(event.currentTarget)}
          >
            + add
          </button>
        ) : null}
      </div>
      {peers.length ? (
        <ul className="rail-list">
          {peers.slice(0, drawn).map((peer) => (
            <PeerRow key={peer.peer.id} detail={detail} rail={peer} mode={mode} />
          ))}
        </ul>
      ) : (
        <p className="rail-none">None</p>
      )}
      {!all && peers.length > SHOWN ? (
        <button type="button" className="rel-more" onClick={() => setAll(true)}>
          Show all {peers.length}
        </button>
      ) : null}
    </section>
  );
}

/**
 * One peer: a link with its status, title and `Kind#seq`, then a line per edge
 * joining it, each a relation row with its own actions. A child's priority under
 * this inquiry is its edge's priority glyph (COLD-17).
 */
function PeerRow({ detail, rail: { peer, edges }, mode }: { detail: Detail; rail: RailPeer; mode: WriteMode }) {
  const ref = `${peer.kind}#${peer.seq}`;
  const href = formatRoute({ name: "ref", kind: peer.kind, seq: peer.seq });
  return (
    <li className="rail-peer">
      <a className="rail-link" href={href}>
        <StateGlyphs status={peer.status} judgement={peer.judgement} />
        <span className="rail-title">
          {peer.title} <span className="rel-ref">{ref}</span>
        </span>
      </a>
      <ul className="rail-edges">
        {edges.map(({ group, peer: edged }) => {
          const inbound = group.direction === "in";
          return (
            <RelationRow key={group.edgeKind} detail={detail} group={group} peer={edged} mode={mode}>
              <a className="rel-link" href={href}>
                {inbound && edged.priority !== undefined ? <PriorityGlyph priority={edged.priority} /> : null}
                <span className="edge-name">{group.name}</span>
                <span className="sr-only"> {ref}</span>
                <EdgeMarks peer={edged} inbound={inbound} />
              </a>
            </RelationRow>
          );
        })}
      </ul>
    </li>
  );
}

/** Peers a section shows before "Show all". */
const SHOWN = 10;

type WriteMode = ReturnType<typeof useWriteMode>;
