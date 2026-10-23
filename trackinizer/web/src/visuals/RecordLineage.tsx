import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useContext, useEffect, useState, type MouseEvent } from "react";
import { lineageQuery, type LineageEdge, type LineageFilters } from "../api/lineage";
import type { TimelineRecord } from "../api/timeline";
import { MetaContext } from "../app/boot";
import { formatRoute } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { useCanvasActions } from "./canvasActions";
import { useVisualMark } from "./marks";
import type { RendererProps } from "./registry";
import "./RecordLineage.css";

/** Shared historical research reader: follow stored links without requiring a launch or inventing a tree. */
export function RecordLineage({ recordId, visualType = "trax.timeline", recordKinds, mark }: {
  readonly recordId: string; readonly visualType?: string; readonly recordKinds?: readonly TimelineRecord["kind"][];
  readonly mark?: Pick<RendererProps, "instance" | "canvas">;
}) {
  const read = useInfiniteQuery(lineageQuery(recordId, { direction: "out" }));
  const target = read.data?.pages[0]?.target;
  const sections = target?.kind === "Paper" ? [...SECTIONS, ...PAPER_SECTIONS] : SECTIONS;
  const [ready, setReady] = useState({ recordId: "", sections: new Set<string>() });
  const landed = useCallback((id: string, title: string) => setReady((previous) => {
    if (previous.recordId === id && previous.sections.has(title)) return previous;
    return { recordId: id, sections: new Set(previous.recordId === id ? previous.sections : []).add(title) };
  }), []);
  useVisualMark(mark?.instance, mark?.canvas ?? null, "data", !!target && ready.recordId === recordId && sections.every(({ title }) => ready.sections.has(title)));
  if (!target) return read.error ? <ReadFailure error={read.error} retry={() => void read.refetch()} />
    : <p aria-busy="true">Loading research records…</p>;
  return <section className="record-lineage" aria-label="Research records">
    <header><span className="mono">{reference(target)} · {target.status}</span><h2>{target.title}</h2></header>
    {target.outcome ? <p className="rl-summary">{target.outcome}</p> : target.description ? <p className="rl-summary">{target.description}</p> : null}
    {sections.map(({ title, filters }) => <Relations key={title} recordId={recordId} visualType={visualType} recordKinds={recordKinds} title={title} filters={filters} landed={landed} />)}
  </section>;
}

const SECTIONS: readonly { readonly title: string; readonly filters: LineageFilters }[] = [
  { title: "Origins and broader questions", filters: { direction: "out" } },
  { title: "Outputs and narrower questions", filters: { direction: "in" } },
  { title: "Citations from this record", filters: { direction: "out", edgeKinds: ["proves", "favors"] } },
  { title: "Evidence citing this record", filters: { direction: "in", edgeKinds: ["proves", "favors"] } },
  { title: "Dependencies and replacements", filters: { direction: "out", edgeKinds: ["requires", "supersedes"] } },
  { title: "Dependents and successors", filters: { direction: "in", edgeKinds: ["requires", "supersedes"] } },
];

const PAPER_SECTIONS: typeof SECTIONS = [
  { title: "References cited", filters: { direction: "out", edgeKinds: ["cites_paper"] } },
  { title: "Cited by papers", filters: { direction: "in", edgeKinds: ["cites_paper"] } },
];

function Relations({ recordId, visualType, recordKinds, title, filters, landed }: {
  readonly recordId: string; readonly visualType: string; readonly recordKinds?: readonly TimelineRecord["kind"][]; readonly title: string; readonly filters: LineageFilters;
  readonly landed: (recordId: string, title: string) => void;
}) {
  const read = useInfiniteQuery(lineageQuery(recordId, filters));
  const hasData = read.data !== undefined;
  useEffect(() => { if (hasData) landed(recordId, title); }, [hasData, landed, recordId, title]);
  const meta = useContext(MetaContext);
  const actions = useCanvasActions();
  const pages = read.data?.pages ?? [];
  const records = [...new Map(pages.flatMap((page) => page.records).map((record) => [record.id, record])).values()];
  const edges = [...new Map(pages.flatMap((page) => page.edges).map((edge) => [`${edge.from_id}:${edge.edge_kind}:${edge.to_id}`, edge])).values()];
  const selected = pages[0]?.target;
  const open = (event: MouseEvent, record: TimelineRecord) => {
    const type = !recordKinds || recordKinds.includes(record.kind) ? visualType : "trax.timeline";
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !actions?.visualTypes.has(type)) return;
    event.preventDefault();
    window.location.hash = href(record);
    actions.operate({ kind: "show", visual_type: type, record_id: record.id });
  };
  return <section aria-label={title}>
    <h3>{title}{read.data ? <span className="muted"> · {records.length} loaded</span> : null}</h3>
    {read.error ? <ReadFailure error={read.error} retry={() => void read.refetch()} /> : null}
    {!read.data && !read.error ? <p aria-busy="true">Loading links…</p> : null}
    {read.data && records.length === 0 ? <p className="muted">No recorded links.</p> : null}
    <ol>{records.map((record) => <li key={record.id}>
      <a href={href(record)} onClick={(event) => open(event, record)}>{reference(record)} · {record.title}</a>
      <span className="muted">{record.status} · {record.created.slice(0, 10)}</span>
      {edges.filter((edge) => edge.from_id === record.id || edge.to_id === record.id).map((edge) => {
        const label = meta?.edges[edge.edge_kind]?.forward ?? edge.edge_kind;
        const source = edge.from_id === record.id ? record : selected;
        const destination = edge.to_id === record.id ? record : selected;
        return <p key={`${edge.from_id}:${edge.edge_kind}:${edge.to_id}`} className="rl-edge">
          {source ? reference(source) : edge.from_id} → {label} → {destination ? reference(destination) : edge.to_id}
          {edge.valence !== null ? <strong data-sign={sign(edge)}> {edge.valence > 0 ? "+" : ""}{edge.valence}</strong> : null}
          {edge.note ? <> · {edge.note}</> : null}
        </p>;
      })}
      {record.outcome ? <p className="rl-summary">{record.outcome}</p> : null}
    </li>)}</ol>
    {read.hasNextPage ? <button className="btn ghost" type="button" disabled={read.isFetchingNextPage} onClick={() => void read.fetchNextPage()}>
      {read.isFetchingNextPage ? "Loading…" : `Show more ${title.toLowerCase()}`}
    </button> : null}
  </section>;
}

const reference = (record: TimelineRecord) => `${record.kind}#${record.seq}`;
const href = (record: TimelineRecord) => formatRoute({ name: "ref", kind: record.kind, seq: record.seq });
const sign = (edge: LineageEdge) => edge.valence === null || edge.valence === 0 ? "neutral" : edge.valence > 0 ? "for" : "against";
