import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type MouseEvent } from "react";
import { getEvidenceTimeline, type TimelineEvidence, type TimelineRecord } from "../api/timeline";
import { formatRoute } from "../router/route";
import { useVisualMark } from "./marks";
import type { RendererProps } from "./registry";
import {
  DEFAULT_WIDTH, ROW_H, TOP, BOTTOM, heightOf, nodeAt, resultXs, rowY, ticks, timeAxis, timelineRows,
  type Axis, type RowRole, type TimelineRow,
} from "./timelineLayout";
import { useWorkspaceActions } from "./workspaceActions";
import "../graph/tokens.css";
import "./Timeline.css";

const ROLE_TAG: Readonly<Record<RowRole, string>> = { lead: "lead", record: "this record", direction: "direction" };
/** Evidence badges drawn above one result; the rest are summed and listed on hover. */
const MARKS_SHOWN = 4;

/**
 * Lineage and timeline in one view, `trax.timeline`. Time runs across; lineage
 * runs down: lead issues pinned at the axis's left edge, the record, then its
 * directions as rows. Each row's results sit on it by date, their signed
 * evidence marked for or against. A card or square moves the page to that
 * record and re-centres this window on it.
 */
export function Timeline({ instance, workspace, width }: RendererProps & { readonly width?: number }) {
  const recordId = instance.record_id;
  const directionLimit = bounded(instance.params?.direction_limit, 8, 12);
  const resultsPerDirection = bounded(instance.params?.results_per_direction, 3, 5);
  const query = useQuery({
    queryKey: ["visual", "timeline", recordId, directionLimit, resultsPerDirection],
    queryFn: ({ signal }) => getEvidenceTimeline(recordId!, {
      directionLimit, resultsPerDirection, signal,
    }),
    enabled: !!recordId,
    retry: false,
  });
  useVisualMark(instance, workspace, "data", query.isSuccess);
  const actions = useWorkspaceActions();
  const [host, setHost] = useState<HTMLElement | null>(null);
  const measured = useWidth(host);
  if (!recordId) return <div className="visual-unsupported">Choose a record to show its lineage and timeline.</div>;
  if (query.isPending) return <div className="visual-loading" aria-busy="true">Loading lineage and timeline…</div>;
  if (query.isError) {
    return <div className="visual-unsupported" role="alert">
      Could not load lineage and timeline.
      <button className="btn ghost" type="button" onClick={() => void query.refetch()}>Retry</button>
    </div>;
  }
  const data = query.data;
  const rows = timelineRows(data);
  const axis = timeAxis(rows, width ?? measured);
  const lineAt = (index: number) => nodeAt(rows[index]!, index, axis);
  const at = rows.findIndex((row) => row.role === "record");
  const open = (event: MouseEvent, target: TimelineRecord) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (target.id === recordId) return;
    window.location.hash = recordHref(target);
    actions?.operate({ kind: "show", visual_type: instance.type, record_id: target.id });
  };
  const height = heightOf(rows.length);
  const alone = rows.length === 1 && rows[0]!.results.length === 0;
  return <section className="lineage-timeline" ref={setHost} aria-label="Lineage and timeline">
    <header className="lineage-timeline-heading">
      <span className="mono">{ref(data.target)}</span>
      <h2>{data.target.title}</h2>
    </header>
    <Legend rows={rows} />
    <svg className="tl-chart" width={axis.width} height={height} viewBox={`0 0 ${axis.width} ${height}`} role="group" aria-label="Lineage and timeline chart">
      <g aria-hidden="true">
        {ticks(axis).map((time) => {
          const x = axis.x(new Date(time).toISOString());
          return <g key={time}>
            <line x1={x} y1={TOP - 8} x2={x} y2={height - BOTTOM} className="tl-tick" />
            <text x={x} y={TOP - 16} className="tl-tick-label">{day(new Date(time).toISOString())}</text>
          </g>;
        })}
        <line x1={axis.axisL - 8} y1={TOP - 8} x2={axis.axisL - 8} y2={height - BOTTOM} className="tl-lead-edge" />
      </g>
      {rows.map((row, index) => <Band key={`band-${row.record.id}`} row={row} index={index} width={axis.width} />)}
      <g aria-hidden="true" fill="none">
        {rows.slice(0, at).map((_, index) => <path key={`up-${index}`} className="tl-lineage"
          d={curve(lineAt(index), lineAt(index + 1), "vertical")} />)}
        {rows.slice(at + 1).map((_, index) => <path key={`down-${index}`} className="tl-lineage"
          d={curve(lineAt(at), lineAt(at + 1 + index), "horizontal")} />)}
      </g>
      {rows.map((row, index) => <Results key={`results-${row.record.id}`} row={row} index={index} axis={axis}
        selectedId={data.selected_result?.record.id} open={open} />)}
      {rows.map((row, index) => <Cards key={`card-${row.record.id}`} row={row} index={index} axis={axis} open={open} />)}
    </svg>
    {data.root_results_truncated
      && <p className="lineage-timeline-note">Showing the latest {resultsPerDirection} results of this record.</p>}
    {data.directions_truncated
      && <p className="lineage-timeline-note">Showing the first {directionLimit} directions.</p>}
    {alone && <p className="lineage-timeline-note" role="status">Nothing else is linked to this record yet.</p>}
  </section>;
}

