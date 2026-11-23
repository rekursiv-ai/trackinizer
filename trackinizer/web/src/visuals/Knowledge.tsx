import { useQueries, queryOptions, useQuery } from "@tanstack/react-query";
import { useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Change, Detail, Peer } from "../api/detail";
import { type FocusGraph, type FocusNode, type GraphEdge, getGraphFocus } from "../api/graph";
import { type Meta, useMeta } from "../app/boot";
import { detailQueries } from "../detail/queries";
import { calendarDate } from "../detail/time";
import { hopsName } from "../graph/search";
import { formatRoute, UUID } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { useVisualMark } from "./marks";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import "./Knowledge.css";
import { Block } from "../ui/block";
import { Modal } from "../ui/frame";

/**
 * The fold, `trax.knowledge`: what the graph claims of one record.
 *
 * A block is any record the store holds within reach — a Belief, the Paper or run
 * cited for it, the Issue it answers — and it is drawn as a slot, `K#1`, `K#2`,
 * because the fold's own order is what a reader is shown, not the row's number.
 * Between two blocks a wire is drawn only when the store holds an edge between
 * them, which is what makes the fold a graph and not a list; the blocks hang in
 * tiers by how far they stand from the record the window was opened on, so the
 * claim reads first and what formed it fans below.
 *
 * Opening a block reads that claim's row and every record wired into it, each
 * printing its own recorded line, and opening a wire says who put it there. Nothing
 * here is written by this page: a block's body, a source's line and a wire's word
 * are all stored text.
 */
export function Knowledge({ instance, workspace }: RendererProps) {
  const recordId = instance.record_id ?? null;
  const { hops, claim } = foldParams(instance.params ?? {});
  const actions = useWorkspaceActions();
  const meta = useMeta();
  const read = useQuery({ ...foldQuery(recordId ?? "", hops), enabled: recordId !== null });
  useVisualMark(instance, workspace, "data", read.isSuccess);
  const blocks = useMemo(() => (read.data && recordId ? foldClaims(read.data, recordId, hops) : []), [read.data, recordId, hops]);
  const shown = blocks.slice(0, CLAIM_LIMIT);
  const relation = edgeName(meta);
  // The slots a reader can reach: a fold the limit cut names what it left out by
  // its record address, never by a slot no card carries.
  const drawn = new Set(shown.map((block) => block.claim.id));
  // A block says what its claim recorded. The graph read carries a title and a status
  // alone, so each block's row is read for its own body and its own count of records:
  // one cached read per block on the surface, and a block whose row has not arrived
  // yet shows its headline and address alone.
  const bodies = useQueries({ queries: shown.map((block) => detailQueries.detail(block.claim.id)) });
  const rows = new Map(shown.map((block, at) => [block.claim.id, bodies[at]?.data]));
  const [hiding, setHiding] = useState(false);
  const [wire, setWire] = useState<FoldRun | null>(null);
  const surface = useRef<HTMLDivElement>(null);

  /** Move the window: which block it has open. Its reach is a parameter, not a control. */
  const select = useEffectEvent((nextClaim: string) => {
    setWire(null);
    // A close keeps the panel away while the station's revision lands, so that flag is
    // what stops it flashing back open. Asking for a block is a new answer, so it clears
    // the flag: measured live, a fold left shut by `esc` opened nothing else after it.
    if (nextClaim !== "") setHiding(false);
    void actions?.operate({
      kind: "show",
      visual_type: instance.type,
      record_id: recordId ?? undefined,
      params: { hops, claim: nextClaim },
    });
  });

  // The wires are drawn where the blocks actually landed, so their boxes are
  // measured after layout and again whenever the fold or the stage changes size.
  const boxes = useBoxes(surface, blocks, claim);

  if (recordId === null) return <div className="visual-unsupported">Open a record to see what the graph claims of it.</div>;
  const anchor = read.data?.nodes.find((row) => row.id === recordId);
  // The runs are made here, not inside the overlay, so the fold's own machine line
  // can count the wires it actually drew and never a wire the gutter refused.
  const runs = boxes.region
    ? wireRuns(shown, (id) => {
        const one = boxes.of(id);
        return one && { top: one.top - boxes.region!.top, bottom: one.bottom - boxes.region!.top, left: one.left - boxes.region!.left, width: one.width };
      }, relation)
    : [];
  // The opened block is looked for among all of them, not only the drawn ones: a fold
  // the limit cut still opens the block it was addressed to.
  const openedBlock = blocks.find((block) => block.claim.id === claim);
  return (
    <section className="knowledge" aria-label="What we know">
      <header className="knowledge-h">
        {anchor ? (
          <span className="knowledge-lead">
            <span className="knowledge-of mono">{reference(anchor)}</span>
            {blocks.length > shown.length ? <span className="knowledge-count mono">{`${shown.length} of ${blocks.length} within ${hopsName(hops)}`}</span> : null}
            <span className="knowledge-title">{anchor.title || "(untitled)"}</span>
          </span>
        ) : (
          <span className="knowledge-lead muted">Loading…</span>
        )}
        <a className="btn ghost" href={formatRoute({ name: "graph", focus: { ref: { id: recordId }, hops } })}>Open in graph</a>
      </header>
      <div className="knowledge-body" ref={surface}>
        {read.isError ? <ReadFailure error={read.error} retry={() => void read.refetch()} /> : null}
        {!read.data && !read.isError ? <p className="muted" aria-busy="true">Loading claims…</p> : null}
        {read.data && blocks.length === 0 ? <p className="knowledge-none">Nothing in the graph reaches this record yet. A block is any record with a wire to it.</p> : null}
        {shown.length > 0 ? (
          <>
            <Wires runs={runs} host={boxes.region} picked={wire?.key ?? ""} onPick={(run) => setWire(run)} />
            <div className="knowledge-tiers">
              {tiers(shown).map((tier) => (
                <div className="knowledge-tier" key={tier[0]!.claim.id}>
                  {tier.map((block) => (
                    <Block
                      className="knowledge-card"
                      key={block.claim.id}
                      ref={(node) => boxes.observe(block.claim.id, node)}
                      selected={block.claim.id === claim}
                      onSelect={() => select(block.claim.id === claim ? "" : block.claim.id)}
                      slot={`K#${block.slot}`}
                      claim={block.claim.title || "(untitled)"}
                      // What the block claims, in the words its row recorded; a row that
                      // recorded none says what the block rests on instead, in the same
                      // clause the opened panel prints, so no card is a blank box.
                      lines={[body(rows.get(block.claim.id)) || neighbours(block, drawn, relation)]}
                      foot={machine(block, rows.get(block.claim.id))}
                    />
                  ))}
                </div>
              ))}
            </div>
          </>
        ) : null}
      </div>
      {/* The design's bottom line: the fold as the machine reads it, one grey row
          under the tiers, saying what was drawn and from how much it was read. */}
      {shown.length > 0 && anchor ? (
        <footer className="knowledge-machine mono">
          {`${shown.length} ${shown.length === 1 ? "block" : "blocks"} · ${runs.length} ${runs.length === 1 ? "wire" : "wires"} · ${read.data?.nodes.length ?? shown.length} records within ${hopsName(hops)}`}
        </footer>
      ) : null}
      {openedBlock ? <Formation block={openedBlock} drawn={drawn} name={relation} hiding={hiding} onHide={() => { setHiding(true); select(""); }} /> : null}
      {wire ? <Wire run={wire} rows={rows} hops={hops} onHide={() => setWire(null)} /> : null}
    </section>
  );
}

