import { expect, test } from "vitest";
import type { MetricPoint } from "../../api/metrics";
import { formatValue, seriesOf, sparkOf } from "./series";

const point = (key: string, step: number, value: number): MetricPoint => ({ key, step, value, kind: "scalar", timestamp: null });

test("points group by key in the server's order, each with its last value and range", () => {
  const series = seriesOf([point("loss", 0, 2), point("loss", 5, 0.5), point("loss", 9, 0.8), point("acc", 0, 0.1)]);
  expect(series).toEqual([
    { key: "loss", steps: [0, 5, 9], values: [2, 0.5, 0.8], last: 0.8, min: 0.5, max: 2 },
    { key: "acc", steps: [0], values: [0.1], last: 0.1, min: 0.1, max: 0.1 },
  ]);
});

test("a sparkline spans the box by step and by the series' own range", () => {
  const spark = sparkOf({ steps: [0, 1, 3], values: [0, 10, 5] }, 106, 47);
  // x from 2 to 102 by step; y from 44 (lowest) to 4 (highest), as the mock pads.
  expect(spark.line).toBe("M2.0 44.0L35.3 4.0L102.0 24.0");
  expect(spark.area).toBe("M2.0 44.0L35.3 4.0L102.0 24.0L102.0 47L2.0 47Z");
  expect(spark.end).toEqual([102, 24]);
});

test("a flat series draws across the middle, and a single point sits in the centre", () => {
  expect(sparkOf({ steps: [0, 10], values: [1, 1] }, 100, 40).line).toBe("M2.0 20.0L96.0 20.0");
  expect(sparkOf({ steps: [7], values: [42] }, 100, 40).end).toEqual([50, 20]);
});

test("a range as wide as a double can hold still draws, scaled like any other", () => {
  // high - low overflows to Infinity here, and Infinity / Infinity is NaN.
  const spark = sparkOf({ steps: [0, 1, 2], values: [-1e308, 0, 1e308] }, 220, 44);
  expect(spark.line).toBe("M2.0 41.0L109.0 22.5L216.0 4.0");
  expect(spark.end).toEqual([216, 4]);
});

test("values print with fewer decimals the larger they are, and tiny or huge ones with an exponent", () => {
  expect([0, 0.0712, 1.5, 12.34, 456.7, -3.25, 0.00004, 2_500_000].map(formatValue)).toEqual([
    "0",
    "0.071",
    "1.50",
    "12.3",
    "457",
    "-3.25",
    "4.00e-5",
    "2.50e+6",
  ]);
});

test("a value that rounds up into a larger size prints as that size does", () => {
  expect([99.96, -99.96, 9.996, 0.9996, 999999.6, 99.94].map(formatValue)).toEqual([
    "100",
    "-100",
    "10.0",
    "1.00",
    "1.00e+6",
    "99.9",
  ]);
});