function Band({ row, index, width }: { readonly row: TimelineRow; readonly index: number; readonly width: number }) {
  const cls = row.role === "record" ? "tl-band-record" : row.role === "lead" ? "tl-band-lead" : index % 2 ? "tl-band-alt" : "";
  return <rect x={0} y={TOP + index * ROW_H} width={width} height={ROW_H} className={`tl-band ${cls}`} aria-hidden="true" />;
}

/** The label card on the left and the node on the row; each moves the window to the row's record. */
function Cards({ row, index, axis, open }: {
  readonly row: TimelineRow; readonly index: number; readonly axis: Axis;
  readonly open: (event: MouseEvent, record: TimelineRecord) => void;
}) {
  const { record } = row;
  const { cx, cy } = nodeAt(row, index, axis);
  const radius = row.role === "record" ? 9 : 7;
  const y = rowY(index);
  const label = `${ref(record)} ${record.title}`;
  return <g data-role={row.role} data-ref={ref(record)}>
    <a href={recordHref(record)} aria-label={label} onClick={(event) => open(event, record)}>
      <title>{hover(record)}</title>
      <rect x={0} y={TOP + index * ROW_H} width={axis.labelW} height={ROW_H} className="tl-card-hit" />
      <text x={16} y={y - 4} className="tl-row-ref">{ref(record)}</text>
      <text x={16} y={y + 13} className="tl-row-title">{clip(record.title, axis.titleChars)}</text>
      <text x={axis.labelW - 14} y={y - 4} className="tl-row-tag">{ROLE_TAG[row.role]}</text>
      {row.role === "record" && <circle cx={cx} cy={cy} r={radius + 5} className="tl-halo" />}
      <circle cx={cx} cy={cy} r={radius} className={`tl-node tl-kind-${record.kind}${faded(record)}`} />
    </a>
    {row.role === "lead" && <text x={axis.labelW + 30} y={cy + 4} className="tl-lead-date" aria-hidden="true">{day(record.created)}</text>}
  </g>;
}

/** The row's results at their dates, each joined to the row's node, with evidence badges above. */
function Results({ row, index, axis, selectedId, open }: {
  readonly row: TimelineRow; readonly index: number; readonly axis: Axis; readonly selectedId: string | undefined;
  readonly open: (event: MouseEvent, record: TimelineRecord) => void;
}) {
  const node = nodeAt(row, index, axis);
  const xs = resultXs(row, axis);
  const ry = rowY(index) + 12;
  return <g>
    {row.results.map(({ record, evidence, evidence_truncated }, i) => {
      const rx = xs[i]!;
      const shown = evidence.slice(0, MARKS_SHOWN);
      const more = evidence.length - shown.length;
      return <g key={record.id} className={`tl-result${record.id === selectedId ? " tl-selected" : ""}`} data-ref={ref(record)}>
        <line x1={node.cx} y1={node.cy} x2={rx} y2={ry} className="tl-produced" aria-hidden="true" />
        <a href={recordHref(record)} aria-label={`${ref(record)} ${record.title}`} onClick={(event) => open(event, record)}>
          <title>{resultHover(record, evidence, evidence_truncated)}</title>
          <rect x={rx - 6} y={ry - 6} width={12} height={12} rx={3}
            className={`tl-square tl-kind-${record.kind}${faded(record)}`} />
        </a>
        {shown.map((item, k) => <Mark key={`${item.claim.id}:${item.edge_kind}`} item={item} x={rx} y={ry - 27 - k * 15} />)}
        {(more > 0 || evidence_truncated) && <g className="tl-mark" data-sign="neutral">
          <rect x={rx - 15} y={ry - 27 - shown.length * 15} width={30} height={13} rx={6} />
          <text x={rx} y={ry - 17.5 - shown.length * 15}>{more > 0 ? `+${more}` : "…"}</text>
        </g>}
      </g>;
    })}
  </g>;
}

