import type { EvidenceTimeline, TimelineExperiment, TimelineRecord } from "../api/timeline";

/** What a row is to the record in the middle: a lead above it, itself, or a direction below. */
export type RowRole = "lead" | "record" | "direction";
export type TimelineRow = {
  readonly role: RowRole;
  readonly record: TimelineRecord;
  /** Results along the row, oldest first. */
  readonly results: readonly TimelineExperiment[];
};

/** Narrowest the chart is laid out; a narrower window scrolls rather than squeezes. */
export const MIN_WIDTH = 520;
/** Width used before the window has been measured. */
export const DEFAULT_WIDTH = 900;
const LABEL_MIN = 170;
const LABEL_MAX = 330;
const LABEL_SHARE = 0.32;
/** Mean glyph width of the 12.5px row title, in px. */
const CHAR_PX = 6.6;
export const LEAD_W = 90;
export const PAD_R = 36;
export const ROW_H = 58;
export const TOP = 64;
export const BOTTOM = 36;
/** The least a day tick may sit from the next one, in px. */
export const MIN_TICK_PX = 64;
/** Results made close together sit this far apart rather than on top of each other. */
export const RESULT_GAP = 15;

const DAY = 86_400_000;
const TICK_STEPS = [1, 2, 3, 7, 14, 30] as const;

/**
 * The view's rows, top to bottom: leads (farthest first), the record, then its
 * directions. An Experiment's own row is the Issue that produced it, and the
 * Experiment sits among that row's results.
 */
export function timelineRows(data: EvidenceTimeline): readonly TimelineRow[] {
  const { target, issue, selected_result: selected } = data;
  const own = target.kind === "Experiment" && issue ? issue : target;
  const results = [...data.root_results];
  if (selected && !results.some(({ record }) => record.id === selected.record.id)) results.push(selected);
  const byDate = (a: TimelineExperiment, b: TimelineExperiment) => a.record.created.localeCompare(b.record.created);
  return [
    ...data.leads.map((record): TimelineRow => ({ role: "lead", record, results: [] })),
    { role: "record", record: own, results: results.sort(byDate) },
    ...data.directions.map(({ issue: record, results: found }): TimelineRow => (
      { role: "direction", record, results: [...found].sort(byDate) })),
  ];
}

export type Axis = {
  /** The chart's width in CSS px: never below `MIN_WIDTH`. */
  readonly width: number;
  /** Width of the label column, which narrows with the chart. */
  readonly labelW: number;
  /** Left edge of the time axis: the label column, then the room for pinned leads. */
  readonly axisL: number;
  /** Characters of a row title the label column fits. */
  readonly titleChars: number;
  readonly t0: number;
  readonly t1: number;
  /** Days between ticks. */
  readonly step: number;
  readonly x: (iso: string) => number;
};

/**
 * Time across. The axis spans the record and its own work; leads are context
 * pinned at its left edge with their own dates, so an old lead squeezes nothing.
 */
export function timeAxis(rows: readonly TimelineRow[], available: number): Axis {
  const width = Math.max(MIN_WIDTH, Math.round(available));
  const labelW = Math.round(Math.min(LABEL_MAX, Math.max(LABEL_MIN, width * LABEL_SHARE)));
  const axisL = labelW + LEAD_W;
  const times = rows.filter((row) => row.role !== "lead")
    .flatMap((row) => [row.record.created, ...row.results.map(({ record }) => record.created)])
    .map((iso) => Date.parse(iso));
  const t0 = Math.floor(Math.min(...times) / DAY) * DAY - DAY / 2;
  const t1 = Math.ceil(Math.max(...times) / DAY) * DAY + DAY / 2;
  const span = width - axisL - PAD_R;
  const step = TICK_STEPS.find((days) => (days * DAY * span) / (t1 - t0) >= MIN_TICK_PX) ?? 30;
  return {
    width, labelW, axisL, titleChars: Math.floor((labelW - 30) / CHAR_PX), t0, t1, step,
    x: (iso) => axisL + ((Date.parse(iso) - t0) / (t1 - t0)) * span,
  };
}

/** Tick times, one every `step` days from the axis's first midnight. */
export function ticks(axis: Axis): readonly number[] {
  const out: number[] = [];
  for (let t = axis.t0 + DAY / 2; t <= axis.t1; t += axis.step * DAY) out.push(t);
  return out;
}

export const rowY = (index: number) => TOP + index * ROW_H + ROW_H / 2;
export const heightOf = (rows: number) => TOP + rows * ROW_H + BOTTOM;

/** Where a row's node sits: leads at the axis's left edge, the rest at their date. */
export function nodeAt(row: TimelineRow, index: number, axis: Axis) {
  return { cx: row.role === "lead" ? axis.labelW + 18 : axis.x(row.record.created), cy: rowY(index) };
}

/** A result's x along its row: its date, nudged right of the one before it. */
export function resultXs(row: TimelineRow, axis: Axis): readonly number[] {
  let last = -Infinity;
  return row.results.map(({ record }) => {
    last = Math.max(axis.x(record.created), last + RESULT_GAP);
    return last;
  });
}