/**
 * One block opened: the block's own row, then every record the store wired into it,
 * each printing the line its own row holds and the citation row of the wire that
 * brought it. The panel is the formation and not a second copy of the card, so the
 * words it states are the ones no wire over the page paints: an edge this block rests
 * on that stands beside it, on its own row, where there is no gutter to run through.
 */
function Formation({ block, drawn, name, hiding, onHide }: {
  readonly block: FoldClaim;
  readonly drawn: ReadonlySet<string>;
  readonly name: (kind: string) => string;
  readonly hiding: boolean;
  readonly onHide: () => void;
}) {
  const opened = useQuery({ ...detailQueries.detail(block.claim.id), enabled: !hiding });
  const formed = useMemo(() => (opened.data ? formation(opened.data) : []), [opened.data]);
  const listed = formed.slice(0, SOURCE_LIMIT);
  // A source's own line is on its row, which the list of neighbours does not carry, so
  // each row shown is read — and only the rows shown: a hub's 36 sources cost 36 reads.
  const sources = useQueries({ queries: listed.map((row) => ({ ...detailQueries.detail(row.peer.id), enabled: opened.isSuccess })) });
  if (!opened.data || hiding) return null;
  const detail = opened.data;
  const n = formed.length;
  const found = recorded(detail.self);
  const into = neighbours(block, drawn, name);
  return (
    <Modal
      cls="knowledge-modal"
      kicker={`K#${block.slot}`}
      note={`${detail.self.status} · ${n} ${n === 1 ? "record" : "records"}`}
      title={detail.self.title || "(untitled)"}
      foot={<a className="btn ghost" href={formatRoute({ name: "ref", kind: detail.self.kind, seq: detail.self.seq })}>Open the claim</a>}
      onClose={onHide}>
      {opened.isError ? <ReadFailure error={opened.error} retry={() => void opened.refetch()} /> : null}
      <p className="knowledge-sec">How it was formed</p>
      <div className="knowledge-spine">
        <div className="knowledge-me">
          <p className="knowledge-src-h">
            <span className="mono">{`K#${block.slot}`}</span>
            {/* A block nothing in the fold rests on has no clause to say, and an
                empty one would hold the row's space. */}
            {into ? <span className="knowledge-into mono">{into}</span> : null}
          </p>
          {/* The card behind clamps this line to four; the whole recorded sentence
              is what a reader came here for, so the panel prints all of it. */}
          {found ? <p className="knowledge-src-line">{found}</p> : null}
        </div>
        {n === 0 ? <p className="knowledge-none">No record points at this claim, so nothing formed it.</p> : null}
        <ol className="knowledge-sources">
          {listed.map((row, i) => (
            <SourceRow key={`${row.kind}:${row.peer.id}`} row={row} asserted={assertRow(detail, row.peer.id, row.kind)} detail={sources[i]?.data} />
          ))}
        </ol>
        {formed.length > listed.length ? <p className="knowledge-rest">{`${formed.length - listed.length} more formed it`}</p> : null}
      </div>
    </Modal>
  );
}

