import { useQuery } from "@tanstack/react-query";
import { useDeferredValue, useMemo } from "react";
import type { Detail, DetailRow } from "../api/detail";
import type { FocusGraph } from "../api/graph";
import { useMeta } from "../app/boot";
import { CopyDetails } from "../debug/CopyDetails";
import type { Palette } from "../graph/encode";
import { GraphModel, PLAIN_LENS } from "../graph/model";
import { useThemePalette } from "../graph/palette";
import { formatRoute } from "../router/route";
import { useRouter } from "../router/router";
import { detailQueries, NEIGHBOURHOOD_LIMIT } from "./queries";
import { radialLayout } from "./radial";

/**
 * The graph preview at the top of the rail: what lies within two hops of the
 * inquiry, the inquiry in the middle and each ring a hop further out, drawn as
 * the graph draws it (`encode.ts`), each node named by its `Kind#seq` and title
 * on hover. The whole preview is one link to the graph focused there, where the
 * inquiry opens in Peek.
 *
 * An inquiry with no relations has none, and in the graph's Peek, where the
 * link would lead, there is none either, as there is no Show in graph.
 */
export function GraphPreview({ detail }: { detail: Detail }) {
  const { route } = useRouter();
  const related = [detail.edges, detail.backlinks].some((byEdge) => Object.values(byEdge).some((peers) => peers.length > 0));
  if (route.name === "graph" || !related) return null;
  return <Preview row={detail.self} />;
}

/**
 * The preview of `row`'s neighbourhood: its box at once, so the rail never
 * moves, and the drawing in a later render.
 */
function Preview({ row }: { row: DetailRow }) {
  const palette = useThemePalette(useMeta());
  // Read and drawn after the rest of the page, in a background render React can
  // interrupt, as Activity is: the detail's first paint never waits for it.
  const shown = useDeferredValue(true, false);
  const query = useQuery({ ...detailQueries.neighbourhood(row.id, HOPS), enabled: shown });
  const graph = shown ? query.data : undefined;
  const name = `${row.kind}#${row.seq}`;
  const href = formatRoute({ name: "graph", focus: { ref: { kind: row.kind, seq: row.seq }, hops: HOPS } });
  return (
    <div className="rail-graph">
      <a className="rail-graph-link" href={href} aria-label={`Open in graph: ${name}, ${HOPS} hops`}>
        <svg viewBox={`0 0 ${SIZE.width} ${SIZE.height}`} aria-hidden="true">
          {graph ? <Drawing graph={graph} focus={row.id} palette={palette} /> : null}
        </svg>
        <span className="rail-graph-caption">{graph ? countText(graph.nodes.length) : "Loading the graph…"}</span>
      </a>
      {query.isError ? (
        <p className="rail-graph-error" role="alert">
          Could not load the graph: {query.error.message}
          <CopyDetails message={`Could not load the graph around ${name}: ${query.error.message}`} error={query.error} />
        </p>
      ) : null}
    </div>
  );
}

/**
 * The neighbourhood `graph` around `focus`: edges, then nodes, each with the
 * look the graph's model gives it, lit by its hops so two hops out fades, the
 * focus haloed; sizes scaled down to the preview's.
 */
function Drawing({ graph, focus, palette }: { graph: FocusGraph; focus: string; palette: Palette }) {
  const { nodes, links, placed } = useMemo(() => {
    const model = new GraphModel(palette);
    model.apply(graph);
    model.show(palette, { ...PLAIN_LENS, lit: new Map(graph.nodes.map((row) => [row.id, row.hops])), strong: new Set([focus]) });
    return { nodes: model.nodes, links: model.links, placed: radialLayout(graph, SIZE) };
  }, [graph, focus, palette]);
  return (
    <>
      {links.map(({ from, to, kind, look }) => {
        const [start, end] = [placed.get(from)!, placed.get(to)!];
        return (
          <line
            key={`${from} ${to} ${kind}`}
            x1={start.x}
            y1={start.y}
            x2={end.x}
            y2={end.y}
            stroke={look.color}
            strokeWidth={look.width * SCALE}
            strokeDasharray={look.dash?.map((dash) => dash * SCALE).join(" ")}
          />
        );
      })}
      {nodes.map(({ id, kind, seq, title, look }) => {
        const { x, y } = placed.get(id)!;
        const radius = look.radius * SCALE;
        return (
          <g key={id} data-id={id}>
            <title>{`${kind}#${seq} · ${title || "(untitled)"}`}</title>
            {look.halo ? <circle className="halo" cx={x} cy={y} r={radius + HALO_GAP_PX} fill="none" stroke={look.halo} strokeWidth={1.5} /> : null}
            <circle
              className="disc"
              cx={x}
              cy={y}
              r={radius}
              fill={look.fillAlpha > 0 ? look.fill : "none"}
              fillOpacity={look.fillAlpha}
              stroke={look.ring}
              strokeOpacity={look.ringAlpha}
              strokeWidth={RING_PX}
            />
            {/* The canvas washes the whole disc, then strokes the ring over it; inset by half the ring, the same. */}
            {look.wash ? <circle cx={x} cy={y} r={radius - RING_PX / 2} fill={look.wash} fillOpacity={look.washAlpha} /> : null}
            {look.dot ? <circle cx={x} cy={y} r={radius * 0.4} fill={look.dot} /> : null}
          </g>
        );
      })}
    </>
  );
}

/** `12 nodes within 2 hops`; `60+` when the read's limit cut it short. */
function countText(count: number): string {
  const shown = count >= NEIGHBOURHOOD_LIMIT ? `${count}+` : String(count);
  return `${shown} ${count === 1 ? "node" : "nodes"} within ${HOPS} hops`;
}

const HOPS = 2;
/** The drawing's box, in the units of its `viewBox`; it scales to fit the rail, never past its height. */
const SIZE = { width: 280, height: 190 };
/** The preview's discs and edges, as a fraction of the graph's at 1x. */
const SCALE = 0.6;
const RING_PX = 1.2;
const HALO_GAP_PX = 2.5;
