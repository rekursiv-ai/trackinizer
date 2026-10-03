import { client, send, TIMEOUT_MS, type CallOptions } from "./client";
import type { components } from "./generated/schema";

export type TimelineRecord = components["schemas"]["TimelineRecord"];
export type TimelineEvidence = components["schemas"]["TimelineEvidence"];
export type TimelineExperiment = components["schemas"]["TimelineExperiment"];
export type TimelineDirection = components["schemas"]["TimelineDirection"];
export type EvidenceTimeline = components["schemas"]["EvidenceTimelineResponse"];
export type TimelineOptions = CallOptions & {
  readonly directionLimit?: number;
  readonly resultsPerDirection?: number;
};

export const TIMELINE_MAX_DIRECTIONS = 12;
export const TIMELINE_MAX_RESULTS = 5;
export const TIMELINE_MAX_EVIDENCE = 6;

/** Fetch one bounded Issue/Experiment timeline through the generated API client. */
export async function getEvidenceTimeline(
  recordId: string,
  { directionLimit = 8, resultsPerDirection = 3, signal }: TimelineOptions = {},
): Promise<EvidenceTimeline> {
  if (!Number.isInteger(directionLimit) || directionLimit < 1 || directionLimit > TIMELINE_MAX_DIRECTIONS
    || !Number.isInteger(resultsPerDirection) || resultsPerDirection < 1 || resultsPerDirection > TIMELINE_MAX_RESULTS) {
    throw new Error("Invalid evidence timeline bounds");
  }
  const value = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/visuals/timeline/{record_id}", {
      params: { path: { record_id: recordId }, query: {
        direction_limit: directionLimit, results_per_direction: resultsPerDirection,
      } }, signal,
    }),
  );
  if (!isEvidenceTimeline(value)) throw new Error("Invalid evidence timeline");
  return value;
}

function isEvidenceTimeline(value: unknown): value is EvidenceTimeline {
  if (!isObject(value) || !isRecord(value.target) || !(value.issue === null || isRecord(value.issue))) return false;
  if (!isRecordArray(value.root_results, isExperiment, TIMELINE_MAX_RESULTS)
    || !isRecordArray(value.directions, isDirection, TIMELINE_MAX_DIRECTIONS)
    || !isRecordArray(value.unresolved_questions, isRecord, TIMELINE_MAX_DIRECTIONS)
    || !(value.selected_result === null || isExperiment(value.selected_result))) return false;
  return typeof value.root_results_truncated === "boolean"
    && typeof value.directions_truncated === "boolean";
}

function isExperiment(value: unknown): value is TimelineExperiment {
  return isObject(value) && isRecord(value.record)
    && isRecordArray(value.evidence, isEvidence, TIMELINE_MAX_EVIDENCE)
    && typeof value.evidence_truncated === "boolean";
}
function isDirection(value: unknown): value is TimelineDirection {
  return isObject(value) && isRecord(value.issue)
    && isRecordArray(value.results, isExperiment, TIMELINE_MAX_RESULTS)
    && typeof value.results_truncated === "boolean";
}
function isEvidence(value: unknown): value is TimelineEvidence {
  return isObject(value) && isRecord(value.claim)
    && (value.edge_kind === "proves" || value.edge_kind === "favors")
    && (value.valence === null || (typeof value.valence === "number"
      && Number.isFinite(value.valence) && value.valence >= -1 && value.valence <= 1))
    && (value.note === null || typeof value.note === "string");
}
function isRecord(value: unknown): value is TimelineRecord {
  return isObject(value) && typeof value.id === "string"
    && (value.kind === "Issue" || value.kind === "Experiment" || value.kind === "Belief")
    && typeof value.seq === "number" && typeof value.title === "string"
    && typeof value.status === "string" && typeof value.created === "string"
    && typeof value.modified === "string"
    && (value.description === null || typeof value.description === "string")
    && (value.outcome === null || typeof value.outcome === "string");
}
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isRecordArray<T>(
  value: unknown,
  guard: (item: unknown) => item is T,
  maximum: number,
): value is T[] {
  return Array.isArray(value) && value.length <= maximum && value.every(guard);
}
