import type { MetricPoint } from "../../api/metrics";

/** One metric of an Experiment: its points in step order, and their summary. */
export type Series = {
  readonly key: string;
  readonly steps: readonly number[];
  readonly values: readonly number[];
  readonly last: number;
  readonly min: number;
  readonly max: number;
};

/**
 * `points` grouped by key, keys in the order the server sent them.
 *
 * The server sends `(key, step)` order, so each key's points arrive in step
 * order and in one run.
 */
export function seriesOf(points: readonly MetricPoint[]): Series[] {
  const byKey = new Map<string, MetricPoint[]>();
  for (const point of points) {
    const run = byKey.get(point.key);
    if (run) run.push(point);
    else byKey.set(point.key, [point]);
  }
  return [...byKey].map(([key, run]) => {
    const values = run.map((point) => point.value);
    return {
      key,
      steps: run.map((point) => point.step),
      values,
      last: values.at(-1)!,
      min: Math.min(...values),
      max: Math.max(...values),
    };
  });
}

/** A sparkline's line, the area under it, and its last point, in SVG units. */
export type Spark = { readonly line: string; readonly area: string; readonly end: readonly [number, number] };

/**
 * Draw `series` in a `width` by `height` box, as the mock's `spark()` does:
 * a line scaled to the series' own range, the area under it, and a dot on the
 * last point.
 *
 * Points sit at their steps, so a run logged at uneven steps keeps its shape. A
 * flat series draws across the middle; a single point sits in the centre.
 */
export function sparkOf(series: Pick<Series, "steps" | "values">, width: number, height: number): Spark {
  const { steps, values } = series;
  const [first, last] = [steps[0]!, steps.at(-1)!];
  const [low, high] = [Math.min(...values), Math.max(...values)];
  // Values scale by half when the range overflows (1e308 - -1e308 is Infinity,
  // and Infinity / Infinity is NaN). Halving a double that large is exact.
  const half = Number.isFinite(high - low) ? 1 : 0.5;
  const x = (step: number) => (last === first ? width / 2 : 2 + ((step - first) / (last - first)) * (width - 6));
  const y = (value: number) =>
    high === low ? height / 2 : height - 3 - ((value * half - low * half) / (high * half - low * half)) * (height - 7);
  const points = steps.map((step, index) => [x(step), y(values[index]!)] as const);
  const line = points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join("");
  const end = points.at(-1)!;
  return {
    line,
    area: `${line}L${end[0].toFixed(1)} ${height}L${points[0]![0].toFixed(1)} ${height}Z`,
    end,
  };
}

/**
 * A metric value in a few characters, as the mock prints one: fewer decimals
 * the larger it is, and an exponent when fixed decimals would hide it.
 *
 * The size is judged as printed: 99.96 rounds to 100, so it prints as 100 does.
 */
export function formatValue(value: number): string {
  const size = Math.abs(value);
  if (size === 0) return "0";
  if (size < 1e-3) return value.toExponential(2);
  const printed = Math.abs(Number(value.toFixed(decimals(size))));
  return printed >= 1e6 ? value.toExponential(2) : value.toFixed(decimals(printed));
}

/** How many decimals `formatValue` prints for a value of `size`. */
function decimals(size: number): number {
  return size >= 100 ? 0 : size >= 10 ? 1 : size >= 1 ? 2 : 3;
}
