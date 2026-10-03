import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { getEvidenceTimeline, type TimelineExperiment, type TimelineRecord } from "../api/timeline";
import { formatRoute } from "../router/route";
import type { RendererProps } from "./registry";
import { useWorkspaceActions } from "./workspaceActions";
import "./Timeline.css";

/** Render a bounded chronology with visible dates and citations. */
export function Timeline({ instance }: RendererProps) {
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
  if (!recordId) return <div className="visual-unsupported">Choose a record to show its evidence timeline.</div>;
  if (query.isPending) return <div className="visual-loading" aria-busy="true">Loading evidence timeline…</div>;
  if (query.isError) {
    const unsupported = query.error instanceof ApiError && query.error.status === 422;
    return <div className="visual-unsupported" role="alert">
      {unsupported ? "Evidence timeline supports Issue and Experiment records only." : "Could not load evidence timeline."}
      {!unsupported && <button className="btn ghost" type="button" onClick={() => void query.refetch()}>Retry</button>}
    </div>;
  }
  const timeline = query.data;
  const selectedIsInRoot = timeline.selected_result !== null
    && timeline.root_results.some(({ record }) => record.id === timeline.selected_result?.record.id);
  return <section className="evidence-timeline" aria-label="Evidence timeline">
    <header className="evidence-timeline-heading">
      <h2>{timeline.issue?.title ?? timeline.target.title}</h2>
      {timeline.issue && <RecordLink record={timeline.issue} />}
    </header>
    {timeline.root_results.length > 0 && <TimelineGroup title="Results" results={timeline.root_results} />}
    {timeline.selected_result && !selectedIsInRoot
      && <TimelineGroup title="Selected result" results={[timeline.selected_result]} />}
    {timeline.root_results_truncated
      && <p className="timeline-truncated">Showing the latest {resultsPerDirection} root results.</p>}
    {timeline.directions.map(({ issue, results, results_truncated }) => <article className="timeline-direction" key={issue.id}>
      <div className="timeline-date">{date(issue.created)}</div>
      <div className="timeline-direction-body">
        <h3><RecordLink record={issue} /></h3>
        <p className="timeline-status">{issue.status}{timeline.unresolved_questions.some((row) => row.id === issue.id) ? " · unresolved question" : ""}</p>
        {issue.description && <details className="timeline-detail">
          <summary>Direction details</summary><p>{issue.description}</p>
        </details>}
        {results.length > 0 && <TimelineResults results={results} />}
        {results_truncated && <p className="timeline-truncated">More results are available on this Issue.</p>}
      </div>
    </article>)}
    {timeline.selected_result === null && timeline.directions.length === 0 && timeline.root_results.length === 0
      && <p className="timeline-empty">No directions or results are linked to this Issue yet.</p>}
    {timeline.directions_truncated && <p className="timeline-truncated">Showing the first {directionLimit} directions.</p>}
  </section>;
}

function TimelineGroup({ title, results }: { readonly title: string; readonly results: readonly TimelineExperiment[] }) {
  return <section className="timeline-root-results"><h3>{title}</h3><TimelineResults results={results} /></section>;
}

function TimelineResults({ results }: { readonly results: readonly TimelineExperiment[] }) {
  return <ol className="timeline-results">
    {results.map(({ record, evidence, evidence_truncated }) => <li className="timeline-result" key={record.id}>
      <div className="timeline-date">{date(record.created)}</div>
      <div className="timeline-result-body">
        <h4><RecordLink record={record} /></h4>
        {record.outcome && <p className="timeline-outcome">{record.outcome}</p>}
        <details className="timeline-detail">
          <summary>Evidence and details</summary>
          {record.description && <p>{record.description}</p>}
          {evidence.length > 0 ? <ul className="timeline-evidence">
            {evidence.map(({ claim, edge_kind, valence, note }) => <li key={`${claim.id}:${edge_kind}`}>
              <span className={evidenceClass(valence)}>
                {edge_kind} · {evidenceJudgement(valence)}
                {valence === null ? "" : ` (${valence})`}
              </span>{" "}<RecordLink record={claim} />{note && <span> · {note}</span>}
            </li>)}
          </ul> : <p>No signed evidence edges are linked.</p>}
          {evidence_truncated && <p className="timeline-truncated">Additional evidence is available on the Experiment.</p>}
        </details>
      </div>
    </li>)}
  </ol>;
}

function evidenceJudgement(valence: number | null): string {
  if (valence === null) return "valence not recorded";
  if (valence < 0) return "argues against";
  if (valence === 0) return "neutral";
  return "supports";
}

function evidenceClass(valence: number | null): string {
  if (valence === null || valence === 0) return "evidence-neutral";
  return valence < 0 ? "evidence-negative" : "evidence-positive";
}

function RecordLink({ record }: { readonly record: TimelineRecord }) {
  const actions = useWorkspaceActions();
  const href = formatRoute({ name: "lookup", id: record.id });
  return <a href={href} aria-disabled={actions?.busy ?? false} tabIndex={actions?.busy ? -1 : undefined}
    onClick={(event) => {
      if (!actions || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (actions.busy) return;
      void actions.revealRecord(record.id).then((revealed) => {
        if (revealed) window.location.hash = href;
      });
    }}>{record.kind}#{record.seq} {record.title}</a>;
}

function date(value: string): string {
  return value.slice(0, 10);
}

function bounded(value: unknown, fallback: number, maximum: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? Math.min(value, maximum) : fallback;
}
