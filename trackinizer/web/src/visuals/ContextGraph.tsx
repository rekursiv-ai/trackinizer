import { queryOptions, useQuery } from "@tanstack/react-query";
import { useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { type FocusGraph, getGraphFocus } from "../api/graph";
import { useMeta } from "../app/boot";
import { Tooltip, ZoomButtons } from "../graph/controls";
import { type DrawNode, GraphModel, type Lens, PLAIN_LENS } from "../graph/model";
import { useThemePalette } from "../graph/palette";
import { type CreateRenderer, forceGraphRenderer, type Renderer } from "../graph/renderer";
import { hopsName } from "../graph/search";
import { formatRoute, UUID } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { useVisualMark } from "./marks";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import "../graph/graph.css";
import "./ContextGraph.css";

/** The most inquiries one window reads: the record, what lies near it, and the dimmed ring past that. */
export const CONTEXT_LIMIT = 150;

/** How far the window lights, as the catalog bounds `hops`. */
type Reach = 1 | 2 | 3;

/** A window's parameters, read from its instance: how far it lights, and the inquiries it marks. */
export type ContextParams = { readonly hops: Reach; readonly highlight: readonly string[] };

/**
 * The context graph window, `trax.subgraph`: the record in the graph, drawn as
 * the graph page draws it. One read takes what lies within `hops` of the record
 * and the ring one hop past it (`/api/web/graph?focus=`); the record and what
 * lies within `hops` are lit, the ring dimmed, and the record and the inquiries
 * `highlight` names are haloed and titled. A click on another node moves the
 * page to it and asks the canvas to centre the window there, keeping its hops
 * and highlight. The read is keyed by the record's id, so an open detail of
 * the same record keeps it current (`live/detail.ts`).
 *
 * `createRenderer` draws it; tests replace it, as jsdom has no canvas.
 */
export function ContextGraph({
  instance,
  workspace,
  createRenderer = forceGraphRenderer,
}: RendererProps & { readonly createRenderer?: CreateRenderer }) {
  const recordId = instance.record_id ?? null;
  const { hops, highlight } = contextParams(instance.params ?? {});
  const highlighted = highlight.join(",");
  const actions = useWorkspaceActions();
  const palette = useThemePalette(useMeta());
  const query = useQuery({ ...contextQuery(recordId ?? "", hops), enabled: recordId !== null });
  const data = query.data;
  useVisualMark(instance, workspace, "data", query.isSuccess);
  const [model] = useState(() => new GraphModel(palette));
  const host = useRef<HTMLDivElement>(null);
  const [renderer, setRenderer] = useState<Renderer | null>(null);
  const [hovered, setHovered] = useState<{ node: DrawNode; x: number; y: number } | null>(null);
  const lens = useMemo(
    () => (data && recordId ? contextLens(data, recordId, hops, highlighted.split(",")) : PLAIN_LENS),
    [data, recordId, hops, highlighted],
  );
  /** What the window last framed: a new record or reach frames again. */
  const framed = useRef<string | null>(null);

  const open = useEffectEvent((node: DrawNode | null) => {
    if (!node || node.id === recordId) return;
    location.hash = formatRoute({ name: "ref", kind: node.kind, seq: node.seq });
    void actions?.operate({ kind: "show", visual_type: instance.type, record_id: node.id });
  });
  const drawing = recordId !== null;
  useEffect(() => {
    if (!drawing) return;
    const made = createRenderer(
      host.current!,
      {
        hover: (node) => setHovered(node ? { node, ...made.screenOf(node) } : null),
        click: (node) => open(node),
        doubleClick: () => {},
      },
      () => NOTHING_COVERS,
    );
    setRenderer(made);
    return () => made.dispose();
  }, [createRenderer, drawing]);
  useEffect(() => {
    if (!renderer || !data) return;
    model.show(palette, lens);
    const lit = (node: DrawNode) => lens.lit?.has(node.id) ?? true;
    const frame = `${recordId} ${hops}`;
    if (model.apply(data)) renderer.setData(model.nodes, model.links, lit);
    else if (framed.current !== frame) renderer.fit(lit);
    else renderer.repaint();
    framed.current = frame;
  }, [renderer, model, data, palette, lens, recordId, hops]);

  if (recordId === null) return <div className="visual-unsupported">Open a record to see it in context.</div>;
  const focus = data?.nodes.find((row) => row.id === recordId);
  const lit = lens.lit?.size ?? 0;
  return (
    <section className="context-graph" aria-label="Context graph">
      <header className="context-graph-h">
        {focus ? (
          <span className="context-graph-ttl">
            <span className="mono">
              {focus.kind}#{focus.seq}
            </span>
            <span className="context-graph-title">{focus.title || "(untitled)"}</span>
          </span>
        ) : (
          <span className="context-graph-ttl muted">Loading…</span>
        )}
        {data ? (
          <span className="context-graph-count muted num">{`${lit}${data.nodes.length >= CONTEXT_LIMIT ? "+" : ""} ${lit === 1 ? "node" : "nodes"} within ${hopsName(hops)}`}</span>
        ) : (
          <span className="context-graph-count" />
        )}
        <div className="view-seg" role="group" aria-label="Hops">
          {REACHES.map((reach) => (
            <button
              key={reach}
              type="button"
              aria-pressed={reach === hops}
              disabled={!actions || actions.busy}
              onClick={() =>
                void actions?.operate({
                  kind: "show",
                  visual_type: instance.type,
                  record_id: recordId,
                  params: { hops: reach, highlight: highlighted },
                })
              }
            >
              {hopsName(reach)}
            </button>
          ))}
        </div>
        <a className="btn ghost" href={formatRoute({ name: "graph", focus: { ref: { id: recordId }, hops } })}>
          Open in graph
        </a>
      </header>
      <div className="graph-stage">
        <div className="graph-canvas" ref={host} aria-busy={!data} />
        {query.isError ? <ReadFailure error={query.error} retry={() => void query.refetch()} /> : null}
        <ZoomButtons onZoom={(factor) => renderer?.zoomBy(factor)} onFit={() => renderer?.fit((node) => lens.lit?.has(node.id) ?? true)} />
        {hovered ? <Tooltip {...hovered} far={data?.nodes.find((row) => row.id === hovered.node.id)?.hops} hint="Click opens it here" /> : null}
      </div>
    </section>
  );
}

/** The record's neighbourhood, a hop past what the window lights, keyed by its id. */
function contextQuery(recordId: string, hops: Reach) {
  return queryOptions({
    queryKey: ["graph", "context", recordId, hops],
    queryFn: ({ signal }) => getGraphFocus({ focus: recordId, hops: Math.min(3, hops + 1) as Reach, limit: CONTEXT_LIMIT }, { signal }),
  });
}

/**
 * What the window shows of `graph`: lit, each inquiry within `hops` of `focus`
 * by its hops from it, so two out fades as on the graph page and the ring past
 * them dims; haloed and titled, `focus` and those of `highlight` the read
 * brought.
 */
export function contextLens(graph: FocusGraph, focus: string, hops: number, highlight: readonly string[]): Lens {
  const drawn = new Set(graph.nodes.map((row) => row.id));
  const marked = new Set([focus, ...highlight.filter((id) => drawn.has(id))]);
  return {
    ...PLAIN_LENS,
    lit: new Map(graph.nodes.filter((row) => row.hops <= hops).map((row) => [row.id, row.hops])),
    strong: marked,
    labelled: marked,
  };
}

/**
 * A window's parameters from its instance's: `hops` 1 to 3, else 2; `highlight`
 * the inquiry ids it lists, comma-separated, in lower case as the server's are,
 * and nothing else.
 */
export function contextParams(params: { readonly [name: string]: string | number | boolean }): ContextParams {
  const { hops, highlight } = params;
  return {
    hops: REACHES.find((reach) => reach === hops) ?? DEFAULT_HOPS,
    highlight: typeof highlight === "string" ? highlight.split(",").map((id) => id.trim().toLowerCase()).filter((id) => UUID.test(id)) : [],
  };
}

const REACHES: readonly Reach[] = [1, 2, 3];
const DEFAULT_HOPS: Reach = 2;
/** Nothing lies over the window's canvas but its zoom buttons, in a corner. */
const NOTHING_COVERS = { right: 0 };