function Mark({ item, x, y }: { readonly item: TimelineEvidence; readonly x: number; readonly y: number }) {
  const sign = signOf(item.valence);
  const text = item.valence === null ? "n/a" : `${item.valence > 0 ? "+" : item.valence < 0 ? "−" : ""}${Math.abs(item.valence)}`;
  return <g className="tl-mark" data-sign={sign}>
    <rect x={x - 15} y={y} width={30} height={13} rx={6} />
    <text x={x} y={y + 9.5}>{text}</text>
    <title>{evidenceLine(item)}</title>
  </g>;
}

function Legend({ rows }: { readonly rows: readonly TimelineRow[] }) {
  const kinds = [...new Set(rows.flatMap((row) => [row.record.kind, ...row.results.map(({ record }) => record.kind)]))];
  return <div className="tl-legend" aria-hidden="true">
    {kinds.map((kind) => <span key={kind}><svg width="10" height="10" className="tl-legend-swatch"><rect width="10" height="10" rx="3" className={`tl-kind-${kind}`} /></svg> {kind}</span>)}
    <span><i className="tl-key-for" />evidence for</span>
    <span><i className="tl-key-against" />evidence against</span>
    <span>faded: complete</span>
  </div>;
}

function curve(a: { cx: number; cy: number }, b: { cx: number; cy: number }, shape: "vertical" | "horizontal"): string {
  const mid = (a.cy + b.cy) / 2;
  return shape === "vertical"
    ? `M${a.cx},${a.cy} C${a.cx},${mid} ${b.cx},${mid} ${b.cx},${b.cy}`
    : `M${a.cx},${a.cy} C${a.cx},${b.cy} ${a.cx},${b.cy} ${b.cx},${b.cy}`;
}

function signOf(valence: number | null): "for" | "against" | "neutral" {
  if (valence === null || valence === 0) return "neutral";
  return valence > 0 ? "for" : "against";
}

function evidenceLine({ claim, edge_kind, valence, note }: TimelineEvidence): string {
  const verdict = { for: "for", against: "against", neutral: "neutral to" }[signOf(valence)];
  return `${verdict} ${ref(claim)} (${edge_kind}${valence === null ? "" : ` ${valence}`}): ${clip(claim.title, 90)}${note ? ` · ${clip(note, 120)}` : ""}`;
}

const ref = (record: TimelineRecord) => `${record.kind}#${record.seq}`;
const recordHref = (record: TimelineRecord) => formatRoute({ name: "ref", kind: record.kind, seq: record.seq });
const faded = (record: TimelineRecord) => record.status === "complete" ? " tl-faded" : "";
const stamp = (iso: string) => iso.slice(0, 16).replace("T", " ");
const hover = (record: TimelineRecord) => `${ref(record)} · ${record.title}\n${record.status} · ${stamp(record.created)}`;

function resultHover(record: TimelineRecord, evidence: readonly TimelineEvidence[], truncated: boolean): string {
  return [
    hover(record),
    record.outcome ? `Outcome: ${clip(record.outcome, 300)}` : "",
    ...evidence.map(evidenceLine),
    truncated ? "More evidence is on the Experiment." : "",
  ].filter(Boolean).join("\n");
}

/** The element's width in CSS px, kept current; a default until it is measured (jsdom has no observer). */
function useWidth(host: HTMLElement | null): number {
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  useEffect(() => {
    if (!host || typeof ResizeObserver === "undefined") return;
    const read = () => { if (host.clientWidth > 0) setWidth(host.clientWidth); };
    read();
    const observer = new ResizeObserver(read);
    observer.observe(host);
    return () => observer.disconnect();
  }, [host]);
  return width;
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

function clip(text: string, length: number): string {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function bounded(value: unknown, fallback: number, maximum: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? Math.min(value, maximum) : fallback;
}