/**
 * What the block rests on: each stored edge's own word and the record it points at,
 * in the order the page shows them — down the tiers, then across one. Only that
 * direction is printed: what points INTO the claim is the whole list the panel draws
 * beneath it, and a hub carries 36 of them, so repeating them would say one fact
 * twice and bury the rows. This is where a pair the fold cannot draw — a child on its
 * parent's own row, with no gutter for a wire — states its edge: no run paints it.
 */
function neighbours(block: FoldClaim, drawn: ReadonlySet<string>, name: (kind: string) => string): string {
  const at = (wire: FoldWire) => (wire.peerSlot !== undefined && drawn.has(wire.peer.id) ? `K#${wire.peerSlot}` : reference(wire.peer));
  const page = (wire: FoldWire) => wire.peerSlot ?? Number.MAX_SAFE_INTEGER;
  const by = new Map<string, string[]>();
  for (const wire of [...block.rests].sort((a, b) => page(a) - page(b) || a.peer.seq - b.peer.seq)) {
    // One word per kind, so a block resting on two papers the same way is one clause
    // and two addresses.
    by.set(wire.edge.edge_kind, [...(by.get(wire.edge.edge_kind) ?? []), at(wire)]);
  }
  const clauses = [...by.entries()].map(([kind, slots]) => `${name(kind)} ${slots.join(" · ")}`);
  return clauses.length === 0 ? "" : `→ ${clauses.join(" · ")}`;
}

/**
 * One wire opened: the two records it joins, what the store writes on the edge
 * itself, and whose change put it there. As on a block, every line is stored text.
 */
function Wire({ run, rows, hops, onHide }: { readonly run: FoldRun; readonly rows: Map<string, Detail | undefined>; readonly hops: Reach; readonly onHide: () => void }) {
  const child = rows.get(run.child.id);
  const parent = rows.get(run.parent.id);
  // The edge's own annotations live on the parent's row, which lists the child as
  // its peer: the note, the labels and the edge's priority, beside the valence the
  // graph read already carried.
  const marked = parent ? formation(parent).find((one) => one.peer.id === run.child.id)?.peer : undefined;
  const said = assertRow(child, run.parent.id, run.edge.edge_kind) ?? assertRow(parent, run.child.id, run.edge.edge_kind);
  const weight = typeof run.edge.valence === "number" ? `${run.edge.valence > 0 ? "+" : ""}${run.edge.valence} ${sign(run.edge.valence)}` : null;
  const onWire = [weight, typeof marked?.priority === "number" ? `priority ${marked.priority}` : null, marked?.labels?.length ? marked.labels.join(", ") : null].filter((one) => one !== null);
  return (
    <Modal
      cls="knowledge-modal"
      title={`${reference(run.child)} ${run.said} ${reference(run.parent)}`}
      foot={<a className="btn ghost" href={formatRoute({ name: "graph", focus: { ref: { id: run.parent.id }, hops } })}>Open in graph</a>}
      onClose={onHide}>
      <p className="knowledge-sec">What it starts at</p>
      <End node={run.child} row={child} />
      <p className="knowledge-sec">What it points at</p>
      <End node={run.parent} row={parent} />
      <p className="knowledge-sec">On the wire</p>
      {marked?.note ? <p className="knowledge-src-line">{marked.note}</p> : null}
      {onWire.length > 0 ? <p className="knowledge-src-note">{onWire.join(" · ")}</p> : null}
      {said ? <p className="knowledge-src-note">{`${said.actor} · ${calendarDate(said.created)}${word(said.reason) ? ` · ${word(said.reason)}` : ""}`}</p> : null}
      {!marked?.note && onWire.length === 0 && !said ? <p className="knowledge-none">The store holds this edge and nothing written on it.</p> : null}
    </Modal>
  );
}

/** One record a wire joins: its address, its headline and the line its own row recorded. */
function End({ node, row }: { readonly node: FocusNode; readonly row: Detail | undefined }) {
  const text = row ? recorded(row.self) : "";
  return (
    <>
      <p className="knowledge-src-h">
        <span className="mono">{reference(node)}</span>
        <span className="knowledge-src-title">{node.title || "(untitled)"}</span>
      </p>
      {text ? <p className="knowledge-src-line">{text}</p> : null}
    </>
  );
}

/** The change that put an edge between a record and its peer, newest first in the log read. */
function assertRow(row: Detail | undefined, peer: string, kind: string): Change | undefined {
  return row?.changes.find((one) => {
    const side = one.new.peer_id ? one.new : one.old;
    return side.peer_id === peer && side.peer_edge_kind === kind;
  });
}

/** The blocks in tiers, one per step from the record the window was opened on. */
function tiers(blocks: readonly FoldClaim[]): FoldClaim[][] {
  const by = new Map<number, FoldClaim[]>();
  for (const block of blocks) by.set(block.tier ?? 0, [...(by.get(block.tier ?? 0) ?? []), block]);
  return [...by.entries()].sort((a, b) => a[0] - b[0]).map(([, one]) => one);
}

