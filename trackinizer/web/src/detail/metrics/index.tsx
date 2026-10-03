import { queryOptions, useQuery } from "@tanstack/react-query";
import { useId } from "react";
import type { DetailRow } from "../../api/detail";
import { METRICS_PAGE_LIMIT, readMetrics } from "../../api/metrics";
import { ReadFailure } from "../../ui/failure";
import { Refresh } from "../Refresh";
import "./metrics.css";
import { formatValue, type Series, seriesOf, sparkOf } from "./series";

/** The metrics section's read, so the stream layer and Refresh can find it by key. */
export const metricsQuery = (experimentId: string) =>
  queryOptions({
    queryKey: ["metrics", experimentId],
    queryFn: ({ signal }) => readMetrics(experimentId, { signal }),
  });

/**
 * An Experiment's metrics as sparklines, one card per key, as the mock draws
 * them. Nothing for other kinds. Metric writes are not in the live stream, so
 * the section reads on open and on Refresh.
 */
export function Metrics({ row }: { row: DetailRow }) {
  return row.kind === "Experiment" ? <ExperimentMetrics id={row.id} /> : null;
}

function ExperimentMetrics({ id }: { id: string }) {
  const query = useQuery(metricsQuery(id));
  const heading = useId();
  const series = query.data ? seriesOf(query.data.points) : [];
  const next = query.data?.next ?? null;
  // The run goes on past the page: in its last key, the card is partial.
  const partial = next !== null && next.key === series.at(-1)?.key;
  return (
    <section className="sec" aria-labelledby={heading} data-section="metrics">
      <div className="sec-h">
        <h2 id={heading}>
          Metrics {query.data ? <span className="count">{series.length}</span> : null}
        </h2>
        <span className="spacer" />
        <Refresh queryKey={metricsQuery(id).queryKey} title="Metric writes do not refresh on their own" />
      </div>
      {!query.data ? (
        query.isError ? (
          <ReadFailure error={query.error} retry={() => void query.refetch()} />
        ) : (
          <p className="unset">Loading metrics…</p>
        )
      ) : series.length === 0 ? (
        <p className="unset">No metrics logged.</p>
      ) : (
        <>
          {next && (
            <p className="metrics-note" role="note">
              Truncated: the server returns at most {METRICS_PAGE_LIMIT.toLocaleString("en")} points, in key order.{" "}
              {partial ? (
                <>
                  Later points of <code>{next.key}</code> and any metric after it are not shown.
                </>
              ) : (
                <>
                  <code>{next.key}</code> and any metric after it are not shown.
                </>
              )}
            </p>
          )}
          <div className="metrics">
            {series.map((one, index) => (
              <Card key={one.key} series={one} partial={partial && index === series.length - 1} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

/** One metric: its key, last value, sparkline, and how many points over which steps. */
function Card({ series, partial }: { series: Series; partial: boolean }) {
  const { key, steps, values, last, min, max } = series;
  const spark = sparkOf(series, 220, 44);
  const count = values.length;
  return (
    <figure className="metric" data-metric={key}>
      <figcaption className="k">{key}</figcaption>
      <span className="v" title={String(last)}>
        {formatValue(last)}
      </span>
      <svg className="spark" width="220" height="44" viewBox="0 0 220 44" aria-hidden="true">
        <path d={spark.area} fill="var(--accent-hover)" opacity=".12" />
        <path d={spark.line} fill="none" stroke="var(--accent-hover)" strokeWidth="1.5" strokeLinejoin="round" />
        <circle cx={spark.end[0].toFixed(1)} cy={spark.end[1].toFixed(1)} r="2.6" fill="var(--accent-hover)" />
      </svg>
      <span className="r">
        {count} {count === 1 ? "point" : "points"} · {steps.length > 1 ? `steps ${steps[0]}–${steps.at(-1)}` : `step ${steps[0]}`}
        {min === max ? "" : ` · range ${formatValue(min)}–${formatValue(max)}`}
        {partial ? " · partial" : ""}
      </span>
    </figure>
  );
}
