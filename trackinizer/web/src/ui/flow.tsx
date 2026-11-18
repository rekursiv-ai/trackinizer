// THE FLOW NODE: carded items and the relations between them, on React Flow.
//
// This replaces a hand-rolled SVG router. `@xyflow/react` was already a dependency and
// imported by nothing, and it is the library for exactly this: nodes, labelled edges,
// orthogonal routing, arrowheads, pan and zoom. Hand-writing that was 120 lines of
// geometry to re-derive what a 12.x release already ships and tests.
//
// What the agent composes: which blocks exist, which tier each sits in, and the
// relations between them with their own words. What React Flow owns: the routing.
// What this file owns: binding it to the law, because the library arrives with handles,
// a control panel, an attribution badge and its own greys — none of which belong on a
// monochrome instrument.
//
// Positions are derived, not authored: a tier index becomes a row and an index within
// it becomes a column. An agent that had to place nodes in pixels would be designing
// in coordinates, which is the one thing the law never asks of it.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyNodeChanges, Background, Controls, Handle, MarkerType, Position, type Edge, type Node, type NodeChange, ReactFlow } from "@xyflow/react";
import type { ReactNode } from "react";
import "@xyflow/react/dist/base.css";
import "./flow.css";

/** One block in the flow: its slot, what it holds, and the tier it hangs in. */
export type FlowBlock = {
  /** The slot the relations name it by, `K#1`. */
  readonly slot: string;
  /** The tier it sits in, 0 at the top. One up and three down is tiers 0 and 1. */
  readonly tier: number;
  /** Rendered by the caller, so a block is whatever the composition put in it. */
  readonly body: ReactNode;
};

/** One relation, as its own word reads it. */
export type FlowRelation = {
  readonly from: string;
  readonly to: string;
  readonly label: string;
  /** A want rather than an assertion — drawn dashed. */
  readonly open?: boolean;
};

/** The measure a block is laid out at. Not the agent's: a column width is the app's
 *  decision, and 320 is what holds a claim of 7–9 words at the card's own type size
 *  without a second line of title. */
const LANE = 320;
/* The clear corridor left BETWEEN two tiers, once the taller card in the upper tier has
   had its own height. It is a gap, not a pitch: a fixed pitch assumes a card height, and
   every time a block grew a line — a folded record, an evidence tally — the assumption
   went stale and the tiers sat on top of each other again. Tier offsets are accumulated
   from measured heights instead, so a tier of tall cards simply pushes the next one
   down. 110 is what lets a relation's word sit clear of both cards it runs between. */
const TIER_CORRIDOR = 110;
/* Only used before React Flow has measured anything — one frame, then the real heights
   replace it. */
const CARD_GUESS = 240;

/** A block as React Flow takes it: our card, with the library's chrome stripped.
 *
 *  The handles are required, not decorative — an edge resolves to a handle, and a
 *  custom node without them renders with every relation silently dropped. They are
 *  hidden in flow.css rather than omitted, because what is wanted is a card with no
 *  visible ports, not a card with no ports. */
const TYPES = {
  block: ({ data }: { readonly data: { readonly body: ReactNode } }) => (
    <>
      {/* A relation points AT the claim, so the target is the top edge and the source
          the bottom: a child supports the block above it. */}
      <Handle type="target" position={Position.Bottom} isConnectable={false} />
      <Handle type="source" position={Position.Top} isConnectable={false} />
      {data.body}
    </>
  ),
};

/**
 * The flow.
 *
 * `nodesDraggable` is on: a reader rearranging a graph to see it is reading, and the
 * composition's own order is restored by a reload. Pan and zoom come with it, which is
 * what makes a flow of thirty blocks legible at all — the thing the ball cluster never
 * had, because no amount of panning makes a thousand unnamed dots into a reading.
 */