/** One record that formed the claim: its address and headline, the line its own row
 *  recorded, and the machine row of the wire that brought it. No relation word: the
 *  fold behind the panel paints each one on the wire between the same two records. */
function SourceRow({ row, asserted, detail }: { readonly row: FormationRow; readonly asserted: Change | undefined; readonly detail: Detail | undefined }) {
  const line = detail ? recorded(detail.self) : "";
  const weight = typeof row.peer.valence === "number" ? `${row.peer.valence > 0 ? "+" : ""}${row.peer.valence} ${sign(row.peer.valence)}` : null;
  const onWire = [weight, word(row.peer.note), typeof row.peer.priority === "number" ? `priority ${row.peer.priority}` : null, row.peer.labels?.length ? row.peer.labels.join(", ") : null].filter((one) => one !== null);
  const cited = [asserted ? `${asserted.actor} · ${calendarDate(asserted.created)}` : null, onWire.join(" · ")].filter((one) => one !== null && one !== "");
  return (
    <li>
      <p className="knowledge-src-h">
        <span className="mono">{reference(row.peer)}</span>
        <span className="knowledge-src-title">{row.peer.title || "(untitled)"}</span>
      </p>
      {line ? <p className="knowledge-src-line">{line}</p> : null}
      {cited.length > 0 ? <p className="knowledge-src-note">{cited.join(" · ")}</p> : null}
    </li>
  );
}

/**
 * The blocks the fold draws: every record the read brought within `hops` of
 * `anchor`, each with a wire to every other record the read brought. Of those
 * wires, `under` are the ones drawn as a run up into the block and `rests` the
 * ones its footer says, so a pair of blocks is named once and not twice.
 *
 * A block is any kind, because a claim's knowledge is the paper, the run, the
 * change and the web result wired into it as much as the claim itself; the kind
 * is read from the block's own address line.
 *
 * A block's tier is how far the walk along those wires stands from the record the
 * window was opened on. The order inside a tier is the store's — more wires
 * first, then a higher certainty, then the newer — so two reads of one window
 * draw one page.
 */
export function foldClaims(graph: FocusGraph, anchor: string, hops: number): FoldClaim[] {
  const claims = graph.nodes.filter((row) => row.hops <= hops);
  const blocks: Fold[] = claims.map((row) => ({ claim: row, wires: [], rests: [], under: [], across: [] }));
  const byId = new Map(blocks.map((block) => [block.claim.id, block]));
  // One pass over the stored edges: each edge both of whose ends are drawn joins
  // the two blocks, and is listed from each side. `onto` says which side this is:
  // the parent's view has the peer as the child that points at it.
  for (const edge of graph.edges) {
    const child = byId.get(edge.from_id);
    const parent = byId.get(edge.to_id);
    if (!child || !parent || child === parent) continue;
    parent.wires.push({ edge, peer: child.claim, onto: true });
    child.wires.push({ edge, peer: parent.claim, onto: false });
  }
  for (const block of blocks) {
    block.wires.sort(
      (a, b) =>
        Number(b.onto) - Number(a.onto) ||
        strength(b.edge.valence) - strength(a.edge.valence) ||
        b.peer.seq - a.peer.seq ||
        a.peer.kind.localeCompare(b.peer.kind),
    );
  }
  // The walk runs both ways along every drawn wire: down to what a block points at
  // and up to the blocks that point at it, so a record is placed by how far it
  // stands from the record the window was opened on, not by what kind it is.
  // Each block is entered once, so one tier is one walk step.
  const seed = byId.get(anchor);
  if (seed) {
    seed.tier = 0;
    const queue = [seed];
    for (let at = 0; at < queue.length; at += 1) {
      const one = queue[at]!;
      for (const wire of one.wires) {
        const next = byId.get(wire.peer.id);
        if (!next || next.tier !== undefined) continue;
        next.tier = one.tier! + 1;
        queue.push(next);
      }
    }
  }
  // A block the walk cannot reach is on no wire this fold draws: it is placed by
  // how far the store's own walk found it, so it is not dropped below the page.
  for (const block of blocks) block.tier ??= block.claim.hops;
  // A run is drawn only where the store's child stands below its parent, so the
  // arrow points up into the end the edge is stored as pointing at. One pair is
  // said once: by the run, when the two blocks stand on different rows. A child on
  // its parent's row or above it has no gutter to run through, so that pair is said
  // by the opened block's own list of what it rests on and by no wire.
  for (const block of blocks) {
    // A wire that is drawn as a run. `onto` is this block's parent's side of it, so
    // the same test reads from either end: the child is the one standing lower.
    const drawn = (wire: FoldWire) => {
      const peer = byId.get(wire.peer.id);
      return peer !== undefined && (wire.onto ? peer.tier! > block.tier! : peer.tier! < block.tier!);
    };
    block.under = block.wires.filter((wire) => wire.onto && drawn(wire));
    // A pair the walk put on ONE tier. The tier is distance from the anchor, so two
    // records a single hop out land side by side even when the store holds an edge
    // between them — and until this list existed that edge was drawn by nothing. A
    // fold would read `0 wires` under a card whose own footer said `came from K#2`,
    // which is a surface contradicting itself in two places a reader can see at once.
    //
    // They are not pushed down a tier to make a gutter: the tier IS the distance
    // from the record the window was opened on, and moving a block to suit a line
    // would make the layout lie about the thing it exists to show. The run is drawn
    // across the row instead.
    block.across = block.wires.filter((wire) => wire.onto && !drawn(wire) && byId.get(wire.peer.id)?.tier === block.tier);
    // The edge is stored child to parent, so the child is the one that rests,
    // whatever kind either end is. These are the wires no run paints — now only the
    // pairs that neither cross a gutter nor share a row.
    block.rests = block.wires.filter((wire) => !wire.onto && !drawn(wire) && byId.get(wire.peer.id)?.tier !== block.tier);
  }
  blocks.sort((a, b) => a.tier! - b.tier! || b.wires.length - a.wires.length || (b.claim.confidence ?? -1) - (a.claim.confidence ?? -1) || b.claim.created.localeCompare(a.claim.created) || a.claim.seq - b.claim.seq);
  blocks.forEach((block, at) => {
    block.slot = at + 1;
  });
  // A wire that runs to another block names it by the slot that block was given; one that
  // runs to a record this fold left out has no slot, and its own address is printed instead.
  for (const block of blocks) for (const wire of [...block.under, ...block.across, ...block.rests]) wire.peerSlot = byId.get(wire.peer.id)?.slot;
  return blocks;
}

