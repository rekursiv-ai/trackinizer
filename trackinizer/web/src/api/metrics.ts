import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/**
 * One logged point of an Experiment's metric: its `key`, `step` and scalar
 * `value`. From `readMetrics`, `step` is a safe integer.
 */
export type MetricPoint = components["schemas"]["MetricPoint"];

/** The most points one request returns: the server's `MAX_LIST_LIMIT`. */
export const METRICS_PAGE_LIMIT = 1000;

/** The first page of a run's metrics, and whether the run goes on past it. */
export type MetricsPage = {
  /** The first `METRICS_PAGE_LIMIT` points, in `(key, step)` order. */
  readonly points: readonly MetricPoint[];
  /** The point after the page: null when the page is the whole run. */
  readonly next: MetricPoint | null;
};

/**
 * Fetch the first `METRICS_PAGE_LIMIT` points of an Experiment's metrics with
 * `GET /api/experiments/{id}/metrics`, in `(key, step)` order.
 *
 * A full page proves nothing by itself: a run of exactly that many points fills
 * it too. So a full page asks for the one point after it, which says whether the
 * run is cut and where: in the page's last key, or at a key after it. An id that
 * is not an Experiment is an `ApiError` with status 409.
 *
 * The server stores a step as a BIGINT, up to 2^63 - 1, but a number holds an
 * integer exactly only up to 2^53 - 1: `JSON.parse` reads a larger step as a
 * nearby one, so two steps could print and draw as one. A page with such a step
 * is an `ApiError` with code `unreadable`, not a chart that is wrong.
 */
export async function readMetrics(experimentId: string, { signal }: CallOptions = {}): Promise<MetricsPage> {
  const page = (offset: number, limit: number) =>
    send(TIMEOUT_MS.read, signal, (signal) =>
      client.GET("/api/experiments/{experiment_id}/metrics", {
        params: { path: { experiment_id: experimentId }, query: offset ? { limit, offset } : { limit } },
        signal,
      }),
    );
  const { points } = await page(0, METRICS_PAGE_LIMIT);
  const inexact = points.find((point) => !Number.isSafeInteger(point.step));
  if (inexact) {
    const detail = `Metric ${inexact.key} has a step past ${Number.MAX_SAFE_INTEGER}, the largest this page can show exactly.`;
    throw new ApiError(200, detail, "unreadable");
  }
  if (points.length < METRICS_PAGE_LIMIT) return { points, next: null };
  const { points: after } = await page(METRICS_PAGE_LIMIT, 1);
  return { points, next: after[0] ?? null };
}