export function Flow({ blocks, relations, height = 560, onNodeClick, onEdgeClick }: {
  readonly blocks: readonly FlowBlock[];
  readonly relations: readonly FlowRelation[];
  readonly height?: number;
  /** Called with the block's slot id when a node is clicked. */
  readonly onNodeClick?: (nodeId: string) => void;
  /** Called with the relation's index when an edge is clicked. */
  readonly onEdgeClick?: (index: number) => void;
}) {
  /** Lay the tiers out downward, each clearing the tallest card in the one above.
   *  `heights` is what React Flow measured; anything unmeasured takes the guess. */
  const place = useCallback((measured: Record<string, number>): Node[] => {
    const tiers = new Map<number, FlowBlock[]>();
    for (const block of blocks) tiers.set(block.tier, [...(tiers.get(block.tier) ?? []), block]);
    const top = new Map<number, number>();
    let cursor = 0;
    for (const tier of [...tiers.keys()].sort((a, b) => a - b)) {
      top.set(tier, cursor);
      const tallest = Math.max(...tiers.get(tier)!.map((b) => measured[b.slot] ?? CARD_GUESS));
      cursor += tallest + TIER_CORRIDOR;
    }
    return blocks.map((block) => {
      const row = tiers.get(block.tier)!;
      const at = row.indexOf(block);
      // Each tier is centred on the same axis, so a single block sits over the middle
      // of three below it and the runs come down symmetrically.
      const span = (row.length - 1) * (LANE + 40);
      return {
        id: block.slot,
        type: "block",
        position: { x: at * (LANE + 40) - span / 2, y: top.get(block.tier)! },
        data: { body: block.body },
        draggable: true,
      };
    });
  }, [blocks]);

  const heights = useRef<Record<string, number>>({});
  const [nodes, setNodes] = useState<Node[]>(() => place({}));
  useEffect(() => { heights.current = {}; setNodes(place({})); }, [place]);

  /** React Flow reports each card's real height once it has rendered it. Those are the
   *  only true heights there are — the body is the caller's ReactNode, so nothing here
   *  can know how tall a block is until it exists. A height that changed re-lays the
   *  tiers; a drag is left exactly where the reader put it. */
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    let remeasured = false;
    for (const change of changes) {
      if (change.type === "dimensions" && change.dimensions) {
        const was = heights.current[change.id];
        if (was !== change.dimensions.height) {
          heights.current[change.id] = change.dimensions.height;
          remeasured = true;
        }
      }
    }
    setNodes((prev) => {
      const next = applyNodeChanges(changes, prev);
      if (!remeasured) return next;
      const laid = place(heights.current);
      const byId = new Map(laid.map((node) => [node.id, node.position]));
      return next.map((node) => ({ ...node, position: byId.get(node.id) ?? node.position }));
    });
  }, [place]);

  // How many relations cross each tier boundary, and this one's index among them:
  // React Flow puts every label at its edge's own midpoint, so two edges spanning the
  // same gap collide there. Knowing the index lets each take a different fraction of
  // its path, which is what separates them without moving the blocks.
  const share = useMemo(() => {
    const tierOf = new Map(blocks.map((block) => [block.slot, block.tier]));
    const lanes = new Map<string, string[]>();
    for (const relation of relations) {
      const key = `${tierOf.get(relation.from)}-${tierOf.get(relation.to)}`;
      lanes.set(key, [...(lanes.get(key) ?? []), `${relation.from}>${relation.to}`]);
    }
    const at = new Map<string, { readonly index: number; readonly of: number }>();
    for (const [, ids] of lanes) {
      ids.forEach((id, index) => at.set(id, { index, of: ids.length }));
    }
    return at;
  }, [blocks, relations]);

  const edges = useMemo<Edge[]>(() => relations.map((relation, at) => ({
    id: `${relation.from}-${relation.to}-${at}`,
    source: relation.from,
    target: relation.to,
    // Right-angled, never a curve: a curve implies a magnitude the store does not hold,
    // and at six relations a bezier fan reads as weather.
    type: "step",
    label: relation.label,
    // Spread the labels across the corridor rather than stacking them at 50%: with
    // three edges over one boundary they sit at 30% / 50% / 70% of their own path.
    // Measured: at a shared midpoint, `spread across B × N tokens` overlapped
    // `paid once per window` and the pane read as one smeared line.
    labelPosition: (() => {
      const lane = share.get(`${relation.from}>${relation.to}`);
      return lane && lane.of > 1 ? 0.3 + (0.4 * lane.index) / (lane.of - 1) : 0.5;
    })(),
    animated: false,
    data: { index: at },
    style: { ...(relation.open ? { strokeDasharray: "5 4" } : undefined), ...(onEdgeClick ? { cursor: "pointer" } : undefined) },
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
  })), [relations]);

  return (
    <div className="ui-flow" style={{ height: `${height}px` }}>
      <ReactFlow
        nodes={nodes}
        onNodesChange={onNodesChange}
        edges={edges}
        nodeTypes={TYPES}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        proOptions={{ hideAttribution: true }}
        nodesConnectable={false}
        elementsSelectable={false}
        minZoom={0.3}
        maxZoom={1.6}
        onNodeClick={onNodeClick ? (_e, node) => onNodeClick(node.id) : undefined}
        onEdgeClick={onEdgeClick ? (_e, edge) => onEdgeClick((edge.data as { index: number })?.index ?? 0) : undefined}
      >
        {/* A dot grid at the pane's own hairline: it says the surface can be panned,
            which a flat black field does not. */}
        <Background gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