/** A block while the fold is still working out its tier, its slots and its runs. */
type Fold = { claim: FocusNode; wires: FoldWire[]; rests: FoldWire[]; under: FoldWire[]; across: FoldWire[]; slot?: number; tier?: number };

/**
 * The records that formed a claim: every row the store points at it. What the claim
 * itself points at is not here — the block's own footer says that in the voice of the
 * record that rests on it.
 *
 * `/api/web/get` keys the group by edge kind and puts the neighbour at the far
 * end, so the record listed first is always the edge's child and the word its
 * own `forward`: `Paper#2 proves Belief#1`, never the reverse.
 */
export function formation(detail: Detail): readonly FormationRow[] {
  const rows = Object.entries(detail.backlinks).flatMap(([kind, peers]) => peers.map((peer) => ({ kind, peer })));
  return rows.sort(
    (a, b) => strength(b.peer.valence) - strength(a.peer.valence) || b.peer.seq - a.peer.seq || a.peer.kind.localeCompare(a.peer.kind),
  );
}

/** One block: a record's row, the records wired to it, those drawn below it that run up into it, and the blocks it rests on. */
export type FoldClaim = {
  readonly claim: FocusNode;
  readonly wires: readonly FoldWire[];
  readonly rests: readonly FoldWire[];
  readonly under: readonly FoldWire[];
  /** Wires to a block on this one's own row, drawn across it rather than up a gutter. */
  readonly across: readonly FoldWire[];
  slot?: number;
  tier?: number;
};

/** One wire of a block, read from the claim's own side of it. */
export type FoldWire = {
  readonly edge: GraphEdge;
  /** The record at the far end. */
  readonly peer: FocusNode;
  /** True when the wire points at the claim, so its peer is evidence for or against it. */
  readonly onto: boolean;
  /** Set once every block has a slot: the block this wire runs to, when it runs to one. */
  peerSlot?: number;
};

/** One row of an opened claim: the kind of wire it came in on, and the record at its far end. */
export type FormationRow = { readonly kind: string; readonly peer: Peer };

/** Where each block's box landed, by its record id, and the surface they sit on. */
type Boxes = {
  readonly of: (id: string) => DOMRect | undefined;
  observe: (id: string, node: HTMLElement | null) => void;
  readonly region: DOMRect | null;
};

/** The blocks' boxes as the page laid them out, re-measured when they or the stage change. */
function useBoxes(surface: React.RefObject<HTMLDivElement | null>, blocks: readonly FoldClaim[], claim: string): Boxes {
  const nodes = useRef(new Map<string, HTMLElement>());
  const [region, setRegion] = useState<DOMRect | null>(null);
  const [, redo] = useState(0);
  useLayoutEffect(() => {
    const host = surface.current;
    if (!host) return;
    const measure = () => {
      setRegion(host.getBoundingClientRect());
      redo((n) => n + 1);
    };
    measure();
    // Guarded the way the canvas guards its own measure: a tile resizes without the
    // window moving, and a test browser has no observer to ask.
    const watch = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    watch?.observe(host);
    window.addEventListener("resize", measure);
    return () => {
      watch?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [surface, blocks.length, claim]);
  return {
    of: (id) => nodes.current.get(id)?.getBoundingClientRect(),
    observe: (id, node) => {
      if (node) nodes.current.set(id, node);
      else nodes.current.delete(id);
    },
    region,
  };
}

/**
 * The white wires between blocks: one right-angle run per stored edge between two of
 * them. Each is clickable, the way a block is, and says in its own words which two
 * records it joins and what the store calls the relation.
 */
function Wires({ runs, host, picked, onPick }: { readonly runs: readonly FoldRun[]; readonly host: DOMRect | null; readonly picked: string; readonly onPick: (run: FoldRun) => void }) {
  if (!host) return null;
  const said = (run: FoldRun) => `${reference(run.child)} ${run.said} ${reference(run.parent)}`;
  return (
    <svg className="knowledge-wires" width={host.width} height={host.height}>
      <defs>
        <marker id="knowledge-head" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">
          <path className="knowledge-head" d="M 0 0 L 8 4 L 0 8 Z" />
        </marker>
      </defs>
      {runs.map((run) => (
        <g key={run.key} className={run.key === picked ? "knowledge-picked" : undefined}>
          <path className={`knowledge-run${OPEN_WIRES.has(run.edge.edge_kind) ? " is-open" : ""}`} d={run.d} markerEnd="url(#knowledge-head)" />
          {/* The word is the target a reader aims at, so it is the button and carries
              the name; the run itself gains a wide invisible stroke to hit with a
              mouse, and says nothing twice. */}
          <text
            className="knowledge-said"
            role="button"
            tabIndex={0}
            aria-label={said(run)}
            x={run.x}
            y={run.y}
            onClick={() => onPick(run)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onPick(run);
              }
            }}
          >
            {run.said}
          </text>
          <path aria-hidden="true" className="knowledge-hit" d={run.d} onClick={() => onPick(run)} />
        </g>
      ))}
    </svg>
  );
}

