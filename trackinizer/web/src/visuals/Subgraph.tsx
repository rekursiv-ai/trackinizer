import { useQuery } from "@tanstack/react-query";
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { useCallback } from "react";
import { getDetail, type Detail, type DetailRow, type Peer } from "../api/detail";
import { formatRoute } from "../router/route";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import "@xyflow/react/dist/style.css";

type GraphRow = Pick<DetailRow, "id" | "kind" | "seq" | "title">;
type GraphEntry = { readonly row: GraphRow; readonly depth: number };
type GraphLink = { readonly source: string; readonly target: string; readonly kind: string };
export type SubgraphData = {
  readonly entries: readonly GraphEntry[];
  readonly links: readonly GraphLink[];
};

/** Show a bounded lineage of Issue ancestors around the selected record. */
export function Subgraph({ instance }: RendererProps) {
  const recordId = instance.record_id;
  const actions = useWorkspaceActions();
  const query = useQuery({
    queryKey: ["visual", "subgraph", recordId],
    queryFn: ({ signal }) => loadSubgraph(recordId!, signal),
    enabled: !!recordId,
    retry: false,
  });
  const openNode = useCallback((_event: React.MouseEvent, node: Node) => {
    const href = formatRoute({ name: "lookup", id: node.id });
    if (actions) {
      void actions.revealRecord(node.id).then((revealed) => {
        if (revealed) window.location.hash = href;
      });
    } else {
      window.location.hash = href;
    }
  }, [actions]);

  if (!recordId) return <div className="visual-unsupported">Choose a record to show its context graph.</div>;
  if (query.isPending) return <div className="visual-loading" aria-busy="true">Loading context graph…</div>;
  if (query.isError) return <div className="visual-unsupported" role="alert">
    Could not load context graph. <button className="btn ghost" type="button" onClick={() => void query.refetch()}>Retry</button>
  </div>;

  const nodes: Node[] = query.data.entries.map(({ row }, index) => {
    const selected = row.id === recordId;
    return {
      id: row.id,
      position: { x: 0, y: index * 120 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      data: { label: `${row.kind}#${row.seq} · ${row.title}` },
      style: {
        width: 220,
        padding: 10,
        textAlign: "left",
        whiteSpace: "normal",
        overflowWrap: "anywhere",
        color: "var(--ink)",
        background: selected ? "var(--surface-overlay)" : "var(--surface-raised)",
        border: selected ? "2px solid var(--accent)" : "1px solid var(--line-strong)",
        borderRadius: 8,
        boxShadow: selected ? "0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent)" : undefined,
      },
      ariaLabel: `${selected ? "Selected " : ""}${row.kind}#${row.seq}: ${row.title}`,
    };
  });
  const edges: Edge[] = query.data.links.map(({ source, target, kind }) => ({
    id: `${source}:${kind}:${target}`,
    source,
    target,
    label: kind.replaceAll("_", " "),
    type: "smoothstep",
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--muted)" },
    style: { stroke: "var(--muted)" },
    labelStyle: { fill: "var(--muted)", fontSize: 10 },
  }));

  return <section aria-label="Context graph" style={{ display: "flex", flex: 1, flexDirection: "column", minHeight: 0 }}>
    <p style={{ padding: "8px 12px", color: "var(--muted)", fontSize: 12 }}>
      Issue lineage · {nodes.length} records · select a node to open it
    </p>
    <div style={{ flex: 1, minHeight: 240 }}>
      <ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.1} maxZoom={2}
        nodesDraggable={false} nodesConnectable={false} onNodeClick={openNode}>
        <Background color="var(--line-strong)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  </section>;
}

/** Fetch at most thirty records, four requests at once, through five ancestor hops. */
export async function loadSubgraph(
  recordId: string,
  signal: AbortSignal,
  fetchDetail: (id: string, options: { signal: AbortSignal }) => Promise<Detail> = getDetail,
): Promise<SubgraphData> {
  const root = await fetchDetail(recordId, { signal });
  const entries = new Map<string, GraphEntry>([[recordId, { row: root.self, depth: 0 }]]);
  const links = new Map<string, GraphLink>();
  let frontier: Detail[] = [root];

  for (let depth = 0; depth < 5 && frontier.length; depth++) {
    const candidates: string[] = [];
    for (const detail of frontier) {
      for (const kind of ["produced_by", "narrows"] as const) {
        // Issue provenance branches into sibling campaigns; narrows traces the lineage.
        if (detail.self.kind === "Issue" && kind === "produced_by") continue;
        for (const peer of detail.edges[kind] ?? []) {
          if (peer.kind !== "Issue") continue;
          if (!entries.has(peer.id) && entries.size < 30) {
            entries.set(peer.id, { row: peer as Peer, depth: depth + 1 });
            if (depth < 4) candidates.push(peer.id);
          }
          if (entries.has(peer.id)) {
            const link = { source: detail.self.id, target: peer.id, kind };
            links.set(`${link.source}:${kind}:${link.target}`, link);
          }
        }
      }
    }
    const next: Detail[] = [];
    for (let start = 0; start < candidates.length; start += 4) {
      const batch = await Promise.all(candidates.slice(start, start + 4).map((id) => fetchDetail(id, { signal })));
      next.push(...batch);
    }
    frontier = next;
  }
  return { entries: [...entries.values()], links: [...links.values()] };
}