/** One block's box, in the surface's own coordinates. */
export type FoldBox = { readonly top: number; readonly bottom: number; readonly left: number; readonly width: number };

/** One drawn wire: its path, where its word stands, the word the server labels it with, and the stored edge with both its records. */
export type FoldRun = {
  readonly key: string;
  readonly d: string;
  readonly x: number;
  readonly y: number;
  readonly said: string;
  readonly edge: GraphEdge;
  /** The stored child: the record the run leaves, drawn below. */
  readonly child: FocusNode;
  /** The stored parent: the record the run points into, drawn above. */
  readonly parent: FocusNode;
};

/** The runs between the blocks a fold draws: one per stored edge from a record to
 * the record above it, and none where no gutter lies.
 *
 * A claim is drawn above what it rests on, so a run leaves the support's top edge,
 * stands up into the gutter above that row and turns into the bottom of the claim the
 * store says it supports. The word on it is that edge's own label, so the three parts
 * read in the order the record was written. The turn is the gutter above the support's own
 * row and never the middle of the span, because a support three rows down would else
 * drop its word onto a card; two blocks on one row have no gutter between them, so
 * their relation is said by the opened block's list of what it rests on and not by a
 * line over the page.
 */
export function wireRuns(blocks: readonly FoldClaim[], box: (id: string) => FoldBox | undefined, name: (kind: string) => string): FoldRun[] {
  const rows = rowsOf(blocks, box);
  // The wires the fold draws: one per pair of blocks, however many edges the store
  // holds for the pair — the strongest relation labelled, the pair's full set left
  // to the footer and the opened panel — and only across the gutter to the next
  // row down, whatever tier each block sits in. A hub's far citations are said by
  // the support's own footer rather than rained through the rows between.
  const byPair = new Map<string, FoldWire>();
  const pairBlock = new Map<string, FoldClaim>();
  // The row each block landed in, so "the next row down" is a place on the page
  // and not a graph distance: a star of thirty citations of one claim is four
  // wrapped rows of cards, and only the first row below a claim is the one its
  // wires cross.
  const rowOf = new Map<string, number>();
  for (const block of blocks) {
    const one = box(block.claim.id);
    if (!one) continue;
    rowOf.set(block.claim.id, rows.findIndex((row) => Math.abs(row.top - one.top) < 2));
  }
  for (const block of blocks) {
    for (const wire of block.under) {
      const at = rowOf.get(block.claim.id);
      const supportAt = rowOf.get(wire.peer.id);
      if (at === undefined || supportAt === undefined || supportAt !== at + 1) continue;
      const key = `${wire.peer.id}->${block.claim.id}`;
      const held = byPair.get(key);
      if (!held || Math.abs(wire.edge.valence ?? 0) > Math.abs(held.edge.valence ?? 0)) {
        byPair.set(key, wire);
        pairBlock.set(key, block);
      }
    }
  }
  // A claim with several records under it spreads their landings along its bottom
  // edge, and a record supporting several claims spreads its departures along its
  // own top: wires that share a card never share a point on it, which is what
  // keeps many relations from reading as one trunk.
  const landings = new Map<string, string[]>();
  const departures = new Map<string, string[]>();
  const slotOf = new Map(blocks.map((block) => [block.claim.id, block.slot ?? 0]));
  for (const key of byPair.keys()) {
    const [peer, claim] = key.split("->");
    landings.set(claim, [...(landings.get(claim) ?? []), peer]);
    departures.set(peer, [...(departures.get(peer) ?? []), claim]);
  }
  for (const [claim, peers] of landings) landings.set(claim, peers.sort((a, b) => (slotOf.get(a) ?? 0) - (slotOf.get(b) ?? 0)));
  for (const [peer, claims] of departures) departures.set(peer, claims.sort((a, b) => (slotOf.get(a) ?? 0) - (slotOf.get(b) ?? 0)));
  const spread = (count: readonly string[], id: string) => count.indexOf(id);
  const runs: FoldRun[] = [];
  for (const [key, wire] of byPair) {
    const block = pairBlock.get(key)!;
    const onto = box(block.claim.id);
    if (!onto) continue;
    const under = box(wire.peer.id);
    if (!under) continue;
    const at = rows.findIndex((row) => Math.abs(row.top - under.top) < 2);
    const lo = at < 1 ? onto.bottom : Math.max(onto.bottom, rows[at - 1]!.bottom);
    if (under.top - lo <= 8) continue;
    const turn = Math.round((lo + under.top) / 2);
    const landing = landings.get(block.claim.id) ?? [];
    const leaving = departures.get(wire.peer.id) ?? [];
    const from = { x: under.left + (under.width * (spread(leaving, block.claim.id) + 1)) / (leaving.length + 1), y: under.top };
    const to = { x: onto.left + (onto.width * (spread(landing, wire.peer.id) + 1)) / (landing.length + 1), y: onto.bottom };
    runs.push({
      key: `${wire.peer.id}:${block.claim.id}:${wire.edge.edge_kind}`,
      d: `M ${from.x} ${from.y} V ${turn} H ${to.x} V ${to.y}`,
      x: (from.x + to.x) / 2,
      y: turn - 5,
      said: name(wire.edge.edge_kind),
      edge: wire.edge,
      child: wire.peer,
      parent: block.claim,
    });
  }
  // The runs across a row. A pair the walk placed on one tier has no gutter between
  // them, and before this they were drawn by nothing at all — the fold said `0 wires`
  // with a card in it reading `came from K#2`. The run leaves the child's facing edge
  // and enters the parent's, along the cards' shared middle, so it reads as the same
  // relation turned on its side rather than as a different kind of thing.
  const sameRow = new Map<string, { wire: FoldWire; parent: FoldClaim }>();
  for (const block of blocks) {
    for (const wire of block.across) {
      const at = rowOf.get(block.claim.id);
      const peerAt = rowOf.get(wire.peer.id);
      if (at === undefined || peerAt === undefined || at !== peerAt) continue;
      // One pair, said once, by its strongest relation — the rule the gutter runs keep.
      const key = [wire.peer.id, block.claim.id].sort().join("~");
      const held = sameRow.get(key);
      if (!held || Math.abs(wire.edge.valence ?? 0) > Math.abs(held.wire.edge.valence ?? 0)) {
        sameRow.set(key, { wire, parent: block });
      }
    }
  }
  for (const [key, { wire, parent }] of sameRow) {
    const onto = box(parent.claim.id);
    const from = box(wire.peer.id);
    if (!onto || !from) continue;
    // Left-hand card first, so the arrow is drawn pointing at the parent whichever
    // side of the row it landed on.
    const childLeft = from.left < onto.left;
    const start = { x: childLeft ? from.left + from.width : from.left, y: Math.round((from.top + from.bottom) / 2) };
    const end = { x: childLeft ? onto.left : onto.left + onto.width, y: Math.round((onto.top + onto.bottom) / 2) };
    if (Math.abs(end.x - start.x) <= 8) continue;
    const mid = Math.round((start.x + end.x) / 2);
    // The word stands in the gutter ABOVE the row, not in the gap between the two
    // cards. The gap is whatever the layout left — between two cards it measured 54px,
    // and `came from` needs about 70, so the label was painted half under the card next
    // to it and read `ame fro`. The gutter is the one place on this surface guaranteed
    // to be clear, which is where every other run's word already stands.
    const band = rows[rowOf.get(parent.claim.id) ?? -1];
    runs.push({
      key: `${key}:${wire.edge.edge_kind}`,
      d: `M ${start.x} ${start.y} H ${mid} V ${end.y} H ${end.x}`,
      x: mid,
      y: (band?.top ?? Math.min(start.y, end.y)) - 7,
      said: name(wire.edge.edge_kind),
      edge: wire.edge,
      child: wire.peer,
      parent: parent.claim,
    });
  }
  return runs;
}

/** The rows the blocks landed in, top first: a tier wider than the window is two rows. */
function rowsOf(blocks: readonly FoldClaim[], box: (id: string) => FoldBox | undefined): { top: number; bottom: number }[] {
  const rows: { top: number; bottom: number }[] = [];
  const rects = blocks
    .map((block) => box(block.claim.id))
    .filter((one): one is FoldBox => one !== undefined)
    .sort((a, b) => a.top - b.top);
  for (const one of rects) {
    const last = rows[rows.length - 1];
    // One row starts at one pixel: a tier's cards are aligned along their tops, and a
    // wrapped row starts a whole card below. Its bottom is its tallest card's.
    if (last && Math.abs(one.top - last.top) < 2) last.bottom = Math.max(last.bottom, one.bottom);
    else rows.push({ top: one.top, bottom: one.bottom });
  }
  return rows;
}

/** A window's parameters from its instance's: reach 1 to 3, else 2, and one claim id. */
export function foldParams(params: { readonly [name: string]: string | number | boolean }): { readonly hops: Reach; readonly claim: string } {
  const { hops, claim } = params;
  return {
    hops: REACHES.find((reach) => reach === hops) ?? DEFAULT_HOPS,
    claim: typeof claim === "string" && UUID.test(claim.trim()) ? claim.trim().toLowerCase() : "",
  };
}

/** The claims' window: a hop past what the fold lists, so each block's own wires come with it. */
function foldQuery(recordId: string, hops: Reach) {
  return queryOptions({
    queryKey: ["graph", "fold", recordId, hops],
    queryFn: ({ signal }) => getGraphFocus({ focus: recordId, hops: Math.min(3, hops + 1) as Reach, limit: FOLD_LIMIT }, { signal }),
  });
}

/** The claim's own words: the text its writer recorded, whichever field a kind keeps it in. */
function recorded(row: Detail["self"] | FocusNode) {
  for (const field of ["description", "outcome", "summary", "question", "answer"]) {
    const value = (row as { [name: string]: unknown })[field];
    if (typeof value === "string" && value) return plain(value);
  }
  return "";
}

/**
 * A recorded field as a card can show it: its words, without its markup.
 *
 * A record's `description` is markdown, and a card is four clamped lines of it with
 * no renderer behind them — so what reached the screen was the source. Three cards
 * of one fold read `The seeded root for the **local web app preview**. - Lists,
 * detail and relations render from this tree. - inline `code`, a
 * [link](https://example.com), and a fenced block: ```python`, which is not a
 * sentence anyone wrote to be read.
 *
 * Reducing rather than rendering is the right answer for THIS surface and not a
 * shortcut: a card is a glance at a claim. A heading, a list and a fenced block are
 * structure for a document, and a four-line clamp has no room to honour any of them
 * — the panel a reader opens off the card is where the record is shown whole.
 */
function plain(text: string) {
  return text
    .replace(/```[\s\S]*?```/g, " ")          // fenced blocks carry no sentence
    .replace(/`([^`]*)`/g, "$1")              // inline code reads as its own words
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")    // an image has nothing to say here
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")  // a link reads as its text
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")       // a heading is just its words
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")        // a bullet is a line, not a glyph
    .replace(/^\s{0,3}\d+\.\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, "$1$2")
    .replace(/(?<![\w*])\*([^*\n]+)\*(?![\w*])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/** A recorded word, or nothing when the row leaves the field unset, empty or a list. */
function word(value: unknown) {
  return typeof value === "string" && value ? value.replace(/_/g, " ") : null;
}

const reference = (row: { kind: string; seq: number }) => `${row.kind}#${row.seq}`;
/**
 * The word an edge kind is shown with. It comes from the server's one table of
 * relation labels (`/api/meta/edges`), read in the child's voice, because a wire
 * is stored child to parent; the graph's key and the activity feed read the same
 * table, so one word means one relation on every surface.
 */
export const edgeName = (meta: Meta) => (kind: string) => (meta.edges[kind]?.forward ?? kind).replace(/_/g, " ");

/** The edge kinds drawn dashed: wants and open dependencies — what the design dashes
 *  as not yet established. What the store asserts (a proof, a favor, a production)
 *  stays solid however it is worded. */
const OPEN_WIRES: ReadonlySet<string> = new Set(["requires", "narrows", "suggests", "needs", "awaits"]);
// A block counts the records pointing at its claim from the claim's own row, which is
// the very list its panel prints under `How it was formed`: one number, one meaning.
const machine = (block: FoldClaim, row: Detail | undefined) => {
  if (!row) return reference(block.claim);
  const n = Object.values(row.backlinks).reduce((deep, peers) => deep + peers.length, 0);
  return `${reference(block.claim)} · ${n} ${n === 1 ? "record" : "records"}`;
};
/** What a block claims, in the words its row recorded — nothing while the row is unread. */
const body = (row: Detail | undefined) => (row ? recorded(row.self) : "");
const strength = (valence: number | undefined) => Math.abs(valence ?? 0);
const sign = (valence: number) => (valence === 0 ? "neutral" : valence > 0 ? "for" : "against");

/** A reach the window lights, as the catalog bounds `hops`. */
type Reach = 1 | 2 | 3;
const REACHES: readonly Reach[] = [1, 2, 3];
const DEFAULT_HOPS: Reach = 2;

/** The most records one window reads: the record, the claims on it, and each claim's own wires. */
export const FOLD_LIMIT = 150;

/**
 * The most blocks one fold draws. Measured on the live window: a block is 272px wide
 * and 192px tall, its row's pitch is 238px, and the body of the wide tile holds 681px,
 * so two full rungs read at once and the rest of the flow is had by scrolling it —
 * which is what a large distillation asks for. Twelve blocks is four such rungs, and
 * a hub that pulls more than that is named by the header's own count rather than
 * stacked into a page no one scrolls to the end of.
 */
export const CLAIM_LIMIT = 12;

/**
 * The most records one block's panel lists. Belief#436 carries 36, and measured live
 * their rows are 3459px against the shared modal's 720px cap, which put `esc closes`
 * and the link out 3084px below the fold — so the panel's own way out could not be
 * reached. Six, strongest wire first, and the line beneath says how many it left out;
 * the claim's page, which that link opens, holds all of them.
 */
export const SOURCE_LIMIT = 6;
